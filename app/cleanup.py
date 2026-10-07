from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

from app import db
from app.settings import JOB_RETENTION_DAYS, JOB_RETENTION_HOURS, JOBS_DIR

logger = logging.getLogger("podkladarna.cleanup")

_cleanup_started = False
_last_pageview_purge: float | None = None
_pageview_purge_lock = threading.Lock()
# Při otevření webu / výpisu jobů; ne při každém pollu.
PAGEVIEW_PURGE_MIN_INTERVAL_S = 60.0


def purge_old_jobs(retention_hours: int | None = None) -> int:
    """Smaže hotové/selhané joby starší než retention_hours + prošlé privátní.

    Vrací počet smazaných.
    """
    hours = retention_hours
    if hours is None:
        if JOB_RETENTION_HOURS > 0:
            hours = JOB_RETENTION_HOURS
        elif JOB_RETENTION_DAYS > 0:
            hours = JOB_RETENTION_DAYS * 24
        else:
            hours = 0

    removed = 0
    now = datetime.now(timezone.utc)

    # Privátní: po splatnosti (created_at + PRIVATE_JOB_RETENTION_HOURS),
    # i když ještě nejsou „done“ (pending/failed) – odkaz už neplatí.
    for job in db.list_expired_private_jobs(now=now):
        if db.delete_job(job["id"]):
            removed += 1
            logger.info("Purged expired private job %s", job["id"])

    if hours > 0:
        cutoff = now - timedelta(hours=hours)
        for job in db.list_jobs(limit=500, include_private=True):
            status = job["status"]
            if status in ("running", "queued", "pending"):
                continue
            if status not in ("done", "failed"):
                continue
            try:
                updated = datetime.fromisoformat(job["updated_at"])
            except ValueError:
                continue
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if updated >= cutoff:
                continue
            if db.delete_job(job["id"]):
                removed += 1
                logger.info("Purged old job %s (%s)", job["id"], status)

    _purge_orphan_job_dirs()
    return removed


def maybe_purge_old_jobs(
    *,
    min_interval_s: float = PAGEVIEW_PURGE_MIN_INTERVAL_S,
) -> int:
    """Stejné jako purge_old_jobs, ale nejvýš jednou za ``min_interval_s`` (pageview)."""
    global _last_pageview_purge
    now = time.monotonic()
    with _pageview_purge_lock:
        # None = ještě neběželo (ne 0.0 – monotonic na CI často začíná u nuly).
        if (
            _last_pageview_purge is not None
            and now - _last_pageview_purge < min_interval_s
        ):
            return 0
        _last_pageview_purge = now
    return purge_old_jobs()


def _purge_orphan_job_dirs() -> None:
    """Složky na disku bez záznamu v DB."""
    if not JOBS_DIR.is_dir():
        return
    known = {j["id"] for j in db.list_jobs(limit=1000, include_private=True)}
    for path in JOBS_DIR.iterdir():
        if not path.is_dir():
            continue
        if path.name not in known:
            shutil.rmtree(path, ignore_errors=True)
            logger.info("Removed orphan job dir %s", path.name)


def start_cleanup_scheduler(interval_hours: int = 24) -> None:
    global _cleanup_started
    if _cleanup_started or interval_hours <= 0:
        return
    _cleanup_started = True

    def _loop() -> None:
        while True:
            time.sleep(max(interval_hours, 1) * 3600)
            try:
                n = purge_old_jobs()
                if n:
                    logger.info("Scheduled cleanup removed %s job(s)", n)
            except Exception:
                logger.exception("Scheduled cleanup failed")

    threading.Thread(target=_loop, name="job-cleanup", daemon=True).start()
