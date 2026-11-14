from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db
from app.i18n import Msg
from app.settings import (
    API_KEY_MAX_ACTIVE_JOBS,
    API_KEY_MAX_JOBS_HOUR,
    MAX_ACTIVE_JOBS_PER_IP,
    MAX_JOBS_PER_IP_HOUR,
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
    return None


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


def queue_priority_key(job_id: str) -> tuple[int, str]:
    """Nižší = dřív ve frontě. Upřednostní IP s méně aktivními joby."""
    try:
        job = db.get_job(job_id)
    except KeyError:
        return (999, job_id)
    ip = (job.get("options") or {}).get("client_ip") or ""
    if is_exempt(ip):
        return (0, job.get("created_at") or "")
    active = db.count_active_jobs_for_ip(ip)
    return (active, job.get("created_at") or "")
