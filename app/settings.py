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
DB_PATH = DATA_ROOT / "podkladarna.db"

PULLAUTA_BIN = resolve_pullauta()
MAX_CONCURRENT_LIDAR = int(os.environ.get("MAX_CONCURRENT_LIDAR", "1"))
MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", "10"))
JOB_RETENTION_DAYS = int(os.environ.get("JOB_RETENTION_DAYS", "30"))
TEMP_RETENTION_DAYS = int(os.environ.get("TEMP_RETENTION_DAYS", "7"))
CLEANUP_INTERVAL_HOURS = int(os.environ.get("CLEANUP_INTERVAL_HOURS", "24"))

DEFAULT_OPTIONS = {
    "run_vectors": True,
    "output_png": True,
    "output_dxf": True,
    "output_zabaged_clean": True,
    "savetempfolders": True,
}
