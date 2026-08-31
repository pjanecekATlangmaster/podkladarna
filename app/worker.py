from __future__ import annotations

import threading
import traceback

from app import db
from app.pipeline.run_job import run_job_pipeline
from app.settings import JOBS_DIR

_lock = threading.Lock()
_running: str | None = None
_queue: list[str] = []


def is_busy() -> bool:
    return _running is not None


def current_job_id() -> str | None:
    return _running


def queue_size() -> int:
    return len(_queue)


def enqueue(job_id: str) -> None:
    with _lock:
        if _running is not None:
            if job_id not in _queue:
                _queue.append(job_id)
            db.update_job(job_id, status="queued")
            return
        _running = job_id
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()


def _start_next() -> None:
    next_id: str | None = None
    with _lock:
        if _queue:
            next_id = _queue.pop(0)
            _running = next_id
    if next_id:
        threading.Thread(target=_run, args=(next_id,), daemon=True).start()


def _run(job_id: str) -> None:
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
            global _running
            if _running == job_id:
                _running = None
        _start_next()
