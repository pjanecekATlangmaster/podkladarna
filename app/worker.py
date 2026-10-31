from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

from app import db, settings
from app.rate_limit import queue_priority_key
from app.settings import (
    APP_ROOT,
    JOB_TIMEOUT_MINUTES,
    JOB_TIMEOUT_SECONDS,
    JOBS_DIR,
    MAX_CONCURRENT_LIDAR,
    MAX_QUEUE_SIZE,
)
from app.slot_lock import SharedSlot, shared_slot_from_env

_lock = threading.Lock()
_running: str | None = None
_queue: list[str] = []
# Sdílený slot mezi instancemi A/B (None = vypnuto); drží ho právě běžící job.
_slot: SharedSlot | None = None
_slot_dir_cache: object = object()


def _shared_slot() -> SharedSlot | None:
    """Slot podle ``settings.SHARED_SLOTS_DIR`` (znovu vytvořen, když se nastavení změní)."""
    global _slot, _slot_dir_cache
    directory = settings.SHARED_SLOTS_DIR
    if _slot_dir_cache != directory:
        _slot = shared_slot_from_env(directory, owner=settings.INSTANCE_NAME)
        _slot_dir_cache = directory
    return _slot


def shared_busy() -> bool:
    """True, když těžký job právě počítá druhá instance (detail se nezveřejňuje)."""
    slot = _shared_slot()
    if slot is None:
        return False
    try:
        return slot.held_by_other()
    except OSError:
        return False


def is_busy() -> bool:
    return _running is not None


# Nasazení (update-nas-ab.sh) před restartem kontejneru založí v DATA_ROOT tento soubor:
# běžící job doběhne, nové joby se přijímají jen do fronty (stav queued přežije restart
# a po startu nové verze se spustí). Zapomenutý soubor po čase přestane platit.
DRAIN_FILE_NAME = ".deploy-drain"
_DRAIN_POLL_S = 15.0


def drain_file():
    return settings.DATA_ROOT / DRAIN_FILE_NAME


def drain_max_age_s() -> float:
    return (2 * JOB_TIMEOUT_MINUTES + 60) * 60


def is_draining() -> bool:
    """True, když čeká nasazení nové verze: nový job se nespouští, jen řadí do fronty."""
    try:
        mtime = drain_file().stat().st_mtime
    except OSError:
        return False
    return time.time() - mtime < drain_max_age_s()


def queue_snapshot() -> dict:
    with _lock:
        return {
            "busy": _running is not None,
            "draining": is_draining(),
            "shared_busy": shared_busy(),
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
        "Přerušeno restartem serveru (uvicorn reload / pád procesu) – "
        "spusťte job znovu. Pro smoke bez reloadu: .\\scripts\\dev.ps1 run"
    )


def requeue_after_restart() -> list[str]:
    """Vrátí do fronty joby, které v DB čekaly ve stavu queued (fronta v paměti restart nepřežije)."""
    ids = db.list_queued_job_ids()
    for job_id in ids:
        with _lock:
            if job_id == _running or job_id in _queue:
                continue
        enqueue(job_id, force=True)
    return ids


def enqueue(job_id: str, *, force: bool = False) -> None:
    global _running
    with _lock:
        if not force and len(_queue) >= MAX_QUEUE_SIZE and (_running is not None or is_draining()):
            raise RuntimeError("Fronta je plná")
        if _running is not None or is_draining():
            if job_id not in _queue:
                if not force and len(_queue) >= MAX_QUEUE_SIZE:
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
    if is_draining():
        return
    with _lock:
        if _queue and _running is None:
            next_id = _pop_next_queued()
            _running = next_id
    if next_id:
        db.update_job(next_id, status="running", phase="starting", started_at=db._utcnow())
        threading.Thread(target=_run, args=(next_id,), daemon=True).start()


def _return_to_queue(job_id: str) -> None:
    """Job ještě nezačal počítat a čeká se na nasazení: zpět na začátek fronty."""
    global _running
    with _lock:
        if _running == job_id:
            _running = None
        if job_id not in _queue:
            _queue.insert(0, job_id)
    db.update_job(job_id, status="queued", phase="waiting")


def _drain_watch() -> None:
    """Po skončení nasazení (soubor zmizel / přestal platit) spustí čekající joby."""
    while True:
        time.sleep(_DRAIN_POLL_S)
        try:
            if _running is None and _queue and not is_draining():
                _start_next()
        except Exception:  # noqa: BLE001 – hlídač nesmí spadnout
            pass


def start_drain_watch() -> None:
    threading.Thread(target=_drain_watch, daemon=True, name="drain-watch").start()


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
    slot = _shared_slot()

    try:
        if not (JOBS_DIR / job_id).is_dir():
            raise FileNotFoundError(f"Chybí složka jobu: {job_id}")

        if is_draining():
            _return_to_queue(job_id)
            return

        if slot is not None:
            # Těžký job smí počítat jen jeden napříč instancemi A/B. Druhá instance se
            # o něm nedozví nic než „vytíženo“; zde jen čekáme, až se slot uvolní.
            def _mark_waiting() -> None:
                db.update_job(job_id, status="queued", phase="waiting_server")

            if not slot.acquire(
                timeout_s=settings.SHARED_SLOT_WAIT_MINUTES * 60,
                on_wait=_mark_waiting,
                abort=is_draining,
            ):
                if is_draining():
                    _return_to_queue(job_id)
                    return
                raise TimeoutError(
                    f"Server byl obsazený déle než {settings.SHARED_SLOT_WAIT_MINUTES} min – "
                    "spusťte job znovu."
                )
            if is_draining():
                _return_to_queue(job_id)
                return
            db.update_job(job_id, status="running", phase="starting", started_at=db._utcnow())

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
        if slot is not None:
            slot.release()
        with _lock:
            if _running == job_id:
                _running = None
        _start_next()
