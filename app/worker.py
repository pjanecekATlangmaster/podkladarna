from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading

from app import db
from app.rate_limit import queue_priority_key
from app.settings import (
    APP_ROOT,
    JOB_TIMEOUT_MINUTES,
    JOB_TIMEOUT_SECONDS,
    JOBS_DIR,
    MAX_CONCURRENT_LIDAR,
    MAX_QUEUE_SIZE,
)

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
            "job_timeout_minutes": JOB_TIMEOUT_MINUTES,
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


def recover_after_restart() -> list[str]:
    """Uvolní joby zůstávající ve stavu running po pádu/restartu procesu."""
    return db.mark_interrupted_running_jobs(
        "Přerušeno restartem serveru – spusťte job znovu."
    )


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


def _pop_next_queued() -> str | None:
    if not _queue:
        return None
    best = min(_queue, key=queue_priority_key)
    _queue.remove(best)
    return best


def _start_next() -> None:
    global _running
    next_id: str | None = None
    with _lock:
        if _queue:
            next_id = _pop_next_queued()
            _running = next_id
    if next_id:
        db.update_job(next_id, status="running", phase="starting", started_at=db._utcnow())
        threading.Thread(target=_run, args=(next_id,), daemon=True).start()


def _job_worker_cmd(job_id: str) -> list[str]:
    return [sys.executable, "-m", "app.job_worker", job_id]


def _popen_job(job_id: str) -> subprocess.Popen:
    kwargs: dict = {
        "cwd": str(APP_ROOT),
        "env": os.environ.copy(),
    }
    if os.name == "nt":
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(_job_worker_cmd(job_id), **kwargs)


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        proc.terminate()
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
        proc.wait(timeout=10)


def _run(job_id: str) -> None:
    global _running
    timed_out = False
    proc: subprocess.Popen | None = None

    try:
        if not (JOBS_DIR / job_id).is_dir():
            raise FileNotFoundError(f"Chybí složka jobu: {job_id}")

        proc = _popen_job(job_id)
        try:
            proc.wait(timeout=JOB_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_tree(proc)
            msg = (
                f"Job překročil časový limit {JOB_TIMEOUT_MINUTES} min – "
                "ukončeno, fronta pokračuje."
            )
            db.append_log(job_id, f"CHYBA: {msg}")
            job = db.get_job(job_id)
            if job["status"] == "running":
                db.update_job(job_id, status="failed", phase="error", error=msg)
            return

        if proc.returncode != 0:
            job = db.get_job(job_id)
            if job["status"] == "running":
                db.update_job(
                    job_id,
                    status="failed",
                    phase="error",
                    error=f"Pipeline skončila s kódem {proc.returncode}",
                )
    except Exception as exc:
        db.append_log(job_id, f"CHYBA worker: {exc}")
        job = db.get_job(job_id)
        if job["status"] == "running":
            db.update_job(job_id, status="failed", phase="error", error=str(exc))
        if proc is not None and proc.poll() is None:
            _terminate_process_tree(proc)
    finally:
        if proc is not None and proc.poll() is None and not timed_out:
            _terminate_process_tree(proc)
        with _lock:
            if _running == job_id:
                _running = None
        _start_next()
