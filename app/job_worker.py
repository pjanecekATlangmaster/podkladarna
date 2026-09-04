"""Spustí jeden job v subprocessu (volá worker.py s časovým limitem)."""

from __future__ import annotations

import sys
import traceback

from app import db
from app.pipeline.run_job import run_job_pipeline
from app.proj_env import ensure_proj_data
from app.settings import JOBS_DIR


def run_job(job_id: str) -> int:
    job_dir = JOBS_DIR / job_id
    ensure_proj_data()

    def log(msg: str) -> None:
        db.append_log(job_id, msg)

    try:
        job = db.get_job(job_id)
        db.update_job(job_id, status="running", phase="prepare", error=None)
        log(f"Start job {job_id} preset={job['preset_id']}")

        run_job_pipeline(
            job_dir,
            job["preset_id"],
            job["options"],
            log,
            job_name=job["name"],
        )
        db.update_job(job_id, status="done", phase="done")
        log("Status: done")
        return 0
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"CHYBA: {exc}")
        log(tb)
        db.update_job(job_id, status="failed", phase="error", error=str(exc))
        return 1


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m app.job_worker <job_id>", file=sys.stderr)
        return 2
    return run_job(sys.argv[1])


if __name__ == "__main__":
    sys.exit(main())
