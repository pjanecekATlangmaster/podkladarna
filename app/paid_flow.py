"""Tok objednávky mapy na doméně v režimu ``paid`` (bez účtů, za vypínačem ``PAYMENTS_PROVIDER``).

1. Formulář na placené doméně (``POST /api/jobs``) job nezaloží: uloží zadání do objednávky
   (``app.orders``) a pošle e-mail s odkazem na potvrzení.
2. ``/order/confirm/<token>``: stránka s tlačítkem (GET nic nemění – skenery odkazů v poště),
   POST založí a zařadí job.
3. Job doběhne → ``app.job_worker`` pošle místo odkazu ke stažení odkaz na stránku objednávky
   ``/order/<token>`` (náhled s vodoznakem, cena, tlačítko Koupit). Výstupy jobu jsou do zaplacení
   zamčené (``job_locked``).
4. Koupit → platba u poskytovatele → webhook ``/api/payments/webhook/<poskytovatel>`` → e-mail
   s odkazem ``/order/download/<token>``.

Mimo placenou doménu, nebo bez ``PAYMENTS_PROVIDER``, se nic nemění. Návrh: ``platby-navrh.md``.
"""

from __future__ import annotations

import html
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app import db, i18n, orders, payments
from app import settings as app_settings
from app.mail import MailError, send_mail

logger = logging.getLogger("podkladarna.paid_flow")

router = APIRouter()

ORDER_PREVIEW_NAME = "order_preview.png"
ORDER_PREVIEW_MAX_SIDE = 1000
ORDER_MAIL_SENT_PREFIX = "E-mail s náhledem objednávky odeslán"

_mail_lock = threading.Lock()
_mail_hits: dict[str, deque[float]] = {}
_MAIL_WINDOW_S = 3600.0
_ALL = "*"


def reset_order_mail_limits() -> None:
    """Jen pro testy – vyprázdní limity potvrzovacích e-mailů."""
    with _mail_lock:
        _mail_hits.clear()


def _check_order_mail_rate(client_ip: str) -> bool:
    """Smí se poslat další potvrzovací e-mail? (limit na IP a celkový, okno 1 h, v paměti procesu)."""
    from app.rate_limit import is_exempt

    if is_exempt(client_ip):
        return True
    now = time.monotonic()
    limits = (
        ((client_ip or "unknown").strip() or "unknown", app_settings.ORDER_MAILS_PER_IP_HOUR),
        (_ALL, app_settings.ORDER_MAILS_PER_HOUR),
    )
    with _mail_lock:
        for key, limit in limits:
            q = _mail_hits.setdefault(key, deque())
            while q and now - q[0] > _MAIL_WINDOW_S:
                q.popleft()
            if limit > 0 and len(q) >= limit:
                return False
        for key, _limit in limits:
            _mail_hits[key].append(now)
    return True


def _one_line(text: str) -> str:
    """Název mapy do předmětu e-mailu a stránek: bez odřádkování a řídicích znaků."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in str(text or "")).split())


# --- Kdy tok platí ---------------------------------------------------------------------------


def paid_site(request: Request) -> i18n.Site | None:
    site = i18n.site_for_request(request)
    return site if site is not None and site.mode == "paid" else None


def active_for(request: Request) -> bool:
    """Prodej běží jen na doméně v režimu paid a se zapnutým poskytovatelem plateb."""
    return paid_site(request) is not None and payments.payments_enabled()


def _provider() -> payments.PaymentProvider:
    provider = payments.get_provider()
    if provider is None:
        raise HTTPException(404, i18n.Msg("api.order.not_found"))
    return provider


def _base_url(site_host: str | None, request: Request | None = None) -> str:
    base = i18n.site_base_url(site_host)
    if base:
        return base
    if request is not None:
        return str(request.base_url).rstrip("/")
    return (getattr(app_settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")


def _lang(value: str | None) -> str:
    lang = str(value or "").strip().lower()
    return lang if lang in i18n.available_langs() else i18n.SOURCE_LANG


def price_label(lang: str) -> str:
    amount = app_settings.PAID_PRICE_CENTS / 100
    text = f"{amount:.2f}"
    if lang in ("cs", "sk", "de", "pl", "fr"):
        text = text.replace(".", ",")
    return i18n.t("order.price", lang, amount=text, currency=app_settings.PAID_CURRENCY)


def valid_email(email: str) -> bool:
    email = (email or "").strip()
    return bool(email) and "@" in email and "." in email.split("@")[-1] and len(email) <= 254


def form_note_html(request: Request, lang: str) -> str:
    """Upozornění nad tlačítkem Spustit: placená služba, cena a postup (jen když prodej běží)."""
    if not active_for(request):
        return ""
    text = i18n.t("order.web.note", lang, price=price_label(lang))
    return f'<p class="hint paid-note" id="paid-note"><strong>{html.escape(text)}</strong></p>'


# --- Zamčené výstupy -----------------------------------------------------------------------


def job_order_id(job: dict[str, Any]) -> str | None:
    value = (job.get("options") or {}).get("order_id")
    return str(value) if value else None


def job_locked(job: dict[str, Any]) -> bool:
    """Výstupy jobu objednávky jsou do zaplacení zamčené (i přes token jobu)."""
    order_id = job_order_id(job)
    if not order_id:
        return False
    try:
        order = orders.get_order(order_id)
    except KeyError:
        return True
    return not orders.is_paid(order)


# --- Založení objednávky z formuláře ---------------------------------------------------------


def start_order(
    request: Request,
    *,
    name: str,
    preset_id: str,
    options: dict[str, Any],
    email: str,
) -> dict[str, Any]:
    """Místo jobu založí objednávku a pošle e-mail s odkazem na potvrzení."""
    lang = _lang(options.get("lang"))
    email = (email or "").strip()
    name = _one_line(name) or "mapa"
    if not valid_email(email):
        raise HTTPException(400, i18n.Msg("api.order.email_required"))
    if not _check_order_mail_rate(str(options.get("client_ip") or "")):
        raise HTTPException(429, i18n.Msg("api.order.rate"))
    site = paid_site(request)
    # Placená mapa je vždy ZIP (volba „jen PNG“ by po zaplacení neměla co doručit).
    job_options = {**options, "private": True, "notify_email": email, "output_zip": True}
    request_data = {"name": name, "preset_id": preset_id, "options": job_options}
    try:
        order, token = orders.create_order(
            email=email,
            job_request=request_data,
            site=site.host if site else None,
            lang=lang,
        )
    except orders.OrderLimitError:
        raise HTTPException(429, i18n.Msg("api.order.limit")) from None
    except orders.OrderError:
        raise HTTPException(400, i18n.Msg("api.order.email_required")) from None
    url = f"{_base_url(order.get('site'), request)}/order/confirm/{token}"
    hours = app_settings.ORDER_CONFIRM_HOURS if app_settings.ORDER_CONFIRM_HOURS > 0 else 24
    subject = i18n.t("order.mail.confirm.subject", lang, name=name)
    body = i18n.t(
        "order.mail.confirm.body",
        lang,
        name=name,
        url=url,
        hours=hours,
        price=price_label(lang),
    )
    try:
        send_mail(email, subject, body)
    except (MailError, ValueError) as exc:  # ValueError: neplatná hlavička (adresa)
        orders.cancel_unconfirmed(order["id"])
        logger.warning("Order confirmation mail failed: %s", exc)
        raise HTTPException(502, i18n.Msg("api.order.mail_failed")) from exc
    logger.info("Order %s awaiting confirmation", order["id"])
    return i18n.localize(
        {
            "order_pending": True,
            "message": i18n.Msg("api.order.check_email", email=email, hours=hours),
        },
        i18n.resolve_lang(request)[0],
    )


# --- Stránky ---------------------------------------------------------------------------------


def _page(lang: str, title: str, body: str, *, status_code: int = 200) -> HTMLResponse:
    esc = html.escape
    doc = (
        f'<!doctype html><html lang="{esc(lang)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="robots" content="noindex"><title>{esc(title)}</title>'
        '<link rel="stylesheet" href="/static/style.css"></head>'
        f'<body><main class="order-page" style="max-width:720px;margin:2rem auto;padding:0 16px">'
        f"<h1>{esc(title)}</h1>{body}</main></body></html>"
    )
    response = HTMLResponse(doc, status_code=status_code)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _p(text: str) -> str:
    return f"<p>{html.escape(text)}</p>"


def _invalid_link(lang: str) -> HTMLResponse:
    return _page(
        lang,
        i18n.t("order.page.invalid_title", lang),
        _p(i18n.t("order.page.invalid_text", lang)),
        status_code=404,
    )


def _request_lang(request: Request) -> str:
    return i18n.resolve_lang(request)[0]


@router.get("/order/confirm/{token}", response_class=HTMLResponse)
def order_confirm_page(token: str, request: Request) -> HTMLResponse:
    lang = _request_lang(request)
    order = orders.get_order_by_token("confirm", token)
    if order is None or order["status"] != orders.STATUS_AWAITING:
        return _invalid_link(lang)
    lang = _lang(order.get("lang")) or lang
    name = str((order.get("job_request") or {}).get("name") or "")
    body = (
        _p(i18n.t("order.page.confirm_text", lang, name=name, price=price_label(lang)))
        + f'<form method="post" action="/order/confirm/{html.escape(token)}">'
        f'<button type="submit">{html.escape(i18n.t("order.page.confirm_button", lang))}</button></form>'
    )
    return _page(lang, i18n.t("order.page.confirm_title", lang), body)


@router.post("/order/confirm/{token}")
def order_confirm(token: str, request: Request):
    from app import worker

    lang = _request_lang(request)
    if not payments.payments_enabled():
        return _invalid_link(lang)
    if (worker.is_busy() or worker.is_draining()) and not worker.can_accept_job():
        return _page(
            lang,
            i18n.t("order.page.busy_title", lang),
            _p(i18n.t("order.page.busy_text", lang)),
            status_code=503,
        )
    order = orders.claim_confirmation(token)
    if order is None:
        return _invalid_link(lang)
    req = order.get("job_request") or {}
    try:
        options = {**(req.get("options") or {}), "order_id": order["id"]}
        job = db.create_job(str(req.get("name") or order["id"]), str(req["preset_id"]), options)
        job_id = job["id"]
        db.append_log(
            job_id,
            f"Přijato (objednávka {order['id'][:8]}…, e-mail potvrzen): preset {req['preset_id']} · "
            f"1:{options.get('map_scale')} · listy SM5 {', '.join(options.get('sm5_sheets') or [])}",
        )
        db.append_log(
            job_id,
            "Objednávka – po dokončení přijde e-mail s náhledem a odkazem na nákup plné mapy.",
        )
        worker.enqueue(job_id)
    except Exception:
        orders.release_confirmation(order["id"])
        logger.exception("Order %s: job creation failed", order["id"])
        raise HTTPException(500, i18n.Msg("api.order.job_failed")) from None
    orders.attach_job(order["id"], job_id)
    return RedirectResponse(f"/?job={job_id}", status_code=303)


# Pevné cesty před /order/{token}, jinak by je zachytila stránka objednávky.


@router.get("/order/thanks", response_class=HTMLResponse)
def order_thanks(request: Request) -> HTMLResponse:
    lang = _request_lang(request)
    return _page(lang, i18n.t("order.page.thanks_title", lang), _p(i18n.t("order.page.thanks_text", lang)))


@router.get("/order/download/{token}")
def order_download(token: str, request: Request):
    lang = _request_lang(request)
    order = orders.get_order_by_token("download", token)
    if order is None or not order.get("job_id"):
        return _invalid_link(lang)
    from app.settings import JOBS_DIR

    job_id = str(order["job_id"])
    try:
        job = db.get_job(job_id)
    except KeyError:
        return _invalid_link(lang)
    out = JOBS_DIR / job_id / "output"
    zip_path = next(
        (out / n for n in ("podkladarna_output.zip", "podkladarna_oom.zip") if (out / n).is_file()),
        None,
    )
    if zip_path is None:
        return _invalid_link(lang)
    try:
        db.record_download(job_id, zip_path.stat().st_size)
    except Exception:  # noqa: BLE001
        logger.exception("Statistika stažení objednávky %s selhala", order["id"])
    return FileResponse(zip_path, filename=db.zip_download_filename(job_id, job["name"]))


def _preview_png(job_id: str) -> Path | None:
    from app.pipeline.preview import resolve_preview_png

    from app.settings import JOBS_DIR

    job_dir = JOBS_DIR / job_id
    return resolve_preview_png(job_dir / "output", job_dir / "work")


def watermarked_preview(src: Path, dst: Path, label: str) -> Path | None:
    """Zmenšený náhled (delší strana ORDER_PREVIEW_MAX_SIDE) se šikmým vodoznakem, bez georeference."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:  # pragma: no cover - Pillow je v image vždy
        return None
    try:
        with Image.open(src) as img:
            base = img.convert("RGBA")
        base.thumbnail((ORDER_PREVIEW_MAX_SIDE, ORDER_PREVIEW_MAX_SIDE))
        w, h = base.size
        try:
            font = ImageFont.load_default(size=max(14, min(w, h) // 18))
        except TypeError:  # starší Pillow bez velikosti
            font = ImageFont.load_default()
        side = int((w * w + h * h) ** 0.5) + 2
        layer = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        text = f"  {label}  "
        box = draw.textbbox((0, 0), text, font=font)
        tw, th = max(1, box[2] - box[0]), max(1, box[3] - box[1])
        for row, y in enumerate(range(0, side, th * 4)):
            offset = (row % 2) * tw // 2
            for x in range(-tw + offset, side, tw):
                draw.text((x, y), text, font=font, fill=(200, 0, 0, 90))
        layer = layer.rotate(30, resample=Image.BICUBIC)
        left, top = (side - w) // 2, (side - h) // 2
        base.alpha_composite(layer.crop((left, top, left + w, top + h)))
        dst.parent.mkdir(parents=True, exist_ok=True)
        base.convert("RGB").save(dst, "PNG", optimize=True)
        return dst
    except Exception:  # noqa: BLE001 – náhled je doplněk, objednávku nezastaví
        logger.exception("Watermarked preview failed for %s", src)
        return None


def order_preview_path(job_id: str, site_host: str | None) -> Path | None:
    from app.settings import JOBS_DIR

    dst = JOBS_DIR / job_id / "output" / ORDER_PREVIEW_NAME
    if dst.is_file():
        return dst
    src = _preview_png(job_id)
    if src is None:
        return None
    label = f"PREVIEW · {site_host or 'Podkladárna'}"
    return watermarked_preview(src, dst, label)


def order_sample_path(job: dict[str, Any]) -> Path | None:
    """Ukázka 4 × 4 cm papíru (``.omap``) z hotového ZIPu; vzniká jednou a leží vedle výstupů."""
    from app import order_sample
    from app.settings import JOBS_DIR

    out = JOBS_DIR / str(job["id"]) / "output"
    dst = out / order_sample.SAMPLE_ZIP_NAME
    if dst.is_file():
        return dst
    src = next(
        (out / n for n in ("podkladarna_output.zip", "podkladarna_oom.zip") if (out / n).is_file()),
        None,
    )
    if src is None:
        return None
    seed = order_sample.sample_seed(job.get("options") or {}, str(job.get("preset_id") or ""))
    return order_sample.build_sample_zip(src, dst, seed)


@router.get("/order/{token}", response_class=HTMLResponse)
def order_page(token: str, request: Request) -> HTMLResponse:
    lang = _request_lang(request)
    order = orders.get_order_by_token("preview", token)
    if order is None:
        return _invalid_link(lang)
    lang = _lang(order.get("lang"))
    esc = html.escape
    try:
        job = db.get_job(str(order.get("job_id") or ""))
        name = str(job.get("name") or "")
    except KeyError:
        return _invalid_link(lang)
    parts = [_p(i18n.t("order.page.preview_text", lang, name=name))]
    if order_preview_path(job["id"], order.get("site")) is not None:
        parts.append(
            f'<p><img src="/order/{esc(token)}/preview.png" alt="{esc(i18n.t("order.page.preview_alt", lang))}" '
            'style="max-width:100%;height:auto;border:1px solid #ccc"></p>'
        )
    if order_sample_path(job) is not None:
        parts.append(
            f'<p><a href="/order/{esc(token)}/sample.zip">{esc(i18n.t("order.page.sample_link", lang))}</a></p>'
        )
    if order["status"] == orders.STATUS_PREVIEW:
        parts.append(_p(i18n.t("order.page.buy_text", lang, price=price_label(lang))))
        parts.append(
            f'<form method="post" action="/order/{esc(token)}/checkout">'
            f'<button type="submit">{esc(i18n.t("order.page.buy_button", lang, price=price_label(lang)))}</button>'
            "</form>"
        )
        parts.append(_p(i18n.t("order.page.withdrawal_note", lang)))
    elif order["status"] == orders.STATUS_PAID:
        parts.append(_p(i18n.t("order.page.paid_text", lang)))
    return _page(lang, i18n.t("order.page.preview_title", lang), "".join(parts))


@router.get("/order/{token}/sample.zip")
def order_sample_download(token: str):
    order = orders.get_order_by_token("preview", token)
    if order is None or not order.get("job_id"):
        raise HTTPException(404, i18n.Msg("api.order.not_found"))
    try:
        job = db.get_job(str(order["job_id"]))
    except KeyError:
        raise HTTPException(404, i18n.Msg("api.order.not_found")) from None
    path = order_sample_path(job)
    if path is None:
        raise HTTPException(404, i18n.Msg("api.order.not_found"))
    response = FileResponse(path, media_type="application/zip", filename=path.name)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.get("/order/{token}/preview.png")
def order_preview(token: str):
    order = orders.get_order_by_token("preview", token)
    if order is None or not order.get("job_id"):
        raise HTTPException(404, i18n.Msg("api.order.not_found"))
    path = order_preview_path(str(order["job_id"]), order.get("site"))
    if path is None:
        raise HTTPException(404, i18n.Msg("api.preview_missing"))
    response = FileResponse(path, media_type="image/png")
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.post("/order/{token}/checkout")
def order_checkout(token: str, request: Request):
    lang = _request_lang(request)
    order = orders.get_order_by_token("preview", token)
    if order is None or order["status"] != orders.STATUS_PREVIEW:
        return _invalid_link(lang)
    provider = _provider()
    base = _base_url(order.get("site"), request)
    try:
        job = db.get_job(str(order.get("job_id") or ""))
    except KeyError:
        return _invalid_link(lang)
    try:
        session = provider.create_checkout(
            order_id=order["id"],
            amount_cents=int(order["price_cents"]),
            currency=str(order["currency"]),
            email=order.get("email"),
            description=i18n.t("order.checkout.description", _lang(order.get("lang")), name=job.get("name") or ""),
            success_url=f"{base}/order/thanks",
            cancel_url=f"{base}/order/{token}",
            lang=order.get("lang"),
        )
    except payments.PaymentError:
        logger.exception("Checkout failed for order %s", order["id"])
        raise HTTPException(502, i18n.Msg("api.order.checkout_failed")) from None
    orders.set_checkout(order["id"], provider.name, session.provider_ref)
    return RedirectResponse(session.url, status_code=303)


# --- Platby ----------------------------------------------------------------------------------


def handle_payment_event(provider: payments.PaymentProvider, event: payments.PaymentEvent) -> None:
    """Zapíše ověřenou událost a při prvním zaplacení pošle e-mail s plnou mapou."""
    order, token = orders.apply_payment_event(event, provider.name)
    if order is None:
        logger.warning("Payment event %s for unknown order %s", event.event_id, event.order_id)
        return
    if order.get("job_id"):
        db.append_log(str(order["job_id"]), f"Platba: {event.kind} (objednávka {order['id'][:8]}…).")
    if not token or not order.get("email"):
        return
    lang = _lang(order.get("lang"))
    try:
        job = db.get_job(str(order.get("job_id") or ""))
        name = str(job.get("name") or "")
    except KeyError:
        name = ""
    base = _base_url(order.get("site"))
    days = app_settings.PAID_DOWNLOAD_DAYS if app_settings.PAID_DOWNLOAD_DAYS > 0 else 30
    receipt = provider.receipt_url(str(order.get("provider_ref") or ""))
    body = i18n.t(
        "order.mail.paid.body",
        lang,
        name=name,
        url=f"{base}/order/download/{token}",
        days=days,
    )
    if receipt:
        body += "\n\n" + i18n.t("order.mail.paid.receipt", lang, url=receipt)
    try:
        send_mail(str(order["email"]), i18n.t("order.mail.paid.subject", lang, name=name), body)
        if order.get("job_id"):
            db.append_log(str(order["job_id"]), "E-mail s odkazem na plnou mapu odeslán.")
    except MailError as exc:
        logger.error("Paid mail for order %s failed: %s", order["id"], exc)
        if order.get("job_id"):
            db.append_log(str(order["job_id"]), f"CHYBA odeslání e-mailu s plnou mapou: {exc}")


@router.post("/api/payments/webhook/{provider_name}")
async def payment_webhook(provider_name: str, request: Request):
    provider = payments.get_provider() if payments.payments_enabled() else None
    if provider is None or provider.name != provider_name:
        raise HTTPException(404, i18n.Msg("api.order.not_found"))
    body = await request.body()
    try:
        event = provider.parse_webhook(body, request.headers)
    except payments.WebhookError:
        logger.warning("Rejected payment webhook (%s)", provider_name)
        return JSONResponse({"ok": False}, status_code=400)
    if event is not None:
        handle_payment_event(provider, event)
    return {"ok": True}


# Falešná platba: jen s PAYMENTS_PROVIDER=fake (vývoj, test B). Skutečný poskytovatel ji nemá.


def _fake_enabled() -> bool:
    provider = payments.get_provider() if payments.payments_enabled() else None
    return provider is not None and provider.name == "fake"


@router.get("/api/payments/fake/checkout/{ref}", response_class=HTMLResponse)
def fake_checkout_page(ref: str, order: str, request: Request) -> HTMLResponse:
    lang = _request_lang(request)
    if not _fake_enabled():
        return _invalid_link(lang)
    esc = html.escape
    body = (
        _p(i18n.t("order.page.fake_text", lang))
        + f'<form method="post" action="/api/payments/fake/checkout/{esc(ref)}?order={esc(order)}">'
        f'<button type="submit">{esc(i18n.t("order.page.fake_button", lang))}</button></form>'
    )
    return _page(lang, i18n.t("order.page.fake_title", lang), body)


@router.post("/api/payments/fake/checkout/{ref}")
def fake_checkout_pay(ref: str, order: str, request: Request):
    from app.payments import fake

    lang = _request_lang(request)
    if not _fake_enabled():
        return _invalid_link(lang)
    try:
        current = orders.get_order(order)
    except KeyError:
        return _invalid_link(lang)
    if current.get("provider_ref") != ref:
        return _invalid_link(lang)
    provider = _provider()
    body = fake.event_body(order, ref, amount_cents=int(current["price_cents"]), currency=current["currency"])
    event = provider.parse_webhook(body, {fake.SIGNATURE_HEADER: fake.sign(body)})
    if event is not None:
        handle_payment_event(provider, event)
    return RedirectResponse("/order/thanks", status_code=303)


# --- Job doběhl (volá app.job_worker v procesu jobu) -------------------------------------------


def notify_order_job(job: dict[str, Any]) -> None:
    """Hotový job objednávky: vydá náhledový token a pošle odkaz na stránku objednávky."""
    job_id = str(job["id"])
    order_id = job_order_id(job)
    if not order_id:
        return
    if any(ORDER_MAIL_SENT_PREFIX in (e.get("line") or "") for e in db.get_logs(job_id)):
        return
    try:
        order = orders.get_order(order_id)
    except KeyError:
        db.append_log(job_id, "CHYBA: objednávka jobu neexistuje – e-mail s náhledem neodeslán.")
        return
    email = str(order.get("email") or "")
    if not email:
        return
    token = orders.issue_preview_token(order_id)
    if not token:
        return
    lang = _lang(order.get("lang"))
    name = str(job.get("name") or "")
    base = _base_url(order.get("site"))
    hours = app_settings.UNPAID_ORDER_RETENTION_HOURS if app_settings.UNPAID_ORDER_RETENTION_HOURS > 0 else 168
    preview = order_preview_path(job_id, order.get("site"))
    sample = order_sample_path(job)
    attachments = [p for p in (preview, sample) if p is not None]
    body = i18n.t(
        "order.mail.ready.body",
        lang,
        name=name,
        url=f"{base}/order/{token}",
        price=price_label(lang),
        days=max(1, hours // 24),
    )
    if sample is not None:
        body += "\n\n" + i18n.t("order.mail.ready.sample", lang)
    try:
        send_mail(
            email,
            i18n.t("order.mail.ready.subject", lang, name=name),
            body,
            attachments=attachments or None,
        )
        db.append_log(job_id, f"{ORDER_MAIL_SENT_PREFIX}.")
    except MailError as exc:
        db.append_log(job_id, f"CHYBA odeslání e-mailu s náhledem: {exc}")
