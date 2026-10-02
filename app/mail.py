"""Odesílání e-mailů přes SMTP (stdlib smtplib + email.message)."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from app import settings

logger = logging.getLogger("podkladarna.mail")


class MailError(Exception):
    """Selhání při odesílání e-mailu."""


def _smtp_configured() -> bool:
    return bool((settings.SMTP_HOST or "").strip() and (settings.SMTP_FROM or "").strip())


def send_mail(
    to: str,
    subject: str,
    body: str,
    *,
    from_addr: str | None = None,
    from_name: str | None = None,
) -> None:
    """Pošle plain-text e-mail. Při chybě vyhodí MailError a zaloguje česky."""
    to = (to or "").strip()
    if not to:
        raise MailError("Chybí adresa příjemce.")
    if not _smtp_configured():
        raise MailError(
            "SMTP není nastavené (SMTP_HOST / SMTP_FROM). "
            "Viz DEV.md."
        )

    sender = (from_addr or settings.SMTP_FROM).strip()
    name = (from_name if from_name is not None else settings.SMTP_FROM_NAME) or ""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((name, sender)) if name else sender
    msg["To"] = to
    msg.set_content(body)

    host = settings.SMTP_HOST.strip()
    port = int(settings.SMTP_PORT)
    enc = (settings.SMTP_ENCRYPTION or "starttls").strip().lower()
    user = (settings.SMTP_USER or "").strip()
    password = settings.SMTP_PASSWORD or ""

    try:
        if enc in {"ssl", "smtps"}:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                if enc in {"starttls", "tls", "1", "true", "yes"}:
                    context = ssl.create_default_context()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
    except MailError:
        raise
    except Exception as exc:
        logger.error("E-mail na %s se nepodařilo odeslat: %s", to, exc)
        raise MailError(f"Odeslání e-mailu selhalo: {exc}") from exc

    logger.info("E-mail na %s odeslán (předmět: %s).", to, subject)


def build_private_ready_email(
    *,
    job_name: str,
    download_url: str,
    retention_hours: int,
) -> tuple[str, str]:
    """Vrací (předmět, tělo) pro hotový privátní job – česky."""
    subject = f"Podkladárna: mapa „{job_name}“ je připravená ke stažení"
    body = (
        f"Dobrý den,\n\n"
        f"vaše mapa „{job_name}“ je hotová.\n\n"
        f"Stažení (privátní odkaz):\n{download_url}\n\n"
        f"Odkaz platí {retention_hours} hodin od založení jobu; "
        f"poté se job i soubory smažou.\n\n"
        f"Mapa není veřejně viditelná v seznamu jobů.\n\n"
        f"— {settings.SMTP_FROM_NAME or 'Podkladárna'}\n"
    )
    return subject, body
