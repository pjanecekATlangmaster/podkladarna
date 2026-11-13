"""Spustí jeden job v subprocessu (volá worker.py s časovým limitem)."""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
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
from app.pipeline import build_stats, dtm_summary
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
        if not (job.get("options") or {}).get("api_client"):
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
        area_km2=job.get("area_km2"),
        issues_summary=(
            job.get("error") if job.get("status") == db.STATUS_DONE_WITH_ERRORS else None
        ),
        notices=dtm_summary.notice_lines(job.get("dtm_summary")),
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


# Kopie provozovateli: kolik posledních záznamů logu a znaků přiložit.
_OPERATOR_LOG_ENTRIES = 25
_OPERATOR_LOG_MAX_CHARS = 8000


def job_page_url(job_id: str) -> str | None:
    """Odkaz na stránku jobu (detail + log), bez tokenu ke stažení. None bez PUBLIC_BASE_URL."""
    base = _public_base_url()
    return f"{base}/?job={job_id}" if base else None


def _operator_failure_details(job_id: str) -> str:
    """Technická část kopie provozovateli: ID jobu, řádky CHYBA a konec logu."""
    entries = [str(e.get("line") or "") for e in db.get_logs(job_id)]
    errors = [ln for ln in entries if ln.startswith("CHYBA")][-5:]
    tail = "\n".join(entries[-_OPERATOR_LOG_ENTRIES:])
    if len(tail) > _OPERATOR_LOG_MAX_CHARS:
        tail = "…\n" + tail[-_OPERATOR_LOG_MAX_CHARS:]
    parts = [f"Job: {job_id}"]
    if errors:
        parts.append("Chyba:\n" + "\n".join(errors))
    parts.append(f"Konec logu ({min(len(entries), _OPERATOR_LOG_ENTRIES)} záznamů):\n{tail}")
    return "\n\n".join(parts)


# Příloha kopie provozovateli: celý technický log, nejvýš tolik bajtů (konec logu).
_OPERATOR_LOG_ATTACH_MAX_BYTES = 512_000


def _operator_log_attachment(job_id: str, folder: Path) -> Path | None:
    """Celý technický log jobu jako textová příloha kopie provozovateli (None bez logu).

    Stejný obsah jako „Technický log“ na stránce jobu; delší log se ořízne zepředu.
    """
    entries = db.get_logs(job_id)
    if not entries:
        return None
    text = "\n".join(f"{e.get('at') or ''}  {e.get('line') or ''}" for e in entries) + "\n"
    data = text.encode("utf-8")
    if len(data) > _OPERATOR_LOG_ATTACH_MAX_BYTES:
        cut = data[-_OPERATOR_LOG_ATTACH_MAX_BYTES:].decode("utf-8", errors="ignore")
        data = ("… (začátek logu vynechán)\n" + cut).encode("utf-8")
    path = folder / f"podkladarna-log-{job_id}.txt"
    path.write_bytes(data)
    return path


def _operator_log_files(job_id: str, folder: Path) -> list[Path]:
    """Přílohy technické části (celý log); chyba zápisu e-mail neshodí."""
    try:
        path = _operator_log_attachment(job_id, folder)
    except OSError:
        logger.exception("Příloha logu jobu %s se nepodařila", job_id)
        return []
    return [path] if path else []


def notify_private_failure(job_id: str) -> None:
    """Privátní job skončil chybou: e-mail zadavateli, kopie FEEDBACK_TO s koncem logu.

    Best-effort, nikdy nevyhazuje. Volá se z job_worker (výjimka v pipeline),
    worker.py (timeout, pád subprocesu) i po restartu serveru.
    Oba e-maily mají odkaz na stránku jobu s logem; provozovatel navíc dostane
    technickou chybu, konec logu a celý log v příloze (zadavatel je nevidí).
    Zadal-li job provozovatel sám, technická část je v jeho jediném e-mailu.
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
        common = dict(
            job_name=job.get("name") or job_id,
            error=job.get("error"),
            duration_s=job.get("duration_s"),
            job_url=job_page_url(job_id),
        )
        operator = (app_settings.FEEDBACK_TO or "").strip()
        # Zadal-li job sám provozovatel, dostane jeden e-mail rovnou s technickou částí.
        own_job = bool(operator) and operator.lower() == email.lower()
        with tempfile.TemporaryDirectory(prefix="podkladarna-mail-") as tmp:
            log_files = _operator_log_files(job_id, Path(tmp)) if operator else []
            details = _operator_failure_details(job_id) if own_job else None
            subject, body = build_private_failed_email(**common, operator_details=details)
            try:
                send_mail(email, subject, body, attachments=log_files if own_job else None)
                db.append_log(job_id, f"{_FAIL_MAIL_SENT_PREFIX} na {email}.")
            except MailError as exc:
                db.append_log(job_id, f"CHYBA odeslání e-mailu o chybě: {exc}")
            if operator and not own_job:
                try:
                    subject_op, body_op = build_private_failed_email(
                        **common, operator_details=_operator_failure_details(job_id)
                    )
                    send_mail(
                        operator, f"[kopie] {subject_op}", body_op, attachments=log_files
                    )
                except MailError as exc:
                    db.append_log(job_id, f"CHYBA odeslání kopie e-mailu o chybě: {exc}")
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


def _record_usage(job_id: str, status: str) -> None:
    """Souhrnná statistika použití (patička webu); chyba job neshodí."""
    try:
        db.record_job_usage(job_id, status)
    except Exception:
        logger.exception("Statistika použití jobu %s selhala", job_id)


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
        _record_usage(job_id, db.STATUS_DONE_WITH_ERRORS if issues else "done")
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
        _record_usage(job_id, "failed")
        notify_private_failure(job_id)
        return 1


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m app.job_worker <job_id>", file=sys.stderr)
        return 2
    return run_job(sys.argv[1])


if __name__ == "__main__":
    sys.exit(main())
