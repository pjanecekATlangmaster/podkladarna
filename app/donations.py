"""Dobrovolné dary přes PayPal na mezinárodní doméně (Omapic; Petr 2026-11-19).

Jen odkaz na stránku PayPal Donate (``paypal.com/donate``), žádný skript PayPalu na webu, žádné webhooky
a nic se u nás neukládá. Měna se předvyplní podle země výřezu; částku zadá dárce sám (jako Purple Pen).

Zapnutí (jen ``.env``, nikdy v repozitáři), každá možnost zvlášť: ``DONATE_PAYPAL_BUSINESS`` = PayPal Merchant ID
(ID účtu, ne e-mail); ``DONATE_STRIPE_LINK`` = Stripe Payment Link s volitelnou částkou; ``DONATE_SEPA_IBAN`` +
``DONATE_SEPA_NAME`` (volitelně ``DONATE_SEPA_BIC``) = převod v EUR s EPC QR kódem (knihovna ``segno``). Dar se ukáže jen na
doméně ze ``SITES`` s jinými zeměmi než Česko (česká Podkladárna má vlastní stránku s QR platbou beze změny)
a jen tam, kde neběží prodej (``paid`` + ``PAYMENTS_PROVIDER``) – placená mapa výzvu k daru nedostane.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from html import escape as html_escape

from app import i18n

PAYPAL_DONATE_URL = "https://www.paypal.com/donate/"
DEFAULT_CURRENCY = "EUR"
# Měna dárce podle země výřezu (jen měny, které PayPal přijímá); ostatní země EUR.
CURRENCY_BY_COUNTRY = {
    "CZ": "CZK",
    "GB": "GBP",
    "CH": "CHF",
    "PL": "PLN",
    "SE": "SEK",
    "NO": "NOK",
    "DK": "DKK",
    "HU": "HUF",
}
# Tlačítka na stránce /podpora (pořadí podle Purple Pen: hlavní měny, pak ostatní evropské).
PAGE_CURRENCIES = ("EUR", "GBP", "USD", "CHF", "CZK", "PLN", "SEK", "NOK", "DKK")
# Merchant ID PayPalu: 13 znaků A–Z a 0–9; nic jiného (e-mail, URL) do odkazu nepustit.
_MERCHANT_ID_RE = re.compile(r"^[A-Z0-9]{8,20}$")


def paypal_business() -> str:
    """Merchant ID z ``DONATE_PAYPAL_BUSINESS`` (čte se živě kvůli testům); neplatné = vypnuto."""
    value = os.environ.get("DONATE_PAYPAL_BUSINESS", "").strip().upper()
    return value if _MERCHANT_ID_RE.match(value) else ""


_STRIPE_LINK_RE = re.compile(r"^https://(buy|donate)\.stripe\.com/[A-Za-z0-9_]+$")


def stripe_link() -> str:
    """Stripe Payment Link z ``DONATE_STRIPE_LINK``; jiná adresa než Stripe = vypnuto."""
    value = os.environ.get("DONATE_STRIPE_LINK", "").strip()
    return value if _STRIPE_LINK_RE.match(value) else ""


_IBAN_RE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")
_BIC_RE = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")


def _iban_valid(iban: str) -> bool:
    if not _IBAN_RE.match(iban):
        return False
    digits = "".join(str(int(c, 36)) for c in iban[4:] + iban[:4])
    return int(digits) % 97 == 1


def sepa_iban() -> str:
    """IBAN pro převod v EUR z ``DONATE_SEPA_IBAN`` (jen .env, nikdy v repozitáři); neplatný = vypnuto."""
    value = re.sub(r"\s+", "", os.environ.get("DONATE_SEPA_IBAN", "")).upper()
    return value if _iban_valid(value) else ""


def sepa_name() -> str:
    """Jméno příjemce z ``DONATE_SEPA_NAME`` (EPC QR nejvýš 70 znaků, jeden řádek)."""
    return " ".join(os.environ.get("DONATE_SEPA_NAME", "").split())[:70]


def sepa_bic() -> str:
    value = re.sub(r"\s+", "", os.environ.get("DONATE_SEPA_BIC", "")).upper()
    return value if _BIC_RE.match(value) else ""


def sepa_enabled() -> bool:
    return bool(sepa_iban() and sepa_name())


def currency_for_country(country: str | None) -> str:
    return CURRENCY_BY_COUNTRY.get((country or "").strip().upper(), DEFAULT_CURRENCY)


def _sale_active(site: i18n.Site) -> bool:
    if site.mode != "paid":
        return False
    from app import payments

    return payments.payments_enabled()


def enabled_for_site(site: i18n.Site | None) -> bool:
    """Dar jen na mezinárodní doméně ze ``SITES``, se zadaným Merchant ID a bez běžícího prodeje."""
    if site is None or tuple(site.countries) == ("CZ",):
        return False
    if not (paypal_business() or stripe_link() or sepa_enabled()):
        return False
    return not _sale_active(site)


def enabled_for_request(request) -> bool:
    return enabled_for_site(i18n.site_for_request(request))


def enabled_for_job(options: dict | None) -> bool:
    """Job zadaný na doméně s darem (``options.site``), ne objednávka placené mapy."""
    options = options or {}
    if options.get("order_id"):
        return False
    site = i18n.sites().get(str(options.get("site") or "").strip().lower())
    return enabled_for_site(site)


def donate_url(currency: str = DEFAULT_CURRENCY, lang: str = i18n.BASE_LANG) -> str:
    """Odkaz na PayPal Donate s předvyplněnou měnou; prázdné bez Merchant ID."""
    business = paypal_business()
    if not business:
        return ""
    params = {
        "business": business,
        "currency_code": (currency or DEFAULT_CURRENCY).upper(),
        "item_name": i18n.t("donate.paypal_item", lang),
    }
    return PAYPAL_DONATE_URL + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def donate_url_for_job(options: dict | None, lang: str) -> str | None:
    """Odkaz do e-mailu hotové mapy, nebo None mimo doménu s darem.

    Jediná možnost daru = přímý odkaz (PayPal s měnou podle země výřezu, nebo Stripe); víc možností, SEPA
    nebo čeština = stránka /podpora na doméně zadání (QR převodu v e-mailu nejde naskenovat)."""
    if not enabled_for_job(options):
        return None
    options = options or {}
    methods = [bool(paypal_business()), bool(stripe_link()), sepa_enabled()]
    if lang != i18n.SOURCE_LANG and sum(methods) == 1 and not sepa_enabled():
        return donate_url(currency_for_country(options.get("country")), lang) or stripe_link() or None
    # Česky jen česká QR Platba na stránce podpory (Petr 2026-11-19), jinak výběr možností; jazyk stránky
    # podle jazyka zadání, ať dárce bez cookie nedostane výchozí jazyk domény.
    base = i18n.site_base_url(options.get("site"))
    return f"{base}/podpora?lang={lang}" if base else None


def _btn(href: str, label: str) -> str:
    return f'<a class="btn donate-btn" href="{html_escape(href)}" target="_blank" rel="noopener">{html_escape(label)}</a>'


def paypal_block_html(lang: str) -> str:
    """Tlačítka měn PayPal pro stránku /podpora (každé je obyčejný odkaz); prázdné bez Merchant ID."""
    if not paypal_business():
        return ""
    buttons = "\n      ".join(_btn(donate_url(code, lang), code) for code in PAGE_CURRENCIES)
    return (
        f'<h2>{html_escape(i18n.t("donate.page.paypal_title", lang))}</h2>\n'
        f'    <p>{html_escape(i18n.t("donate.page.choose", lang))}</p>\n'
        f'    <div class="donate-buttons">\n      {buttons}\n    </div>\n'
        f'    <p class="support-hint">{html_escape(i18n.t("donate.page.paypal_note", lang))}</p>\n'
    )


def stripe_block_html(lang: str) -> str:
    link = stripe_link()
    if not link:
        return ""
    return (
        f'<h2>{html_escape(i18n.t("donate.page.stripe_title", lang))}</h2>\n'
        f'    <div class="donate-buttons">{_btn(link, i18n.t("donate.page.stripe_button", lang))}</div>\n'
        f'    <p class="support-hint">{html_escape(i18n.t("donate.page.stripe_note", lang))}</p>\n'
    )


def epc_payload(lang: str) -> str:
    """Obsah EPC QR (EPC069-12, verze 002, UTF-8, převod SCT v EUR bez částky – zadá ji dárce)."""
    return "\n".join(
        ["BCD", "002", "1", "SCT", sepa_bic(), sepa_name(), sepa_iban(), "", "", "",
         i18n.t("donate.sepa_remittance", lang)[:140], ""]
    )


def _qr_svg(payload: str) -> str:
    try:
        import segno
    except ImportError:  # bez knihovny jen údaje k převodu
        return ""
    return segno.make(payload, error="m", micro=False).svg_inline(scale=4, border=2, omitsize=False)


def _iban_groups(iban: str) -> str:
    return " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))


_CZ_PAY_RE = re.compile(r'<div class="support-pay">.*?</div>\s*<p class="support-hint">.*?</p>', re.S)


def czech_qr_block_html(lang: str) -> str:
    """V češtině klasická QR Platba na český účet – týž blok jako stránka podpory české Podkladárny
    (``web/podpora.html``, Petr 2026-11-19); jiné jazyky nic."""
    if lang != i18n.SOURCE_LANG:
        return ""
    from app.settings import APP_ROOT

    try:
        source = (APP_ROOT / "web" / "podpora.html").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = _CZ_PAY_RE.search(source)
    if not m:
        return ""
    return f'<h2>{html_escape(i18n.t("donate.page.cz_qr_title", lang))}</h2>\n    {m.group(0)}\n'


def sepa_block_html(lang: str) -> str:
    """EPC QR a údaje k převodu v EUR pro stránku /podpora; prázdné bez ``DONATE_SEPA_IBAN`` / jména."""
    if not sepa_enabled():
        return ""
    qr = _qr_svg(epc_payload(lang))
    rows = [
        ("donate.page.sepa_name", sepa_name()),
        ("donate.page.sepa_iban", _iban_groups(sepa_iban())),
    ]
    if sepa_bic():
        rows.append(("donate.page.sepa_bic", sepa_bic()))
    rows.append(("donate.page.sepa_message", i18n.t("donate.sepa_remittance", lang)[:140]))
    dl = "".join(
        f"<dt>{html_escape(i18n.t(k, lang))}</dt><dd><strong>{html_escape(v)}</strong></dd>" for k, v in rows
    )
    qr_html = f'<div class="support-qr donate-qr">{qr}</div>' if qr else ""
    return (
        f'<h2>{html_escape(i18n.t("donate.page.sepa_title", lang))}</h2>\n'
        f'    <div class="support-pay">{qr_html}<dl class="support-account">{dl}</dl></div>\n'
        f'    <p class="support-hint">{html_escape(i18n.t("donate.page.sepa_hint", lang))}</p>\n'
    )
