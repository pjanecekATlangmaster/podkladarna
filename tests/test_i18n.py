from __future__ import annotations

import json
import re

import pytest

from app import i18n


def test_catalogs_complete_and_placeholders_match():
    cs = i18n.catalog("cs")
    en = i18n.catalog("en")
    assert cs and en
    assert i18n.missing_keys(cs, en) == []
    for key, text in cs.items():
        assert i18n.placeholders_in(text) == i18n.placeholders_in(en[key]), key
        # HTML ve stránkových textech: stejné značky a id (app.js na ně sahá).
        markup = lambda x: sorted(re.findall(r'id="[^"]+"|</?[a-z]+', x))
        assert markup(text) == markup(en[key]), key


def test_html_placeholders_exist_in_source_catalog():
    cs = i18n.catalog("cs")
    for name in ("index.html", "licence.html", "podpora.html"):
        html = (i18n.CATALOG_DIR.parent.parent / "web" / name).read_text(encoding="utf-8")
        for key in re.findall(r"\{\{[th]:([a-zA-Z0-9_.\-]+)\}\}", html):
            assert key in cs, f"{name}: {key}"


def test_t_fallback_and_params(monkeypatch: pytest.MonkeyPatch):
    assert i18n.t("web.footer.support", "en") == "support development"
    assert i18n.t("web.footer.support", "xx") == "support development"
    assert i18n.t("neexistuje.klic", "en") == "neexistuje.klic"
    monkeypatch.setattr(i18n, "catalog", lambda lang: {"a": "{n} km² {x}"} if lang == "cs" else {})
    assert i18n.t("a", "cs", n=3) == "3 km² {x}"


class _Req:
    def __init__(self, host="podkladarna.kibos.link", query=None, cookies=None, accept=""):
        self.headers = {"host": host, "accept-language": accept}
        self.query_params = query or {}
        self.cookies = cookies or {}


@pytest.mark.parametrize(
    "env_sites, env_langs, req, expected",
    [
        ("", "", _Req(), ("cs", False)),
        ("", "", _Req(query={"lang": "en"}), ("en", True)),
        ("", "", _Req(query={"lang": "zz"}), ("cs", False)),
        ("", "", _Req(cookies={"lang": "en"}), ("en", False)),
        ("example.eu=en", "", _Req(host="example.eu:443"), ("en", False)),
        ("example.eu=en", "", _Req(host="example.eu", cookies={"lang": "cs"}), ("cs", False)),
        ("example.eu=en", "", _Req(host="evil.test", accept="en"), ("cs", False)),
        ("", "cs,en", _Req(host="10.0.0.1", accept="en-GB,en;q=0.9,cs;q=0.5"), ("en", False)),
        ("podkladarna.kibos.link=cs", "cs,en", _Req(accept="en"), ("cs", False)),
    ],
)
def test_resolve_lang(monkeypatch, env_sites, env_langs, req, expected):
    monkeypatch.setenv("SITES", env_sites)
    monkeypatch.setenv("LANGS", env_langs)
    assert i18n.resolve_lang(req) == expected


def test_sites_allowlist_only(monkeypatch):
    monkeypatch.setenv("SITES", "Example.EU=en, https://podkladarna.kibos.link/=cs, bad, x.test=zz")
    sites = i18n.sites()
    assert set(sites) == {"example.eu", "podkladarna.kibos.link"}
    assert sites["example.eu"].base_url == "https://example.eu"


def test_index_default_czech_without_switcher(client, monkeypatch):
    monkeypatch.delenv("SITES", raising=False)
    monkeypatch.delenv("LANGS", raising=False)
    res = client.get("/")
    assert res.status_code == 200
    assert '<html lang="cs"' in res.text
    assert "{{t:" not in res.text
    assert "podpořit vývoj" in res.text
    assert "lang-switch" not in res.text
    assert res.headers["cache-control"] == "private, no-store"
    assert "set-cookie" not in res.headers
    data = re.search(r"window\.I18N = (\{.*?\});</script>", res.text).group(1)
    payload = json.loads(data)
    assert payload["lang"] == "cs" and payload["locale"] == "cs-CZ"
    assert all(k.startswith(i18n.WEB_PREFIXES) for k in payload["messages"])


def test_index_english_via_query_sets_cookie(client, monkeypatch):
    monkeypatch.setenv("LANGS", "cs,en")
    res = client.get("/?lang=en")
    assert '<html lang="en"' in res.text
    assert "support development" in res.text
    assert 'class="lang-switch"' in res.text and 'href="?lang=cs"' in res.text
    assert "lang=en" in res.headers["set-cookie"]
    again = client.get("/licence")
    assert '<html lang="en"' in again.text


def test_site_default_and_hreflang(client, monkeypatch):
    monkeypatch.setenv("SITES", "podkladarna.kibos.link=cs,example.eu=en")
    res = client.get("/", headers={"host": "example.eu"})
    assert '<html lang="en"' in res.text
    assert 'hreflang="cs" href="https://podkladarna.kibos.link/"' in res.text
    assert '<link rel="canonical" href="https://example.eu/"' in res.text


def test_page_texts_rendered_server_side_only(client, monkeypatch):
    monkeypatch.setenv("LANGS", "cs,en")
    res = client.get("/?lang=en")
    assert "{{h:" not in res.text and "{{t:" not in res.text
    assert "Start generating" in res.text and "Spustit generování" not in res.text
    payload = json.loads(re.search(r"window\.I18N = (\{.*?\});</script>", res.text).group(1))
    assert not any(k.startswith("web.page.") for k in payload["messages"])
    cs = client.get("/?lang=cs")
    assert "Spustit generování" in cs.text
