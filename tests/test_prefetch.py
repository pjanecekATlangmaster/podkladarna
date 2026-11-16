from __future__ import annotations

import importlib
import threading
import time

import pytest

from app import db


@pytest.fixture()
def worker(data_dir, monkeypatch):
    import app.worker as worker

    importlib.reload(worker)
    started: list[tuple[str, str]] = []

    class FakeThread:
        def __init__(self, target, args, daemon, name=None):
            self.target, self.job = target, args[0]

        def start(self):
            started.append((self.target.__name__, self.job))

    monkeypatch.setattr(worker.threading, "Thread", FakeThread)
    worker.started = started
    return worker


def _job(name: str, sheets: list[str], **extra) -> str:
    opts = {"bbox_wgs84": [14.0, 50.0, 14.05, 50.03], "sm5_sheets": sheets, **extra}
    return db.create_job(name, "sprint_2m", opts)["id"]


@pytest.mark.parametrize(
    ("cached", "extra", "expect"),
    [
        ([], {}, ["A", "B"]),  # dva listy mimo cache = velké stahování
        (["A"], {}, []),  # jen jeden chybí: pod prahem, job jde rovnou do fronty
        ([], {"country": "AT"}, []),  # mimo Česko výšky jen do složky jobu
        ([], {"reused_from": "x"}, []),
        ([], {"force_refresh": True}, []),
    ],
)
def test_job_missing_sheets(data_dir, monkeypatch, cached, extra, expect):
    import app.pipeline.fetch_openzu as fz
    from app import prefetch

    monkeypatch.setattr(fz, "dmpok_cached_mapnoms", lambda names: {n for n in names if n in cached})
    opts = {"bbox_wgs84": [14.0, 50.0, 14.05, 50.03], "sm5_sheets": ["A", "B"], **extra}
    assert prefetch.job_missing_sheets(opts) == expect


def test_big_download_info(data_dir, monkeypatch):
    import app.pipeline.fetch_openzu as fz
    from app import prefetch

    monkeypatch.setattr(fz, "dmpok_cached_mapnoms", lambda names: set())
    info = prefetch.big_download_info(["A", "B", "C"])
    assert info["sheets"] == 3 and info["mb"] == 1050 and info["minutes"] >= 3
    assert prefetch.big_download_info(["A"]) is None


def test_busy_server_downloads_outside_queue_then_queues(worker, monkeypatch):
    from app import prefetch

    monkeypatch.setattr(prefetch, "job_missing_sheets", lambda opts: list((opts or {}).get("sm5_sheets") or []))
    worker._running = "other"
    big = _job("big", ["A", "B"])
    small = _job("small", [])
    worker.enqueue(big)
    worker.enqueue(small)
    assert worker.started == [("_run_prefetch", big)]
    assert db.get_job(big)["phase"] == "downloading"
    assert worker._queue == [small]  # krátký job čeká jen na výpočet, ne na stahování
    assert worker.queue_position(big) is None and big in worker.queue_snapshot()["prefetching"]

    class DoneProc:
        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: DoneProc())
    worker._run_prefetch(big)
    assert worker._queue == [small, big] and db.get_job(big)["phase"] == "waiting"
    assert not worker._prefetching


def test_idle_server_starts_big_job_directly(worker, monkeypatch):
    from app import prefetch

    monkeypatch.setattr(prefetch, "job_missing_sheets", lambda opts: ["A", "B"])
    big = _job("big", ["A", "B"])
    worker.enqueue(big)
    assert worker.started == [("_run", big)] and worker._running == big


def test_free_server_takes_job_from_download(worker, monkeypatch):
    from app import prefetch

    monkeypatch.setattr(prefetch, "job_missing_sheets", lambda opts: ["A", "B"])
    worker._running = "other"
    big = _job("big", ["A", "B"])
    worker.enqueue(big)
    worker._running = None
    worker._start_next()  # fronta výpočtů prázdná: nečekat na konec předstažení
    assert worker._running == big and ("_run", big) in worker.started
    # Předstažení pak doběhne a job znovu nezařadí.
    db.update_job(big, status="running")
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: type("P", (), {"wait": lambda s, timeout=None: 0, "poll": lambda s: 0})())
    worker._run_prefetch(big)
    assert worker._queue == []


def test_file_lock_reentrant_and_exclusive(tmp_path):
    from app.slot_lock import file_lock

    path = tmp_path / ".x.lock"
    order: list[str] = []
    with file_lock(path):
        with file_lock(path):  # vnořeně ve stejném vlákně nečeká na sebe
            pass

        def other():
            with file_lock(path, poll_s=0.01):
                order.append("other")

        t = threading.Thread(target=other)
        t.start()
        time.sleep(0.1)
        order.append("first")
    t.join(2)
    assert order == ["first", "other"]
