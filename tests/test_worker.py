from __future__ import annotations

import importlib
import subprocess

import pytest

from app import db


def test_mark_interrupted_running_jobs(data_dir):
    db.init_db()
    job = db.create_job("x", "sprint_2m", {})
    db.update_job(job["id"], status="running", phase="prepare")
    ids = db.mark_interrupted_running_jobs("test interrupt")
    assert job["id"] in ids
    got = db.get_job(job["id"])
    assert got["status"] == "failed"
    assert "test interrupt" in (got["error"] or "")


def test_worker_timeout_frees_slot(monkeypatch, data_dir):
    monkeypatch.setenv("JOB_TIMEOUT_SECONDS", "1")
    import app.settings as settings

    importlib.reload(settings)
    import app.worker as worker

    importlib.reload(worker)

    db.init_db()
    job = db.create_job("timeout", "sprint_2m", {})
    job_id = job["id"]
    db.update_job(job_id, status="running", phase="starting")
    (data_dir / "jobs" / job_id / "work").mkdir(parents=True, exist_ok=True)

    class FakeProc:
        pid = 4242
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)

    monkeypatch.setattr(worker, "_popen_job", lambda _jid: FakeProc())
    monkeypatch.setattr(worker, "_terminate_process_tree", lambda _proc: None)

    worker._running = job_id
    worker._run(job_id)

    got = db.get_job(job_id)
    assert got["status"] == "failed"
    assert "časový limit" in (got["error"] or "")
    assert worker._running is None


def test_requeue_after_restart_starts_waiting_jobs(monkeypatch, data_dir):
    import app.worker as worker

    importlib.reload(worker)
    db.init_db()
    first = db.create_job("a", "sprint_2m", {})["id"]
    second = db.create_job("b", "sprint_2m", {})["id"]
    db.update_job(first, status="queued", phase="waiting")
    db.update_job(second, status="queued", phase="waiting")
    started: list[str] = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            started.append(args[0])

        def start(self):
            pass

    monkeypatch.setattr(worker.threading, "Thread", FakeThread)

    assert worker.requeue_after_restart() == [first, second]
    assert started == [first]
    assert worker._running == first
    assert worker._queue == [second]
    assert db.get_job(first)["status"] == "running"
    assert db.get_job(second)["status"] == "queued"
