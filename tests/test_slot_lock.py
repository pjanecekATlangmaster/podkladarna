"""Sdílený zámek těžkého jobu mezi instancemi A/B."""

from __future__ import annotations

import importlib
import threading
import time

from app import db
from app.slot_lock import SharedSlot, shared_slot_from_env


def test_slot_exclusive_between_instances_and_released_on_close(tmp_path):
    a = SharedSlot(tmp_path, owner="A")
    b = SharedSlot(tmp_path, owner="B")
    assert a.try_acquire() and a.held
    assert a.try_acquire()  # opakované získání vlastním slotem je v pořádku
    assert not b.try_acquire() and not b.held
    assert b.held_by_other() and not a.held_by_other()
    a.release()
    assert not a.held and not b.held_by_other()
    assert b.try_acquire()
    b.release()


def test_slot_acquire_times_out_then_succeeds_after_release(tmp_path):
    a = SharedSlot(tmp_path)
    b = SharedSlot(tmp_path)
    assert a.try_acquire()
    waited: list[bool] = []
    t = time.monotonic()
    assert b.acquire(poll_s=0.05, timeout_s=0.3, on_wait=lambda: waited.append(True)) is False
    assert time.monotonic() - t >= 0.3 and waited == [True]  # on_wait jen jednou
    threading.Timer(0.2, a.release).start()
    assert b.acquire(poll_s=0.05, timeout_s=3.0) is True
    b.release()


def test_context_manager_and_disabled_without_directory(tmp_path):
    assert shared_slot_from_env(None) is None
    with SharedSlot(tmp_path) as slot:
        assert slot.held
        assert SharedSlot(tmp_path).held_by_other()
    assert not SharedSlot(tmp_path).held_by_other()


def _worker(monkeypatch, tmp_path, data_dir):
    import app.settings as settings

    importlib.reload(settings)
    import app.worker as worker

    importlib.reload(worker)
    monkeypatch.setattr(settings, "SHARED_SLOTS_DIR", tmp_path / "slots")
    monkeypatch.setattr(settings, "SHARED_SLOT_WAIT_MINUTES", 1)
    return worker


def test_job_waits_for_other_instance_then_runs(monkeypatch, tmp_path, data_dir):
    worker = _worker(monkeypatch, tmp_path, data_dir)
    db.init_db()
    job_id = db.create_job("ab", "sprint_2m", {})["id"]
    (data_dir / "jobs" / job_id / "work").mkdir(parents=True, exist_ok=True)

    class FakeProc:
        pid = 1
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    started = threading.Event()

    def fake_popen(_jid):
        started.set()
        return FakeProc()

    monkeypatch.setattr(worker, "_popen_job", fake_popen)
    monkeypatch.setattr(worker, "_terminate_process_tree", lambda _p: None)
    # Slot drží „druhá instance“ (jiný objekt na stejném souboru).
    other = SharedSlot(tmp_path / "slots", owner="B")
    assert other.try_acquire()
    assert worker.queue_snapshot()["shared_busy"] is True

    db.update_job(job_id, status="running", phase="starting")
    worker._running = job_id
    monkeypatch.setattr(worker.settings, "SHARED_SLOTS_DIR", tmp_path / "slots")
    t = threading.Thread(target=worker._run, args=(job_id,), daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and db.get_job(job_id)["phase"] != "waiting_server":
        time.sleep(0.05)
    got = db.get_job(job_id)
    assert got["status"] == "queued" and got["phase"] == "waiting_server"
    assert not started.is_set()  # pipeline se nespustila, dokud slot drží druhá instance

    other.release()
    t.join(15)
    assert started.is_set()
    assert db.get_job(job_id)["status"] != "queued"
    assert worker._running is None
    # slot je po jobu volný pro druhou instanci
    assert other.try_acquire()
    other.release()
