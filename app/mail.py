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
    preview_url: str | None = None,
    georef_url: str | None = None,
) -> tuple[str, str]:
    """Vrací (předmět, tělo) pro hotový privátní job – česky.

    Odkazy (ZIP omap, volitelně PNG náhled / georef ZIP) jdou jen e-mailem;
    na webové stránce privátního jobu se nezobrazují.
    """
    subject = f"Podkladárna: mapa „{job_name}“ je připravená ke stažení"
    parts = [
        "Dobrý den,\n",
        f"vaše mapa „{job_name}“ je hotová.\n",
        "Stažení (privátní odkazy):\n",
        f"• ZIP s mapou (.omap / .ocd):\n{download_url}\n",
    ]
    if preview_url:
        parts.append(f"• Náhled PNG:\n{preview_url}\n")
    if georef_url:
        parts.append(f"• ZIP georeferencovaných náhledů:\n{georef_url}\n")
    parts.extend(
        [
            f"\nOdkazy platí {retention_hours} hodin od založení jobu; "
            "poté se job i soubory smažou.\n",
            "Mapa není veřejně viditelná v seznamu jobů "
            "a na stránce jobu se náhled/ZIP nezobrazují.\n",
            f"\n— {settings.SMTP_FROM_NAME or 'Podkladárna'}\n",
        ]
    )
    return subject, "".join(parts)
