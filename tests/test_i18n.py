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
        ("example.eu=en:paid:*", "", _Req(host="example.eu", cookies={"lang": "cs"}), ("cs", False)),
        ("example.eu=en:paid:*", "", _Req(host="example.eu", query={"lang": "cs"}), ("cs", True)),
        # Bezplatná doména má pevný jazyk: ?lang ani cookie ho nemění.
        ("podkladarna.kibos.link=cs:free:CZ", "cs,en", _Req(query={"lang": "en"}), ("cs", False)),
        ("podkladarna.kibos.link=cs", "cs,en", _Req(cookies={"lang": "en"}), ("cs", False)),
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
    monkeypatch.setenv(
        "SITES",
        "Example.EU=en:paid:*, https://podkladarna.kibos.link/=cs, bad, x.test=zz, y.test=en:gold",
    )
    sites = i18n.sites()
    assert set(sites) == {"example.eu", "podkladarna.kibos.link"}
    assert sites["example.eu"].base_url == "https://example.eu"
    assert (sites["example.eu"].mode, sites["example.eu"].countries) == ("paid", ("*",))
    free = sites["podkladarna.kibos.link"]
    assert (free.mode, free.countries, free.fixed_lang) == ("free", ("CZ",), True)
    assert i18n.site_base_url("EXAMPLE.eu") == "https://example.eu"
    assert i18n.site_base_url("evil.test") is None and i18n.site_base_url(None) is None


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


def test_free_site_has_no_switcher(client, monkeypatch):
    monkeypatch.setenv("SITES", "podkladarna.kibos.link=cs:free:CZ,example.eu=en:paid:*")
    monkeypatch.setenv("LANGS", "cs,en")
    free = client.get("/?lang=en", headers={"host": "podkladarna.kibos.link"})
    assert '<html lang="cs"' in free.text and "lang-switch" not in free.text
    assert "set-cookie" not in free.headers
    paid = client.get("/", headers={"host": "example.eu"})
    assert '<html lang="en"' in paid.text and 'class="lang-switch"' in paid.text


def test_site_default_and_hreflang(client, monkeypatch):
    monkeypatch.setenv("SITES", "podkladarna.kibos.link=cs,example.eu=en:paid:*")
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


def test_mail_english_and_czech_unchanged():
    from app.mail import build_private_failed_email, build_private_ready_email

    subject, body = build_private_ready_email(
        job_name="Les", download_url="https://x/d/t", retention_hours=48, area_km2=2.43, lang="en"
    )
    assert subject == "Podkladárna: map “Les” is ready to download"
    assert "Map area: 2.4 km²." in body and "valid for 48 hours" in body
    assert "Dobrý den" not in body
    subject_cs, body_cs = build_private_ready_email(
        job_name="Les", download_url="https://x/d/t", retention_hours=48, area_km2=2.43
    )
    assert subject_cs == "Podkladárna: mapa „Les“ je připravená ke stažení"
    assert "Plocha mapy: 2,4 km²." in body_cs
    _, failed = build_private_failed_email(
        job_name="Les", error="boom", operator_details="log", lang="en"
    )
    assert "Reason: boom" in failed and "==== Pro provozovatele" in failed


def test_job_links_use_site_of_job(monkeypatch):
    from app import job_worker

    monkeypatch.setenv("SITES", "podkladarna.kibos.link=cs,example.eu=en:paid:*")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://podkladarna.kibos.link")
    assert job_worker.job_page_url("abc", {"site": "example.eu"}) == "https://example.eu/?job=abc"
    assert job_worker.job_page_url("abc", {"site": "evil.test"}) == "https://podkladarna.kibos.link/?job=abc"
    assert job_worker.job_page_url("abc") == "https://podkladarna.kibos.link/?job=abc"
    assert job_worker._job_lang({"lang": "en"}) == "en"
    assert job_worker._job_lang({"lang": "zz"}) == "cs" and job_worker._job_lang(None) == "cs"


def test_created_job_remembers_lang_and_site(client, monkeypatch):
    import app.db as db
    import app.main as main

    monkeypatch.setenv("SITES", "podkladarna.kibos.link=cs,example.eu=en:paid:*")
    monkeypatch.setattr(main, "query_sm5_sheets", lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}])
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "enqueue", lambda job_id: None)
    form = {"name": "x", "preset_id": "sprint_2m", "bbox": "14.40,50.08,14.42,50.09"}
    r = client.post("/api/jobs", data=form, headers={"host": "example.eu"})
    assert r.status_code == 200, r.text
    opts = db.get_job(r.json()["id"])["options"]
    assert (opts["lang"], opts["site"]) == ("en", "example.eu")
    r2 = client.post("/api/jobs", data={**form, "name": "y", "bbox": "14.43,50.08,14.45,50.09"}, headers={"host": "evil.test"})
    assert r2.status_code == 200 and r2.json()["id"] != r.json()["id"]
    opts2 = db.get_job(r2.json()["id"])["options"]
    assert opts2["lang"] == "cs" and "site" not in opts2
    with db.connect() as conn:
        origin = {
            row["job_id"]: (row["site"], row["lang"])
            for row in conn.execute("SELECT job_id, site, lang FROM job_origin")
        }
    assert origin[r.json()["id"]] == ("example.eu", "en")
    assert origin[r2.json()["id"]] == (None, "cs")
