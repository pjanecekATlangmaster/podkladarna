from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db
from app.i18n import Msg
from app.settings import (
    API_KEY_MAX_ACTIVE_JOBS,
    API_KEY_MAX_JOBS_HOUR,
    BANNED_EMAILS,
    MAX_ACTIVE_JOBS_PER_IP,
    MAX_JOBS_PER_IP_DAY,
    MAX_JOBS_PER_IP_HOUR,
    MAX_JOBS_PER_NET_DAY,
    MAX_PRIVATE_JOBS_PER_EMAIL_DAY,
    RATE_LIMIT_EXEMPT_IPS,
)


def is_exempt(ip: str) -> bool:
    if not ip:
        return False
    return ip in RATE_LIMIT_EXEMPT_IPS


def check_create_job(client_ip: str) -> str | None:
    """Vrátí chybovou zprávu, nebo None pokud je vytvoření povoleno."""
    if is_exempt(client_ip):
        return None
    active = db.count_active_jobs_for_ip(client_ip)
    if active >= MAX_ACTIVE_JOBS_PER_IP:
        return Msg("api.rate.ip_active", active=active, limit=MAX_ACTIVE_JOBS_PER_IP)
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    hourly = db.count_jobs_for_ip_since(client_ip, since)
    if hourly >= MAX_JOBS_PER_IP_HOUR:
        return Msg("api.rate.ip_hourly", limit=MAX_JOBS_PER_IP_HOUR)
    now = datetime.now(timezone.utc)
    ip_times, net_times = db.origin_jobs_since(client_ip, now - DAY_WINDOW)
    for times, limit, key in (
        (ip_times, MAX_JOBS_PER_IP_DAY, "api.rate.ip_daily"),
        (net_times, MAX_JOBS_PER_NET_DAY, "api.rate.net_daily"),
    ):
        if limit > 0 and len(times) >= limit:
            hours, minutes = _wait_until_free(times, limit, now)
            return Msg(key, limit=limit, hours=hours, minutes=minutes)
    return None


DAY_WINDOW = timedelta(hours=24)


def _wait_until_free(times: list[str], limit: int, now: datetime) -> tuple[int, int]:
    """(hodiny, minuty), než z klouzavého 24h okna vypadne tolik jobů, aby šel založit další."""
    try:
        free_at = datetime.fromisoformat(times[len(times) - limit]) + DAY_WINDOW
    except ValueError:
        free_at = now + DAY_WINDOW
    wait_min = max(1, int((free_at - now).total_seconds() // 60) + 1)
    return wait_min // 60, wait_min % 60


EMAIL_WINDOW = DAY_WINDOW


def is_banned_email(email: str) -> bool:
    """E-mail ze seznamu ``BANNED_EMAILS`` (aliasy +značka a tečky Gmailu jsou tentýž e-mail)."""
    value = db.normalize_email_for_limit(email)
    if not value:
        return False
    banned = {db.normalize_email_for_limit(e) for e in BANNED_EMAILS.split(",") if e.strip()}
    return value in banned


def check_banned_email(email: str) -> str | None:
    """Hláška pro zablokovaný e-mail, jinak None (platí i pro výjimky IP a placenou doménu)."""
    return Msg("api.rate.email_banned") if is_banned_email(email) else None


def check_private_email(email: str, client_ip: str = "") -> str | None:
    """Limit soukromých jobů na jeden e-mail za klouzavých 24 h (``MAX_PRIVATE_JOBS_PER_EMAIL_DAY``, 0 = vypnuto).

    Hláška uvede, za jak dlouho půjde další job (až z okna vypadne nejstarší)."""
    limit = MAX_PRIVATE_JOBS_PER_EMAIL_DAY
    if limit <= 0 or not (email or "").strip() or is_exempt(client_ip):
        return None
    now = datetime.now(timezone.utc)
    times = db.private_email_jobs_since(email, now - EMAIL_WINDOW)
    if len(times) < limit:
        return None
    hours, minutes = _wait_until_free(times, limit, now)
    return Msg("api.rate.email_daily", limit=limit, hours=hours, minutes=minutes)


def check_create_api_job() -> str | None:
    """Limit jobů s API klíčem (Stavitel) – místo limitu podle IP, společný pro klíč."""
    active = db.count_active_api_jobs()
    if active >= API_KEY_MAX_ACTIVE_JOBS:
        return Msg("api.rate.key_active", active=active, limit=API_KEY_MAX_ACTIVE_JOBS)
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    hourly = db.count_api_jobs_since(since)
    if hourly >= API_KEY_MAX_JOBS_HOUR:
        return Msg("api.rate.key_hourly", limit=API_KEY_MAX_JOBS_HOUR)
    return None


def queue_priority_key(job_id: str) -> tuple[int, int, str]:
    """Nižší = dřív ve frontě. Férová fronta (3.104.0): přednost má, kdo za posledních 24 h založil
    méně jobů (z IP nebo s e-mailem), pak IP s méně aktivními joby, pak dřívější job."""
    try:
        job = db.get_job(job_id)
    except KeyError:
        return (999, 999, job_id)
    ip = (job.get("options") or {}).get("client_ip") or ""
    if is_exempt(ip):
        return (0, 0, job.get("created_at") or "")
    active = db.count_active_jobs_for_ip(ip)
    return (db.recent_jobs_before(job_id), active, job.get("created_at") or "")
