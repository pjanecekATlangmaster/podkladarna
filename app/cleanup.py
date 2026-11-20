from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

from app import db
from app.settings import JOB_RETENTION_DAYS, JOB_RETENTION_HOURS, JOB_TRIM_AFTER_MINUTES, JOBS_DIR

logger = logging.getLogger("podkladarna.cleanup")

_cleanup_started = False
_last_pageview_purge: float | None = None
_pageview_purge_lock = threading.Lock()
# Při otevření webu / výpisu jobů; ne při každém pollu.
PAGEVIEW_PURGE_MIN_INTERVAL_S = 60.0
# Plánovač mezi denními úklidy kontroluje joby k úklidu mezivýsledků (JOB_TRIM_AFTER_MINUTES) po tolika sekundách.
TRIM_CHECK_INTERVAL_S = 600.0


def trim_finished_jobs(minutes: int | None = None, *, now: datetime | None = None) -> int:
    """Joby, které doběhly (done) před víc než ``minutes`` minutami: smaže mezivýsledky ``work/`` (kromě souhrnů
    a náhledu) a rozbalené kopie v ``output/``; ZIP, náhled a log zůstávají do smazání celého jobu.

    Platí i pro soukromé joby. Běžící / čekající job se nikdy nebere (jen stav done), selhaný si ``work/``
    nechává kvůli diagnóze. Převzetí sloučeného LAZ („Použít nastavení a výřez“) pak nabízené není a nový
    job ořízne LAZ z cache listů. Vrací počet jobů, u kterých se něco smazalo.
    """
    from app.disk_space import trim_job_output, trim_job_work

    minutes = JOB_TRIM_AFTER_MINUTES if minutes is None else minutes
    if minutes <= 0:
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=minutes)
    trimmed = 0
    freed = 0
    for job_id, updated_at in db.list_done_job_times():
        try:
            updated = datetime.fromisoformat(updated_at)
        except ValueError:
            continue
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if updated >= cutoff:
            continue
        job_dir = JOBS_DIR / job_id
        if not job_dir.is_dir():
            continue
        got = trim_job_work(job_dir) + trim_job_output(job_dir)
        if got:
            trimmed += 1
            freed += got
    if trimmed:
        logger.info("Trimmed intermediate files of %s finished job(s), %.1f GB", trimmed, freed / 1e9)
    return trimmed


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
    try:
        from app import orders

        orders.purge_orders()
    except Exception:  # noqa: BLE001 – úklid objednávek nesmí zastavit úklid jobů
        logger.exception("Purge of orders failed")

    for job in db.list_expired_private_jobs(now=now):
        if db.delete_job(job["id"]):
            removed += 1
            logger.info("Purged expired private job %s", job["id"])

    if hours > 0:
        cutoff = now - timedelta(hours=hours)
        # Všechny dokončené joby mimo objednávky, ne jen nejnovějších 500 (revize 2026-11-16):
        # s limitem starší řádky nad limitem v DB zůstaly navždy. Job objednávky řídí objednávka
        # (výše, job_expires_at).
        for job_id, updated_at in db.list_finished_public_job_times():
            try:
                updated = datetime.fromisoformat(updated_at)
            except ValueError:
                continue
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if updated >= cutoff:
                continue
            if db.delete_job(job_id):
                removed += 1
                logger.info("Purged old job %s", job_id)

    _purge_orphan_job_dirs()
    try:
        trim_finished_jobs()
    except Exception:  # noqa: BLE001 – úklid mezivýsledků nesmí zastavit úklid jobů
        logger.exception("Trim of finished jobs failed")
    try:
        from app import user_sources

        n_user = user_sources.prune_staging() + user_sources.prune_finished_jobs()
        if n_user:
            logger.info("Removed %s leftover user upload(s)", n_user)
    except Exception:  # noqa: BLE001 – úklid nahrávek nesmí zastavit úklid jobů
        logger.exception("Cleanup of user uploads failed")
    n_origin = db.purge_job_origin(now=now)
    if n_origin:
        logger.info("Purged %s job origin record(s) past retention", n_origin)
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
    known = db.all_job_ids()  # všechny řádky: s limitem 1000 by se smazala složka staršího jobu
    for path in JOBS_DIR.iterdir():
        if not path.is_dir():
            continue
        if path.name not in known:
            shutil.rmtree(path, ignore_errors=True)
            logger.info("Removed orphan job dir %s", path.name)


def prune_caches() -> int:
    """Úklid cache (prošlá cache podle výřezu, strop LiDARu podle disku) a obnova rezervy; vrací uvolněné bajty."""
    from app import disk_space

    freed = 0
    try:
        freed = disk_space.prune_all_caches(log=logger.info)
    except Exception:  # noqa: BLE001 – úklid cache nesmí shodit server
        logger.exception("Cache cleanup failed")
    disk_space.ensure_reserve()
    return freed


def start_cleanup_scheduler(interval_hours: int = 24) -> None:
    global _cleanup_started
    if _cleanup_started or interval_hours <= 0:
        return
    _cleanup_started = True

    def _loop() -> None:
        interval_s = max(interval_hours, 1) * 3600
        last_purge = time.monotonic()
        while True:
            # Úklid mezivýsledků po JOB_TRIM_AFTER_MINUTES i bez návštěvy webu; celý úklid jednou za interval.
            time.sleep(min(TRIM_CHECK_INTERVAL_S, interval_s))
            try:  # souhrn problémů zdrojů i bez návštěvy webu (jen když je co hlásit)
                from app import source_events

                source_events.maybe_send_digest()
            except Exception:
                logger.exception("Souhrn zdrojů dat selhal")
            if time.monotonic() - last_purge < interval_s:
                try:
                    trim_finished_jobs()
                except Exception:
                    logger.exception("Scheduled trim failed")
                continue
            last_purge = time.monotonic()
            try:
                n = purge_old_jobs()
                if n:
                    logger.info("Scheduled cleanup removed %s job(s)", n)
            except Exception:
                logger.exception("Scheduled cleanup failed")
            prune_caches()
            try:  # deník zdrojů se zkracuje i bez souhrnu (test B má SOURCE_DIGEST_HOURS=0)
                from app import source_events

                source_events.prune()
            except Exception:
                logger.exception("Zkrácení deníku zdrojů selhalo")

    threading.Thread(target=_loop, name="job-cleanup", daemon=True).start()
