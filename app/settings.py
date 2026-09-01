from __future__ import annotations

import os
from pathlib import Path

from app.tool_env import apply_local_gis_env, resolve_pullauta

APP_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = APP_ROOT / "configs"

apply_local_gis_env()

if os.environ.get("PODKLADARNA_DATA"):
    DATA_ROOT = Path(os.environ["PODKLADARNA_DATA"])
elif os.name == "nt":
    DATA_ROOT = APP_ROOT / "data"
else:
    DATA_ROOT = Path("/data")
JOBS_DIR = DATA_ROOT / "jobs"
CACHE_DIR = DATA_ROOT / "cache"
DOWNLOADS_DIR = CACHE_DIR
DB_PATH = DATA_ROOT / "podkladarna.db"

PULLAUTA_BIN = resolve_pullauta()
MAX_CONCURRENT_LIDAR = int(os.environ.get("MAX_CONCURRENT_LIDAR", "1"))
MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", "10"))
JOB_RETENTION_HOURS = int(os.environ.get("JOB_RETENTION_HOURS", "48"))
JOB_RETENTION_DAYS = int(os.environ.get("JOB_RETENTION_DAYS", "0"))  # legacy; použijte JOB_RETENTION_HOURS
MAX_ACTIVE_JOBS_PER_IP = int(os.environ.get("MAX_ACTIVE_JOBS_PER_IP", "2"))
MAX_JOBS_PER_IP_HOUR = int(os.environ.get("MAX_JOBS_PER_IP_HOUR", "10"))
JOB_TIMEOUT_MINUTES = int(os.environ.get("JOB_TIMEOUT_MINUTES", "90"))
if os.environ.get("JOB_TIMEOUT_SECONDS"):
    JOB_TIMEOUT_SECONDS = max(1, int(os.environ["JOB_TIMEOUT_SECONDS"]))
else:
    JOB_TIMEOUT_SECONDS = max(60, JOB_TIMEOUT_MINUTES * 60)
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "1").lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def _parse_ip_list(raw: str) -> frozenset[str]:
    return frozenset(p.strip() for p in (raw or "").split(",") if p.strip())


RATE_LIMIT_EXEMPT_IPS = _parse_ip_list(os.environ.get("RATE_LIMIT_EXEMPT_IPS", ""))
TEMP_RETENTION_DAYS = int(os.environ.get("TEMP_RETENTION_DAYS", "7"))
CLEANUP_INTERVAL_HOURS = int(os.environ.get("CLEANUP_INTERVAL_HOURS", "24"))
LIDAR_CACHE_MAX_AGE_DAYS = int(os.environ.get("LIDAR_CACHE_MAX_AGE_DAYS", "180"))
ZABAGED_CACHE_MAX_AGE_DAYS = int(os.environ.get("ZABAGED_CACHE_MAX_AGE_DAYS", "30"))

DEFAULT_OPTIONS = {
    "run_vectors": True,
    "output_png": True,
    "output_dxf": True,
    "output_zabaged_clean": False,
    "savetempfolders": False,  # budoucí expert režim / API iterace
}
