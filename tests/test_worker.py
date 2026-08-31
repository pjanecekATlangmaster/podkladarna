from __future__ import annotations


def test_enqueue_starts_worker(monkeypatch, data_dir):
    import app.db as db
    import app.worker as worker

    db.init_db()
    db.create_job("test", "sprint_2m", {})
    started: list[str] = []

    def fake_run(job_id: str) -> None:
        started.append(job_id)

    monkeypatch.setattr(worker, "_run", fake_run)
    worker._running = None
    worker._queue.clear()

    worker.enqueue("abc123")

    assert started == ["abc123"]
    assert worker._running == "abc123"


def test_enqueue_queues_when_busy(data_dir):
    import app.db as db
    import app.worker as worker

    db.init_db()
    db.create_job("busy1", "sprint_2m", {})
    db.create_job("job2", "sprint_2m", {})
    worker._running = "busy1"
    worker._queue.clear()

    worker.enqueue("job2")

    assert "job2" in worker._queue
    assert worker._running == "busy1"

    worker._running = None
    worker._queue.clear()
