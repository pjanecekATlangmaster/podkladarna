"""Zpětná vazba z webu (kontakt + komentář) → e-mail vlastníkovi.

Patří sem i návrh nového zdroje dat (WMS apod.): jen e-mail k ručnímu
posouzení, server navrženou adresu nikdy nevolá (žádné riziko SSRF).
"""

from __future__ import annotations

import threading
import time
import urllib.parse
from collections import defaultdict, deque
from pathlib import Path

from app import settings
from app.i18n import Msg
from app.mail import MailError, send_mail
from app.pipeline.preview import resolve_preview_png
from app.rate_limit import is_exempt
from app.settings import JOBS_DIR

MAX_CONTACT_LEN = 200
MAX_COMMENT_LEN = 4000
MAX_JOB_ID_LEN = 64
MAX_SOURCE_URL_LEN = 500
MAX_SOURCE_AREA_LEN = 200
MAX_SOURCE_LICENSE_LEN = 500

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
            return Msg("api.feedback.rate", limit=limit)
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
    preview_attached: bool = False,
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
    if preview_attached:
        lines.append("Náhled PNG: v příloze tohoto e-mailu.")
    lines.extend(["", "Komentář:", comment.strip(), ""])
    return subject, "\n".join(lines)


def _job_preview_attachment(job_id: str) -> Path | None:
    """Existující náhled jobu pro MIME přílohu, nebo None."""
    job_id = (job_id or "").strip()
    if not job_id:
        return None
    job_dir = JOBS_DIR / job_id
    path = resolve_preview_png(job_dir / "output", job_dir / "work")
    if path is not None and path.is_file():
        return path
    return None


def send_feedback(
    *,
    contact: str,
    comment: str,
    job_id: str,
    client_ip: str | None = None,
    job_name: str | None = None,
    job_status: str | None = None,
) -> None:
    """Pošle zpětnou vazbu na FEEDBACK_TO. Při chybě MailError.

    Pokud job má náhled PNG/JPEG, přiloží ho jako MIME attachment.
    """
    to = (settings.FEEDBACK_TO or "").strip()
    if not to:
        raise MailError("Chybí adresa pro zpětnou vazbu (FEEDBACK_TO).")
    job_id = (job_id or "").strip()
    if not job_id:
        raise MailError("Chybí job_id.")
    preview = _job_preview_attachment(job_id)
    subject, body = build_feedback_email(
        contact=contact,
        comment=comment,
        job_id=job_id,
        client_ip=client_ip,
        job_name=job_name,
        job_status=job_status,
        preview_attached=preview is not None,
    )
    send_mail(
        to,
        subject,
        body,
        attachments=[preview] if preview is not None else None,
    )


def is_valid_source_url(url: str) -> bool:
    """Jen tvar adresy (http/https, host, bez mezer). Adresa se nikdy nevolá."""
    url = (url or "").strip()
    if not url or len(url) > MAX_SOURCE_URL_LEN or any(c.isspace() for c in url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def build_source_suggestion_email(
    *,
    url: str,
    area: str,
    license_text: str,
    contact: str,
    comment: str,
    client_ip: str | None = None,
) -> tuple[str, str]:
    """Vrací (předmět, tělo) pro návrh zdroje dat – česky."""
    host = urllib.parse.urlsplit(url).hostname or "?"
    subject = f"Podkladárna: návrh zdroje dat ({host})"
    lines = [
        "Nový návrh zdroje dat z webu Podkladárny.",
        "Adresa nebyla automaticky ověřena ani volána – před zařazením",
        "zkontroluj funkčnost, licenci (tvorba a šíření map) a citaci.",
        "",
        f"Adresa: {url}",
        f"Země / oblast: {area}",
        f"Licence / podmínky: {license_text or '(neuvedeno)'}",
        f"Kontakt: {contact}",
    ]
    if client_ip:
        lines.append(f"IP: {client_ip}")
    lines.extend(["", "Poznámka:", comment.strip() or "(bez poznámky)", ""])
    return subject, "\n".join(lines)


def send_source_suggestion(
    *,
    url: str,
    area: str,
    license_text: str,
    contact: str,
    comment: str,
    client_ip: str | None = None,
) -> None:
    """Pošle návrh zdroje na FEEDBACK_TO. Při chybě MailError."""
    to = (settings.FEEDBACK_TO or "").strip()
    if not to:
        raise MailError("Chybí adresa pro zpětnou vazbu (FEEDBACK_TO).")
    subject, body = build_source_suggestion_email(
        url=url,
        area=area,
        license_text=license_text,
        contact=contact,
        comment=comment,
        client_ip=client_ip,
    )
    send_mail(to, subject, body)
