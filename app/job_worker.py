"""Spustí jeden job v subprocessu (volá worker.py s časovým limitem)."""

from __future__ import annotations

import logging
import sys
import traceback

from app import db
from app.mail import MailError, build_private_ready_email, send_mail
from app.pipeline.run_job import run_job_pipeline
from app.proj_env import ensure_proj_data
from app.settings import (
    JOBS_DIR,
    PRIVATE_JOB_RETENTION_HOURS,
    PUBLIC_BASE_URL,
)

logger = logging.getLogger("podkladarna.job_worker")


def _job_token(job_id: str) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT download_token FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if not row:
        return None
    val = row["download_token"]
    return str(val) if val else None


def _notify_private_job(job_id: str) -> None:
    """Po úspěchu pošle e-mail s privátním odkazem (best-effort)."""
    job = db.get_job(job_id)
    if not db.job_is_private(job):
        return
    email = str((job.get("options") or {}).get("notify_email") or "").strip()
    if not email:
        db.append_log(job_id, "Privátní job bez e-mailu – odkaz neodeslán.")
        return
    token = _job_token(job_id)
    if not token:
        db.append_log(job_id, "CHYBA: privátní job bez download tokenu.")
        logger.error("Privátní job %s nemá download_token.", job_id)
        return
    base = (PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        msg = (
            "PUBLIC_BASE_URL není nastavené – nelze sestavit odkaz v e-mailu. "
            "Nastavte env (viz DEV.md)."
        )
        db.append_log(job_id, f"CHYBA: {msg}")
        logger.error("%s (job %s)", msg, job_id)
        return
    url = f"{base}/d/{token}"
    hours = PRIVATE_JOB_RETENTION_HOURS if PRIVATE_JOB_RETENTION_HOURS > 0 else 48
    subject, body = build_private_ready_email(
        job_name=job.get("name") or job_id,
        download_url=url,
        retention_hours=hours,
    )
    try:
        send_mail(email, subject, body)
        db.append_log(job_id, f"E-mail s odkazem odeslán na {email}.")
    except MailError as exc:
        db.append_log(job_id, f"CHYBA odeslání e-mailu: {exc}")


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
        if db.job_is_private(job):
            _notify_private_job(job_id)
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
