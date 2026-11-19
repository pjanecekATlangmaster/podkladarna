"""Dobrovolné dary přes PayPal na mezinárodní doméně (app.donations, Petr 2026-11-19)."""

from __future__ import annotations

import pytest

from app import donations, settings
from app.mail import build_private_ready_email

SITES = "podkladarna.kibos.link=cs:free:CZ,example.eu=en:paid:*"
MERCHANT = "ABCDEFGH12345"


@pytest.fixture
def donate_env(monkeypatch):
    monkeypatch.setenv("SITES", SITES)
    monkeypatch.setenv("DONATE_PAYPAL_BUSINESS", MERCHANT)


@pytest.mark.parametrize(
    "country, currency",
    [("GB", "GBP"), ("CH", "CHF"), ("PL", "PLN"), ("CZ", "CZK"), ("NL", "EUR"), ("", "EUR"), (None, "EUR")],
)
def test_currency_by_map_country(country, currency):
    assert donations.currency_for_country(country) == currency


@pytest.mark.parametrize(
    "options, expected",
    [
        ({"site": "example.eu", "country": "GB"}, "currency_code=GBP"),
        ({"site": "example.eu", "country": "NL"}, "currency_code=EUR"),
        ({"site": "podkladarna.kibos.link", "country": "CZ"}, None),  # česká verze beze změny
        ({"country": "CZ"}, None),  # bez SITES / domény
        ({"site": "example.eu", "country": "GB", "order_id": "o1"}, None),  # objednávka placené mapy
    ],
)
def test_donate_url_for_job(donate_env, options, expected):
    url = donations.donate_url_for_job(options, "en")
    if expected is None:
        assert url is None
    else:
        assert url.startswith("https://www.paypal.com/donate/?business=" + MERCHANT)
        assert expected in url


@pytest.mark.parametrize("value", ["", "petr@example.com", "https://evil.example/x"])
def test_invalid_merchant_id_disables_donation(monkeypatch, value):
    monkeypatch.setenv("SITES", SITES)
    monkeypatch.setenv("DONATE_PAYPAL_BUSINESS", value)
    assert donations.donate_url_for_job({"site": "example.eu", "country": "GB"}, "en") is None


def test_no_donation_while_sale_runs(donate_env, monkeypatch):
    monkeypatch.setattr(settings, "PAYMENTS_PROVIDER", "fake")
    monkeypatch.setattr(settings, "PAYMENTS_FAKE_ALLOWED", True)
    assert donations.donate_url_for_job({"site": "example.eu", "country": "GB"}, "en") is None


def test_ready_mail_donation_line_only_with_url():
    common = dict(job_name="Les", download_url="https://x/d/t", retention_hours=48, lang="en")
    _, plain = build_private_ready_email(**common)
    _, with_url = build_private_ready_email(**common, donate_url="https://www.paypal.com/donate/?business=X")
    assert "PayPal" not in plain
    assert "voluntary contribution" in with_url and "https://www.paypal.com/donate/?business=X" in with_url
    assert with_url.rstrip().endswith(plain.rstrip().splitlines()[-1])  # podpis zůstává na konci


def test_international_site_shows_paypal_czech_unchanged(client, monkeypatch):
    monkeypatch.setenv("SITES", SITES)
    cs_before = client.get("/podpora", headers={"host": "podkladarna.kibos.link"}).text
    index_cs_before = client.get("/", headers={"host": "podkladarna.kibos.link"}).text
    monkeypatch.setenv("DONATE_PAYPAL_BUSINESS", MERCHANT)

    en = client.get("/", headers={"host": "example.eu"}).text
    assert 'href="/podpora"' in en and "support-note" in en
    assert "footer-family" not in en and "{{" not in en
    page = client.get("/podpora", headers={"host": "example.eu"})
    assert page.status_code == 200
    html = page.text
    assert f"https://www.paypal.com/donate/?business={MERCHANT}&amp;currency_code=EUR" in html
    assert "currency_code=GBP" in html and '<html lang="en"' in html
    assert "podpora-qr.svg" not in html and "{{" not in html and "<script" not in html

    assert client.get("/podpora", headers={"host": "podkladarna.kibos.link"}).text == cs_before
    index_cs = client.get("/", headers={"host": "podkladarna.kibos.link"}).text
    assert index_cs == index_cs_before and "paypal" not in index_cs.lower()


@pytest.mark.parametrize(
    "link, ok",
    [("https://buy.stripe.com/test_abc123", True), ("https://evil.example/buy.stripe.com/x", False), ("http://buy.stripe.com/x", False)],
)
def test_stripe_link_alone(monkeypatch, link, ok):
    monkeypatch.setenv("SITES", SITES)
    monkeypatch.delenv("DONATE_PAYPAL_BUSINESS", raising=False)
    monkeypatch.setenv("DONATE_STRIPE_LINK", link)
    url = donations.donate_url_for_job({"site": "example.eu", "country": "GB"}, "en")
    assert url == (link if ok else None)
    assert ("Stripe" in donations.stripe_block_html("en")) == ok


EXAMPLE_IBAN = "DE89370400440532013000"  # vzorový IBAN z normy, ne skutečný účet


@pytest.fixture
def sepa_env(monkeypatch):
    monkeypatch.setenv("SITES", SITES)
    monkeypatch.delenv("DONATE_PAYPAL_BUSINESS", raising=False)
    monkeypatch.delenv("DONATE_STRIPE_LINK", raising=False)
    monkeypatch.setenv("DONATE_SEPA_IBAN", "de89 3704 0044 0532 0130 00")
    monkeypatch.setenv("DONATE_SEPA_NAME", "Jan  Novak")
    monkeypatch.setenv("DONATE_SEPA_BIC", "COBADEFFXXX")


@pytest.mark.parametrize(
    "iban, ok",
    [(EXAMPLE_IBAN, True), ("DE89370400440532013001", False), ("petr@example.com", False), ("", False)],
)
def test_sepa_iban_checksum(sepa_env, monkeypatch, iban, ok):
    monkeypatch.setenv("DONATE_SEPA_IBAN", iban)
    assert donations.sepa_enabled() == ok


def test_epc_payload_and_page_block(sepa_env):
    payload = donations.epc_payload("en").split("\n")
    assert payload[:7] == ["BCD", "002", "1", "SCT", "COBADEFFXXX", "Jan Novak", EXAMPLE_IBAN]
    assert payload[7] == ""  # částku zadá dárce
    html = donations.sepa_block_html("en")
    assert "<svg" in html and "DE89 3704 0044 0532 0130 00" in html and "Jan Novak" in html


@pytest.mark.parametrize(
    "stripe, expected",
    [("", "https://example.eu/podpora"), ("https://buy.stripe.com/test_abc123", "https://example.eu/podpora")],
)
def test_mail_links_support_page_when_sepa(sepa_env, monkeypatch, stripe, expected):
    monkeypatch.setenv("DONATE_STRIPE_LINK", stripe)
    assert donations.donate_url_for_job({"site": "example.eu", "country": "NL"}, "en") == expected


def test_support_page_shows_only_configured_methods(client, sepa_env, monkeypatch):
    monkeypatch.setenv("DONATE_STRIPE_LINK", "https://buy.stripe.com/test_abc123")
    html = client.get("/podpora", headers={"host": "example.eu"}).text
    assert "buy.stripe.com/test_abc123" in html and "<svg" in html and EXAMPLE_IBAN[:4] in html
    assert "paypal.com" not in html and "{{" not in html and "<script" not in html


def test_czech_page_language_adds_czech_qr_platba(client, sepa_env):
    cs = client.get("/podpora?lang=cs", headers={"host": "example.eu"}).text
    en = client.get("/podpora?lang=en", headers={"host": "example.eu"}).text
    assert "/static/podpora-qr.svg" in cs and "<svg" in cs  # česká QR Platba i EPC QR
    assert "/static/podpora-qr.svg" not in en and "<svg" in en
