from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time

from app import db, disk_space, settings
from app.pipeline.issue_explain import failure_summary
from app.pipeline.zip_texts_en import zip_lang
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

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_running: str | None = None
_queue: list[str] = []
# Sdílený slot mezi instancemi A/B (None = vypnuto); drží ho právě běžící job.
_slot: SharedSlot | None = None
_slot_dir_cache: object = object()
# Předstažení velkých dat mimo frontu výpočtů (app/prefetch.py): čekající, právě stahující (proces)
# a hotové (ty už znovu neodbočí). Do fronty výpočtů se job zařadí až po stažení.
_prefetch_queue: list[str] = []
_prefetching: dict[str, subprocess.Popen | None] = {}
_prefetch_done: set[str] = set()
# Joby, které si volný server vzal ze stahování předem (proces ještě běží): znovu se nevezmou.
_prefetch_taken: set[str] = set()


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
            "prefetching": list(_prefetching),
            "prefetch_queued": list(_prefetch_queue),
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


def _waiting_count() -> int:
    """Čekající joby (fronta výpočtů + předstažení); volat pod ``_lock``."""
    return len(_queue) + len(_prefetch_queue) + len(_prefetching)


def can_accept_job() -> bool:
    with _lock:
        return _waiting_count() < MAX_QUEUE_SIZE


def _notify_failure_async(job_ids: list[str]) -> None:
    """E-mail o chybě privátních jobů v pozadí (SMTP nesmí zdržet frontu ani start).

    notify_private_failure sama ověří, že job je privátní a ve stavu failed,
    a pošle nejvýš jeden e-mail na job.
    """
    if not job_ids:
        return

    def _send() -> None:
        from app.job_worker import notify_private_failure

        for job_id in job_ids:
            notify_private_failure(job_id)

    threading.Thread(target=_send, name="private-fail-mail", daemon=True).start()


def recover_after_restart() -> list[str]:
    """Uvolní joby zůstávající ve stavu running po pádu/restartu procesu."""
    reason = (
        "Přerušeno restartem serveru (uvicorn reload / pád procesu) – "
        "spusťte job znovu. Pro smoke bez reloadu: .\\scripts\\dev.ps1 run"
    )
    ids = db.mark_interrupted_running_jobs(
        reason, user_message=failure_summary(reason)
    )
    # Karta jobu v jazyce zadání (mezinárodní verze anglicky).
    for job_id in ids:
        try:
            lang = zip_lang(db.get_job(job_id).get("options"))
            if lang != "cs":
                db.update_job(job_id, error=failure_summary(reason, lang))
        except Exception:
            logger.exception("Překlad hlášky přerušeného jobu %s", job_id)
    _notify_failure_async(ids)
    return ids


def requeue_after_restart() -> list[str]:
    """Vrátí do fronty joby, které v DB čekaly ve stavu queued (fronta v paměti restart nepřežije)."""
    ids = db.list_queued_job_ids()
    for job_id in ids:
        with _lock:
            if job_id == _running or job_id in _queue:
                continue
        enqueue(job_id, force=True)
    return ids


def _needs_prefetch(job_id: str) -> bool:
    """Job s velkým stahováním (listy LiDARu mimo cache), který ještě nestahoval předem."""
    if job_id in _prefetch_done or not settings.PREFETCH_ENABLED:
        return False
    try:
        from app import prefetch

        return bool(prefetch.job_missing_sheets(db.get_job(job_id).get("options")))
    except Exception:  # noqa: BLE001 – předstažení je jen optimalizace, job jde do fronty jako dřív
        logger.exception("Posouzení předstažení jobu %s", job_id)
        return False


def enqueue(job_id: str, *, force: bool = False) -> None:
    global _running
    prefetch = _needs_prefetch(job_id)
    with _lock:
        busy = _running is not None or bool(_queue) or is_draining()
        if prefetch and busy and job_id not in _queue and job_id not in _prefetching:
            # Server počítá jiný job: stahovat mimo frontu výpočtů, do ní až po stažení.
            if job_id not in _prefetch_queue:
                if not force and _waiting_count() >= MAX_QUEUE_SIZE:
                    raise RuntimeError("Fronta je plná")
                _prefetch_queue.append(job_id)
            db.update_job(job_id, status="queued", phase="waiting_download")
            diverted = True
        else:
            diverted = False
    if diverted:
        db.append_log(job_id, "Velké stahování: data se stáhnou s nižší prioritou mimo frontu výpočtů, pak se job zařadí.")
        _start_prefetches()
        return
    with _lock:
        if not force and _waiting_count() >= MAX_QUEUE_SIZE and (_running is not None or is_draining()):
            raise RuntimeError("Fronta je plná")
        if _running is not None or is_draining():
            if job_id not in _queue:
                if not force and _waiting_count() >= MAX_QUEUE_SIZE:
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


def _pop_prefetching_for_idle() -> str | None:
    """Fronta výpočtů je prázdná a server volný: job ze stahování předem začne hned (build si zbytek
    stáhne sám, zámek listu brání dvojímu stažení). Přednost má job, který už stahuje."""
    for job_id in list(_prefetching):
        if job_id in _prefetch_taken:
            continue  # už jednou převzatý (běží nebo doběhl / selhal) – ne znovu (review PR 354)
        _prefetch_taken.add(job_id)
        _prefetch_done.add(job_id)
        return job_id
    if _prefetch_queue:
        job_id = _prefetch_queue.pop(0)
        _prefetch_done.add(job_id)
        return job_id
    return None


def _start_next() -> None:
    global _running
    next_id: str | None = None
    if is_draining():
        return
    with _lock:
        if _running is None:
            next_id = _pop_next_queued() if _queue else _pop_prefetching_for_idle()
            _running = next_id
    if next_id:
        db.update_job(next_id, status="running", phase="starting", started_at=db._utcnow())
        threading.Thread(target=_run, args=(next_id,), daemon=True).start()


def _record_failed_usage(job_id: str) -> None:
    """Job zabitý timeoutem / pádem procesu: započítat do statistiky hned (celá doba běhu)."""
    try:
        db.record_job_usage(job_id, "failed")
    except Exception:
        pass


# Pod tolik volného místa se pád jobu bere jako plný disk (hláška „Na serveru došlo místo na disku“).
_DISK_FULL_FREE_BYTES = 64 * 1024 * 1024


def _fail_running_job(job_id: str, msg: str) -> None:
    """Job, který je v DB pořád „running“, označí jako selhaný; stav se zapisuje dřív než log.

    Na plném disku (ostrá A 2026-11-15) selhával už zápis logu a stav zůstal „běží“.
    """
    try:
        job = db.get_job(job_id)
    except Exception:
        logger.exception("Načtení jobu %s selhalo", job_id)
        return
    if job.get("status") != "running":
        return
    free = disk_space.free_bytes()
    if free is not None and free < _DISK_FULL_FREE_BYTES:
        msg = f"{disk_space.DISK_FULL_MESSAGE} ({msg})"
    try:
        disk_space.write_with_reserve(
            db.update_job,
            job_id,
            status="failed",
            phase="error",
            error=failure_summary(msg, zip_lang(job.get("options"))),
        )
    except Exception:
        logger.exception("Zápis stavu failed jobu %s selhal", job_id)
    db.append_log(job_id, f"CHYBA: {msg}")
    _record_failed_usage(job_id)


def _return_to_queue(job_id: str) -> None:
    """Job ještě nezačal počítat a čeká se na nasazení: zpět na začátek fronty."""
    global _running
    with _lock:
        if _running == job_id:
            _running = None
        if job_id not in _queue:
            _queue.insert(0, job_id)
    db.update_job(job_id, status="queued", phase="waiting")


def _start_prefetches() -> None:
    """Spustí předstažení čekajících jobů do limitu ``PREFETCH_CONCURRENCY`` (ne během nasazení)."""
    if is_draining():
        return
    started: list[str] = []
    with _lock:
        while _prefetch_queue and len(_prefetching) < settings.PREFETCH_CONCURRENCY:
            job_id = _prefetch_queue.pop(0)
            _prefetching[job_id] = None
            started.append(job_id)
    for job_id in started:
        db.update_job(job_id, status="queued", phase="downloading")
        threading.Thread(target=_run_prefetch, args=(job_id,), daemon=True, name="prefetch").start()


def _prefetch_cmd(job_id: str) -> list[str]:
    return [sys.executable, "-m", "app.prefetch", job_id]


def _run_prefetch(job_id: str) -> None:
    """Proces předstažení jednoho jobu; po něm (i po chybě) job jde do fronty výpočtů."""
    proc: subprocess.Popen | None = None
    try:
        kwargs: dict = {"cwd": str(APP_ROOT)}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(_prefetch_cmd(job_id), **kwargs)
        with _lock:
            if job_id in _prefetching:
                _prefetching[job_id] = proc
        try:
            proc.wait(timeout=settings.PREFETCH_TIMEOUT_MINUTES * 60)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(proc)
            db.append_log(
                job_id,
                f"Předstažení dat: ⚠ nedoběhlo do {settings.PREFETCH_TIMEOUT_MINUTES} min – zbytek stáhne výpočet",
            )
    except Exception as exc:  # noqa: BLE001 – selhání stahování job nezastaví
        logger.exception("Předstažení jobu %s", job_id)
        db.append_log(job_id, f"Předstažení dat: ⚠ selhalo ({exc}) – data stáhne výpočet")
    finally:
        if proc is not None and proc.poll() is None:
            _terminate_process_tree(proc)
        with _lock:
            _prefetching.pop(job_id, None)
            taken = job_id in _prefetch_taken
            _prefetch_taken.discard(job_id)
            if taken:
                _prefetch_done.discard(job_id)  # job už běžel / běží sám, značka není potřeba
            else:
                _prefetch_done.add(job_id)
            waiting = not taken and job_id != _running and job_id not in _queue
        if waiting:
            try:
                status = db.get_job(job_id).get("status")
            except KeyError:
                status = None
            if status == "queued":
                try:
                    enqueue(job_id, force=True)
                except Exception:  # noqa: BLE001
                    logger.exception("Zařazení jobu %s po předstažení", job_id)
            else:
                with _lock:
                    _prefetch_done.discard(job_id)  # smazaný / zrušený job: značka by jen rostla
        _start_prefetches()


def _drain_watch() -> None:
    """Po skončení nasazení (soubor zmizel / přestal platit) spustí čekající joby."""
    while True:
        time.sleep(_DRAIN_POLL_S)
        try:
            if is_draining():
                continue
            _start_prefetches()
            if _running is None and (_queue or _prefetch_queue or _prefetching):
                _start_next()
        except Exception:  # noqa: BLE001 – hlídač nesmí spadnout
            pass


def start_drain_watch() -> None:
    threading.Thread(target=_drain_watch, daemon=True, name="drain-watch").start()


def _job_worker_cmd(job_id: str) -> list[str]:
    return [sys.executable, "-m", "app.job_worker", job_id]


def _job_env(job_id: str) -> dict:
    """Prostředí procesu jobu: souřadnice země výřezu (``crs_5514.JOB_CRS_ENV``, zahraniční build)."""
    from app.pipeline.crs_5514 import JOB_CRS_ENV, KROVAK_LABEL

    env = os.environ.copy()
    env.pop(JOB_CRS_ENV, None)
    try:
        from app.pipeline.country_profile import job_country

        country = job_country(db.get_job(job_id).get("options") or {})
        crs = (country.crs if country else "") or KROVAK_LABEL
    except Exception:  # noqa: BLE001 – job bez záznamu / starý formát: S-JTSK jako dosud
        crs = KROVAK_LABEL
    if crs != KROVAK_LABEL:
        env[JOB_CRS_ENV] = crs
    return env


def _popen_job(job_id: str) -> subprocess.Popen:
    kwargs: dict = {
        "cwd": str(APP_ROOT),
        "env": _job_env(job_id),
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

    with _lock:
        _prefetch_done.discard(job_id)
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
            _fail_running_job(job_id, msg)
            return

        if proc.returncode != 0:
            _fail_running_job(job_id, f"Pipeline skončila s kódem {proc.returncode}")
    except Exception as exc:
        db.append_log(job_id, f"CHYBA worker: {exc}")
        _fail_running_job(job_id, str(exc))
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
        # Timeout / pád subprocesu: job_worker e-mail o chybě nestihl poslat.
        _notify_failure_async([job_id])
        _start_next()
