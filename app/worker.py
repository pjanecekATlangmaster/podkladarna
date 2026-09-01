from __future__ import annotations

import threading
import traceback

from app import db
from app.pipeline.run_job import run_job_pipeline
from app.settings import JOBS_DIR, MAX_CONCURRENT_LIDAR, MAX_QUEUE_SIZE

_lock = threading.Lock()
_running: str | None = None
_queue: list[str] = []


def is_busy() -> bool:
    return _running is not None


def current_job_id() -> str | None:
    return _running


def queue_size() -> int:
    with _lock:
        return len(_queue)


def queue_snapshot() -> dict:
    with _lock:
        return {
            "busy": _running is not None,
            "current": _running,
            "queued": list(_queue),
            "queue_size": len(_queue),
            "max_concurrent": MAX_CONCURRENT_LIDAR,
            "max_queue_size": MAX_QUEUE_SIZE,
        }


def queue_position(job_id: str) -> int | None:
    """0 = právě běží, 1+ = pozice ve frontě, None = není ve frontě."""
    with _lock:
        if _running == job_id:
            return 0
        if job_id in _queue:
            return _queue.index(job_id) + 1
    return None


def can_accept_job() -> bool:
    with _lock:
        return len(_queue) < MAX_QUEUE_SIZE


def enqueue(job_id: str) -> None:
    global _running
    with _lock:
        if len(_queue) >= MAX_QUEUE_SIZE and _running is not None:
            raise RuntimeError("Fronta je plná")
        if _running is not None:
            if job_id not in _queue:
                if len(_queue) >= MAX_QUEUE_SIZE:
                    raise RuntimeError("Fronta je plná")
                _queue.append(job_id)
            db.update_job(job_id, status="queued", phase="waiting")
            return
        _running = job_id
    db.update_job(job_id, status="running", phase="starting", started_at=db._utcnow())
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()


def _start_next() -> None:
    global _running
    next_id: str | None = None
    with _lock:
        if _queue:
            next_id = _queue.pop(0)
            _running = next_id
    if next_id:
        db.update_job(next_id, status="running", phase="starting", started_at=db._utcnow())
        threading.Thread(target=_run, args=(next_id,), daemon=True).start()


def _run(job_id: str) -> None:
    global _running
    job_dir = JOBS_DIR / job_id

    def log(msg: str) -> None:
        db.append_log(job_id, msg)

    try:
        job = db.get_job(job_id)
        db.update_job(job_id, status="running", phase="prepare", error=None)
        log(f"Start job {job_id} preset={job['preset_id']}")

        run_job_pipeline(
            job_dir,
            job["preset_id"],
            job["options"],
            log,
            job_name=job["name"],
        )
        db.update_job(job_id, status="done", phase="done")
        log("Status: done")
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"CHYBA: {exc}")
        log(tb)
        db.update_job(job_id, status="failed", phase="error", error=str(exc))
    finally:
        with _lock:
            if _running == job_id:
                _running = None
        _start_next()
