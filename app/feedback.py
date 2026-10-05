"""Zpětná vazba z webu (kontakt + komentář) → e-mail vlastníkovi."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from app import settings
from app.mail import MailError, send_mail
from app.rate_limit import is_exempt

MAX_CONTACT_LEN = 200
MAX_COMMENT_LEN = 4000
MAX_JOB_ID_LEN = 64

_lock = threading.Lock()
_hits: dict[str, deque[float]] = defaultdict(deque)


def reset_feedback_rate_limits() -> None:
    """Jen pro testy – vyprázdní in-memory limity."""
    with _lock:
        _hits.clear()


def check_feedback_rate(client_ip: str) -> str | None:
    """Vrátí chybovou zprávu, nebo None pokud je odeslání povoleno."""
    if is_exempt(client_ip):
        return None
    window = 3600.0
    limit = max(1, int(settings.MAX_FEEDBACK_PER_IP_HOUR))
    now = time.monotonic()
    key = (client_ip or "unknown").strip() or "unknown"
    with _lock:
        q = _hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return (
                f"Z této sítě už bylo odesláno {limit} zpráv za hodinu. "
                "Zkuste to později, nebo napište na GitHub Issues."
            )
        q.append(now)
    return None


def job_page_url(job_id: str) -> str | None:
    """Krátký odkaz na job (API detail), pokud je PUBLIC_BASE_URL."""
    base = (settings.PUBLIC_BASE_URL or "").rstrip("/")
    job_id = (job_id or "").strip()
    if not base or not job_id:
        return None
    return f"{base}/api/jobs/{job_id}"


def build_feedback_email(
    *,
    contact: str,
    comment: str,
    job_id: str,
    client_ip: str | None = None,
    job_name: str | None = None,
    job_status: str | None = None,
) -> tuple[str, str]:
    """Vrací (předmět, tělo) pro zpětnou vazbu – česky. job_id je povinné."""
    job_id = (job_id or "").strip()
    subject = f"Podkladárna: zpětná vazba (job {job_id})"
    lines = [
        "Nová zpětná vazba z webu Podkladárny.",
        "",
        f"Kontakt: {contact}",
        f"Job ID: {job_id}",
    ]
    if job_name:
        lines.append(f"Název: {job_name}")
    if job_status:
        lines.append(f"Stav jobu: {job_status}")
    url = job_page_url(job_id)
    if url:
        lines.append(f"Odkaz: {url}")
    if client_ip:
        lines.append(f"IP: {client_ip}")
    lines.extend(["", "Komentář:", comment.strip(), ""])
    return subject, "\n".join(lines)


def send_feedback(
    *,
    contact: str,
    comment: str,
    job_id: str,
    client_ip: str | None = None,
    job_name: str | None = None,
    job_status: str | None = None,
) -> None:
    """Pošle zpětnou vazbu na FEEDBACK_TO. Při chybě MailError."""
    to = (settings.FEEDBACK_TO or "").strip()
    if not to:
        raise MailError("Chybí adresa pro zpětnou vazbu (FEEDBACK_TO).")
    job_id = (job_id or "").strip()
    if not job_id:
        raise MailError("Chybí job_id.")
    subject, body = build_feedback_email(
        contact=contact,
        comment=comment,
        job_id=job_id,
        client_ip=client_ip,
        job_name=job_name,
        job_status=job_status,
    )
    send_mail(to, subject, body)
