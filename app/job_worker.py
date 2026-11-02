"""Spustí jeden job v subprocessu (volá worker.py s časovým limitem)."""

from __future__ import annotations

import logging
import os
import sys
import traceback

# numpy načíst v hlavním vlákně dřív, než run_job_pipeline spustí vlákna na pozadí
# (OSM, DTM, podklady). Souběžný první import ve dvou vláknech nechá v sys.modules
# napůl inicializovaný numpy a každý další import v procesu padá
# („cannot load module more than once per process“, job a8b5f9a18917 na testu B).
try:
    import numpy  # noqa: F401
except ImportError:  # pragma: no cover - numpy je v image vždy (GDAL)
    pass

from app import db
from app import settings as app_settings
from app.mail import (
    MailError,
    build_private_failed_email,
    build_private_ready_email,
    send_mail,
)
from app.pipeline import build_stats
from app.pipeline.issue_explain import failure_summary, user_summary
from app.pipeline.preview import resolve_preview_png
from app.pipeline.run_job import run_job_pipeline
from app.proj_env import ensure_proj_data
from app.settings import JOBS_DIR

logger = logging.getLogger("podkladarna.job_worker")

_MAIL_SENT_PREFIX = "E-mail s odkazem odeslán"
_FAIL_MAIL_SENT_PREFIX = "E-mail o chybě odeslán"


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


def _public_base_url() -> str:
    """Čte PUBLIC_BASE_URL živě (env / settings), ať tip redeploy s .env stačí."""
    return (
        os.environ.get("PUBLIC_BASE_URL") or getattr(app_settings, "PUBLIC_BASE_URL", "") or ""
    ).rstrip("/")


def _mail_already_sent(job_id: str, prefix: str = _MAIL_SENT_PREFIX) -> bool:
    return any(prefix in (entry.get("line") or "") for entry in db.get_logs(job_id))


def _notify_private_job(job_id: str) -> None:
    """Po úspěchu pošle e-mail s privátním odkazem (best-effort)."""
    # reveal_artifacts=True jen pro sestavení mailu (flags existence na disku).
    job = db.get_job(job_id, reveal_artifacts=True)
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
    base = _public_base_url()
    if not base:
        msg = (
            "PUBLIC_BASE_URL není nastavené – nelze sestavit odkaz v e-mailu. "
            "Nastavte env / .env (viz DEV.md)."
        )
        db.append_log(job_id, f"CHYBA: {msg}")
        logger.error("%s (job %s)", msg, job_id)
        return
    if _mail_already_sent(job_id):
        return
    url = f"{base}/d/{token}"
    preview_url = None
    georef_url = None
    job_dir = JOBS_DIR / job_id
    preview_path = resolve_preview_png(job_dir / "output", job_dir / "work")
    if job.get("has_preview") or preview_path is not None:
        preview_url = f"{base}/api/jobs/{job_id}/preview.png?token={token}"
    if job.get("has_georef_previews"):
        georef_url = (
            f"{base}/api/jobs/{job_id}/download/georef-previews?token={token}"
        )
    hours = app_settings.PRIVATE_JOB_RETENTION_HOURS
    if hours <= 0:
        hours = 48
    preview_attached = preview_path is not None and preview_path.is_file()
    subject, body = build_private_ready_email(
        job_name=job.get("name") or job_id,
        download_url=url,
        retention_hours=hours,
        preview_url=preview_url,
        georef_url=georef_url,
        preview_attached=preview_attached,
        duration_s=job.get("duration_s"),
        issues_summary=(
            job.get("error") if job.get("status") == db.STATUS_DONE_WITH_ERRORS else None
        ),
    )
    try:
        send_mail(
            email,
            subject,
            body,
            attachments=[preview_path] if preview_attached else None,
        )
        db.append_log(job_id, f"{_MAIL_SENT_PREFIX} na {email}.")
    except MailError as exc:
        db.append_log(job_id, f"CHYBA odeslání e-mailu: {exc}")


def notify_private_failure(job_id: str) -> None:
    """Privátní job skončil chybou: e-mail zadavateli, skrytá kopie FEEDBACK_TO.

    Best-effort, nikdy nevyhazuje. Volá se z job_worker (výjimka v pipeline),
    worker.py (timeout, pád subprocesu) i po restartu serveru.
    """
    try:
        job = db.get_job(job_id)
        if not db.job_is_private(job) or job.get("status") != "failed":
            return
        email = str((job.get("options") or {}).get("notify_email") or "").strip()
        if not email:
            return
        if _mail_already_sent(job_id, _FAIL_MAIL_SENT_PREFIX):
            return
        subject, body = build_private_failed_email(
            job_name=job.get("name") or job_id,
            error=job.get("error"),
            duration_s=job.get("duration_s"),
        )
        try:
            send_mail(email, subject, body, bcc=app_settings.FEEDBACK_TO or None)
            db.append_log(job_id, f"{_FAIL_MAIL_SENT_PREFIX} na {email}.")
        except MailError as exc:
            db.append_log(job_id, f"CHYBA odeslání e-mailu o chybě: {exc}")
    except Exception:
        logger.exception("E-mail o chybě privátního jobu %s selhal", job_id)


def retry_missed_private_mails(*, limit: int = 50) -> list[str]:
    """Po restartu tipu znovu pošle mail u done privátních jobů bez úspěšného odeslání.

    Pokrývá race: job stihne `Status: done`, pak redeploy zabije proces před SMTP,
    nebo chybějící PUBLIC_BASE_URL/SMTP v původním procesu.
    Neposílá odkaz, když na disku už není ZIP (mrtvý token by Petr zmátl).
    """
    resent: list[str] = []
    for job in db.list_jobs(limit=limit, include_private=True):
        if job.get("status") not in db.JOB_DONE_STATUSES:
            continue
        if not db.job_is_private(job):
            continue
        if db.job_is_expired(job):
            continue
        if _mail_already_sent(job["id"]):
            continue
        lines = [e.get("line") or "" for e in db.get_logs(job["id"])]
        if not any("Status: done" in ln for ln in lines):
            continue
        # Privátní API schová has_output – zkontroluj disk přímo.
        job_dir = JOBS_DIR / job["id"]
        if not db._job_has_output(job_dir):
            logger.info(
                "Skip private mail retry for %s – chybí výstupní ZIP na disku.",
                job["id"],
            )
            continue
        logger.info("Retry private mail for job %s", job["id"])
        try:
            _notify_private_job(job["id"])
        except Exception:
            logger.exception("Retry private mail failed for %s", job["id"])
            continue
        if _mail_already_sent(job["id"]):
            resent.append(job["id"])
    return resent


def run_job(job_id: str) -> int:
    job_dir = JOBS_DIR / job_id
    ensure_proj_data()

    def log(msg: str) -> None:
        db.append_log(job_id, msg)

    try:
        job = db.get_job(job_id)
        db.update_job(job_id, status="running", phase="prepare", error=None)
        log("")
        log(f"Start jobu {job_id} · preset {job['preset_id']}")

        issues = run_job_pipeline(
            job_dir,
            job["preset_id"],
            job["options"],
            log,
            job_name=job["name"],
        )
        build_stats.write(job_dir, status="done_with_errors" if issues else "done")
        if issues:
            summary = user_summary(issues)
            log(f"Shrnutí pro uživatele: {summary}")
            db.update_job(
                job_id,
                status=db.STATUS_DONE_WITH_ERRORS,
                phase="done",
                error=summary,
            )
            log(f"Status: {db.STATUS_DONE_WITH_ERRORS}")
        else:
            db.update_job(job_id, status="done", phase="done")
            log("Status: done")
        # Teprve po zápisu done – při pádu/redeploy během SMTP pomůže
        # retry_missed_private_mails() na startupu.
        if db.job_is_private(job):
            _notify_private_job(job_id)
        return 0
    except Exception as exc:
        tb = traceback.format_exc()
        build_stats.write(job_dir, status="failed")
        log(f"CHYBA: {exc}")
        log(tb)
        db.update_job(
            job_id, status="failed", phase="error", error=failure_summary(str(exc))
        )
        notify_private_failure(job_id)
        return 1


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m app.job_worker <job_id>", file=sys.stderr)
        return 2
    return run_job(sys.argv[1])


if __name__ == "__main__":
    sys.exit(main())
