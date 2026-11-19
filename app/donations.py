"""Dobrovolné dary přes PayPal na mezinárodní doméně (Omapic; Petr 2026-11-19).

Jen odkaz na stránku PayPal Donate (``paypal.com/donate``), žádný skript PayPalu na webu, žádné webhooky
a nic se u nás neukládá. Měna se předvyplní podle země výřezu; částku zadá dárce sám (jako Purple Pen).

Zapnutí: ``DONATE_PAYPAL_BUSINESS`` = PayPal Merchant ID (ID účtu, ne e-mail) v ``.env``; volitelně
``DONATE_STRIPE_LINK`` = Stripe Payment Link s volitelnou částkou (další tlačítko, bez PayPalu i do e-mailu). Dar se ukáže jen na
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
    if not (paypal_business() or stripe_link()):
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
    """Odkaz do e-mailu hotové mapy (měna podle země výřezu), nebo None mimo doménu s darem."""
    if not enabled_for_job(options):
        return None
    return donate_url(currency_for_country((options or {}).get("country")), lang) or stripe_link() or None


def page_buttons_html(lang: str) -> str:
    """Tlačítka měn pro stránku /podpora (každé je obyčejný odkaz na PayPal)."""
    items = []
    for code in PAGE_CURRENCIES:
        url = donate_url(code, lang)
        if url:
            href = url.replace("&", "&amp;")
            items.append(
                f'<a class="btn donate-btn" href="{href}" target="_blank" rel="noopener">{code}</a>'
            )
    link = stripe_link()
    if link:
        label = html_escape(i18n.t("donate.page.stripe_button", lang))
        items.append(f'<a class="btn donate-btn" href="{link}" target="_blank" rel="noopener">{label}</a>')
    return "\n        ".join(items)
