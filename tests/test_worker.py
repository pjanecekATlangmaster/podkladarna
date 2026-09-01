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
