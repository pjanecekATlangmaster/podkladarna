from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import db
from app.settings import JOB_RETENTION_DAYS, JOB_RETENTION_HOURS, JOBS_DIR

logger = logging.getLogger("podkladarna.cleanup")

_cleanup_started = False


def purge_old_jobs(retention_hours: int | None = None) -> int:
    """Smaže hotové/selhané joby starší než retention_hours. Vrací počet smazaných."""
    hours = retention_hours
    if hours is None:
        if JOB_RETENTION_HOURS > 0:
            hours = JOB_RETENTION_HOURS
        elif JOB_RETENTION_DAYS > 0:
            hours = JOB_RETENTION_DAYS * 24
        else:
            return 0
    if hours <= 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    removed = 0

    for job in db.list_jobs(limit=500):
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


def _purge_orphan_job_dirs() -> None:
    """Složky na disku bez záznamu v DB."""
    if not JOBS_DIR.is_dir():
        return
    known = {j["id"] for j in db.list_jobs(limit=1000)}
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
