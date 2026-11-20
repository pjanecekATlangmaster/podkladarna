"""Zpětná vazba z webu (kontakt + komentář) → e-mail vlastníkovi.

Patří sem i návrh nového zdroje dat (WMS apod.): jen e-mail k ručnímu
posouzení, server navrženou adresu nikdy nevolá (žádné riziko SSRF).
"""

from __future__ import annotations

import re
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from pathlib import Path

from app import settings
from app import i18n
from app.i18n import Msg
from app.mail import MailError, send_mail, sender_name
from app.pipeline.preview import resolve_preview_png
from app.rate_limit import is_exempt
from app.settings import JOBS_DIR

MAX_CONTACT_LEN = 200
MAX_COMMENT_LEN = 4000
MAX_JOB_ID_LEN = 64
MAX_SOURCE_URL_LEN = 500
MAX_SOURCE_AREA_LEN = 200
MAX_SOURCE_LICENSE_LEN = 500
MAX_REPLY_TO_LEN = 254

# Jedna holá adresa local@domena.tld (bez jména, čárek, uvozovek, mezer a CR/LF): jde do hlavičky Reply-To.
_REPLY_TO_RE = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}"
)

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


def reply_to_address(contact: str | None) -> str | None:
    """Kontakt z formuláře jako Reply-To, je-li to přesně jedna platná e-mailová adresa; jinak None.

    From zůstává adresa serveru (SPF / DKIM), Petr odpoví rovnou uživateli.
    """
    value = (contact or "").strip()
    if not value or len(value) > MAX_REPLY_TO_LEN:
        return None
    if _REPLY_TO_RE.fullmatch(value) is None or ".." in value:
        return None
    return value


def build_feedback_email(
    *,
    contact: str,
    comment: str,
    job_id: str,
    client_ip: str | None = None,
    job_name: str | None = None,
    job_status: str | None = None,
    preview_attached: bool = False,
    lang: str = i18n.SOURCE_LANG,
) -> tuple[str, str]:
    """Vrací (předmět, tělo) pro zpětnou vazbu v jazyce a se značkou stránky formuláře. job_id je povinné."""
    job_id = (job_id or "").strip()

    def tr(key: str, **params: object) -> str:
        return i18n.t(f"mail.feedback.{key}", lang, **params)

    subject = tr("subject", job_id=job_id)
    lines = [
        tr("intro"),
        "",
        f"{tr('contact')}: {contact}",
        f"{tr('job_id')}: {job_id}",
    ]
    if job_name:
        lines.append(f"{tr('name')}: {job_name}")
    if job_status:
        lines.append(f"{tr('status')}: {job_status}")
    url = job_page_url(job_id)
    if url:
        lines.append(f"{tr('link')}: {url}")
    if client_ip:
        lines.append(f"IP: {client_ip}")
    if preview_attached:
        lines.append(tr("preview"))
    lines.extend(["", f"{tr('comment')}:", comment.strip(), ""])
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
    lang: str = i18n.SOURCE_LANG,
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
        lang=lang,
    )
    send_mail(
        to,
        subject,
        body,
        attachments=[preview] if preview is not None else None,
        from_name=sender_name(lang),
        reply_to=reply_to_address(contact),
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
    lang: str = i18n.SOURCE_LANG,
) -> tuple[str, str]:
    """Vrací (předmět, tělo) pro návrh zdroje dat v jazyce a se značkou stránky formuláře."""
    host = urllib.parse.urlsplit(url).hostname or "?"

    def tr(key: str, **params: object) -> str:
        return i18n.t(f"mail.source.{key}", lang, **params)

    subject = tr("subject", host=host)
    lines = [
        tr("intro"),
        *tr("check").split("\n"),
        "",
        f"{tr('url')}: {url}",
        f"{tr('area')}: {area}",
        f"{tr('license')}: {license_text or tr('not_given')}",
        f"{i18n.t('mail.feedback.contact', lang)}: {contact}",
    ]
    if client_ip:
        lines.append(f"IP: {client_ip}")
    lines.extend(["", f"{tr('note')}:", comment.strip() or tr("no_note"), ""])
    return subject, "\n".join(lines)


def send_source_suggestion(
    *,
    url: str,
    area: str,
    license_text: str,
    contact: str,
    comment: str,
    client_ip: str | None = None,
    lang: str = i18n.SOURCE_LANG,
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
        lang=lang,
    )
    send_mail(to, subject, body, from_name=sender_name(lang), reply_to=reply_to_address(contact))
