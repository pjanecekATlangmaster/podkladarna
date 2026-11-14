"""Odesílání e-mailů přes SMTP (stdlib smtplib + email.message)."""

from __future__ import annotations

import logging
import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Sequence

from app import settings
from app.i18n import t

logger = logging.getLogger("podkladarna.mail")


class MailError(Exception):
    """Selhání při odesílání e-mailu."""


def _smtp_configured() -> bool:
    return bool((settings.SMTP_HOST or "").strip() and (settings.SMTP_FROM or "").strip())


def _guess_mime(path: Path) -> tuple[str, str]:
    """Vrátí (maintype, subtype) pro přílohu."""
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and "/" in guessed:
        main, sub = guessed.split("/", 1)
        return main, sub
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image", "png"
    if suffix in {".jpg", ".jpeg"}:
        return "image", "jpeg"
    return "application", "octet-stream"


def _attach_path(msg: EmailMessage, path: Path) -> None:
    """Přiloží existující soubor k EmailMessage."""
    path = Path(path)
    if not path.is_file():
        raise MailError(f"Příloha neexistuje: {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MailError(f"Přílohu nelze načíst ({path.name}): {exc}") from exc
    maintype, subtype = _guess_mime(path)
    # Stabilní jméno v mail klientovi (náhled mapy).
    filename = path.name
    if filename.lower() == "oom_preview.png":
        filename = "preview.png"
    elif filename.lower() in {"oom_preview.jpg", "oom_preview.jpeg"}:
        filename = "preview.jpg"
    msg.add_attachment(
        data,
        maintype=maintype,
        subtype=subtype,
        filename=filename,
    )


def send_mail(
    to: str,
    subject: str,
    body: str,
    *,
    from_addr: str | None = None,
    from_name: str | None = None,
    attachments: Sequence[Path] | None = None,
    bcc: str | None = None,
) -> None:
    """Pošle plain-text e-mail (volitelně s přílohami a skrytou kopií). Při chybě MailError."""
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
    bcc = (bcc or "").strip()
    if bcc and bcc.lower() != to.lower():
        # send_message pošle i na Bcc a hlavičku z odeslané zprávy vypustí.
        msg["Bcc"] = bcc
    msg.set_content(body)

    for path in attachments or ():
        _attach_path(msg, Path(path))

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

    n_att = len(list(attachments or ()))
    if n_att:
        logger.info(
            "E-mail na %s odeslán (předmět: %s, příloh: %s).",
            to,
            subject,
            n_att,
        )
    else:
        logger.info("E-mail na %s odeslán (předmět: %s).", to, subject)


def format_duration_cs(seconds: int | float | None) -> str | None:
    """České trvání jako ve web UI (např. „12 min 34 s“, „1 h 5 min“)."""
    return format_duration(seconds, "cs")


def format_duration(seconds: int | float | None, lang: str = "cs") -> str | None:
    """Trvání jako ve web UI (h / min / s jsou v cs i en stejné)."""
    if seconds is None:
        return None
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return None
    if s < 0:
        return None
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h:
        return f"{h} h {m} min"
    if m:
        return f"{m} min {sec} s" if sec else f"{m} min"
    return f"{sec} s"


def format_area_km2_cs(area_km2: float | None) -> str | None:
    """Plocha v km² s desetinnou čárkou jako ve web UI („2,4 km²“, pod 1 km² „0,85 km²“)."""
    return format_area_km2(area_km2, "cs")


def format_area_km2(area_km2: float | None, lang: str = "cs") -> str | None:
    """Plocha v km²; desetinná čárka v češtině (a dalších jazycích s čárkou), tečka v angličtině."""
    if area_km2 is None or area_km2 <= 0:
        return None
    text = f"{area_km2:.2f}" if area_km2 < 1 else f"{area_km2:.1f}"
    if lang != "en":
        text = text.replace(".", ",")
    return text + " km²"


def _signature() -> str:
    return f"— {settings.SMTP_FROM_NAME or 'Podkladárna'}\n"


def build_private_ready_email(
    *,
    job_name: str,
    download_url: str,
    retention_hours: int,
    preview_url: str | None = None,
    georef_url: str | None = None,
    preview_attached: bool = False,
    duration_s: int | float | None = None,
    issues_summary: str | None = None,
    area_km2: float | None = None,
    notices: list[str] | None = None,
    lang: str = "cs",
) -> tuple[str, str]:
    """Vrací (předmět, tělo) pro hotový privátní job v jazyce zadání (katalog ``mail.*``).

    ``notices`` = informativní upozornění pro příjemce mapy (např. nedostupná DTM), ne chyba jobu.

    Odkazy (ZIP omap, volitelně PNG náhled / georef ZIP) jdou jen e-mailem;
    na webové stránce privátního jobu se nezobrazují. Náhled PNG preferujeme
    jako MIME přílohu (preview_attached); odkaz zůstává jako záloha.
    """

    def tr(key: str, **params: object) -> str:
        return t(f"mail.ready.{key}", lang, **params)

    subject = tr("subject", name=job_name)
    parts = [tr("greeting") + "\n", "\n", tr("intro", name=job_name) + "\n"]
    duration_label = format_duration(duration_s, lang)
    if duration_label:
        parts.append(tr("duration", value=duration_label) + "\n")
    area_label = format_area_km2(area_km2, lang)
    if area_label:
        parts.append(tr("area", value=area_label) + "\n")
    if issues_summary:
        parts.append("\n" + tr("issues", value=issues_summary) + "\n")
    for notice in notices or []:
        parts.append(f"\n{notice}\n")
    parts.extend(["\n", tr("links") + "\n", "\n", tr("zip", url=download_url) + "\n"])
    if preview_attached:
        parts.append("\n")
        parts.append(tr("preview_attached") + "\n")
        if preview_url:
            parts.append(tr("preview_backup", url=preview_url) + "\n")
    elif preview_url:
        parts.append("\n")
        parts.append(tr("preview", url=preview_url) + "\n")
    if georef_url:
        parts.append("\n")
        parts.append(tr("georef", url=georef_url) + "\n")
    parts.extend(
        [
            "\n",
            tr("expiry", hours=retention_hours) + "\n",
            "\n",
            tr("private_note") + "\n",
            "\n",
            _signature(),
        ]
    )
    return subject, "".join(parts)


def build_private_failed_email(
    *,
    job_name: str,
    error: str | None,
    duration_s: int | float | None = None,
    job_url: str | None = None,
    operator_details: str | None = None,
    lang: str = "cs",
) -> tuple[str, str]:
    """Vrací (předmět, tělo) pro privátní job, který skončil chybou, v jazyce zadání.

    ``job_url`` vede na stránku jobu s logem (stejný odkaz dostane zadavatel
    i provozovatel; stažení ZIPu dál chrání token). ``operator_details`` jen
    v kopii provozovateli: technická chyba a konec logu pro rychlou diagnózu
    (část pro provozovatele je vždy česky).
    """

    def tr(key: str, **params: object) -> str:
        return t(f"mail.failed.{key}", lang, **params)

    subject = tr("subject", name=job_name)
    parts = [tr("greeting") + "\n", "\n", tr("intro", name=job_name) + "\n"]
    duration_label = format_duration(duration_s, lang)
    if duration_label:
        parts.append(tr("duration", value=duration_label) + "\n")
    if error:
        parts.append("\n" + tr("reason", value=error) + "\n")
    if job_url:
        parts.append("\n" + tr("job_link", url=job_url) + "\n")
    parts.extend(
        [
            "\n",
            tr("retry") + "\n",
            "\n",
            tr("operator_knows") + "\n",
            "\n",
            _signature(),
        ]
    )
    if operator_details:
        parts.extend(
            [
                "\n",
                "==== Pro provozovatele (zadavatel tuto část nedostal) ====\n",
                "\n",
                operator_details.rstrip("\n") + "\n",
            ]
        )
    return subject, "".join(parts)
