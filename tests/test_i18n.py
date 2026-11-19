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


def test_github_links_only_in_czech_and_brand_configurable(client, monkeypatch):
    """Mezinárodní (anglická) verze neukazuje GitHub; název služby je jedna hodnota ``web.brand``."""
    monkeypatch.setenv("LANGS", "cs,en")
    cs = client.get("/?lang=cs")
    assert "github.com/pjanecekATlangmaster" in cs.text
    for path in ("/?lang=en", "/licence", "/podpora"):
        res = client.get(path)
        assert '<html lang="en"' in res.text
        assert "github.com/pjanecekATlangmaster" not in res.text, path
        assert "{brand}" not in res.text and "{{t:" not in res.text, path
    real = i18n.catalog

    def branded(lang):
        data = dict(real(lang))
        if lang == "en":
            data["web.brand"] = "Mapname"
        return data

    monkeypatch.setattr(i18n, "catalog", branded)
    assert i18n.t("mail.ready.subject", "en", name="X").startswith("Mapname: ")
    assert i18n.merged_catalog("en")["web.js.stats.app_version"] == "Mapname version"
    res = client.get("/?lang=en")
    assert "<title>Mapname</title>" in res.text and "Podkladárna" not in res.text
    assert "Podkladárna" in client.get("/?lang=cs").text


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


def test_mail_english_and_czech_unchanged(monkeypatch):
    from app import settings
    from app.mail import build_private_failed_email, build_private_ready_email, sender_name

    subject, body = build_private_ready_email(
        job_name="Les", download_url="https://x/d/t", retention_hours=48, area_km2=2.43, lang="en"
    )
    assert subject == "Omapic: map “Les” is ready to download"
    assert "Map area: 2.4 km²." in body and "valid for 48 hours" in body
    assert "Dobrý den" not in body
    subject_cs, body_cs = build_private_ready_email(
        job_name="Les", download_url="https://x/d/t", retention_hours=48, area_km2=2.43
    )
    assert subject_cs == "Podkladárna: mapa „Les“ je připravená ke stažení"
    assert "Plocha mapy: 2,4 km²." in body_cs
    # Podpis a odesílatel: anglicky značka webu, česky beze změny (SMTP_FROM_NAME).
    assert body.endswith("— Omapic\n") and sender_name("en") == "Omapic"
    assert body_cs.endswith(f"— {settings.SMTP_FROM_NAME or 'Podkladárna'}\n") and sender_name("cs") is None
    _, failed = build_private_failed_email(
        job_name="Les", error="boom", operator_details="log", lang="en"
    )
    assert "Reason: boom" in failed and "==== Pro provozovatele" in failed
    # Česky zadaný job z mezinárodní domény: předmět, podpis i odesílatel Omapic (Petr 2026-11-19).
    monkeypatch.setenv("SITES", "podkladarna.kibos.link=cs:free:CZ,example.eu=en:paid:*")
    with i18n.site_context(i18n.site_of_options({"site": "example.eu"})):
        subject_intl, body_intl = build_private_ready_email(job_name="Les", download_url="https://x/d/t", retention_hours=48)
        _, failed_intl = build_private_failed_email(job_name="Les", error="boom")
        assert subject_intl == "Omapic: mapa „Les“ je připravená ke stažení" and sender_name("cs") == "Omapic"
    assert body_intl.endswith("— Omapic\n") and failed_intl.endswith("— Omapic\n")
    assert "Podkladárn" not in body_intl + failed_intl
    with i18n.site_context(i18n.site_of_options({"site": "podkladarna.kibos.link"})):
        free_subject, free_body = build_private_ready_email(job_name="Les", download_url="https://x/d/t", retention_hours=48)
        assert free_subject == "Podkladárna: mapa „Les“ je připravená ke stažení" and sender_name("cs") is None
        assert free_body.endswith(f"— {settings.SMTP_FROM_NAME or 'Podkladárna'}\n")


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


@pytest.mark.parametrize(
    "path, english, czech",
    [
        ("/", "How Omapic evolved", "Jak se Podkladárna vyvíjela"),
        ("/licence", "Licence and sources", "Licence a zdroje"),
    ],
)
def test_pages_in_english(client, monkeypatch, path, english, czech):
    monkeypatch.setenv("SITES", "podkladarna.kibos.link=cs:free:CZ,example.eu=en:paid:*")
    en = client.get(path, headers={"host": "example.eu"}).text
    assert english in en and czech not in en and '<html lang="en"' in en
    assert "{{" not in en
    cs = client.get(path + "?lang=en", headers={"host": "podkladarna.kibos.link"}).text
    assert czech in cs and '<html lang="cs"' in cs


def test_paid_domain_without_donation_family_and_czech_data(client, monkeypatch):
    """Placená doména (Petr 2026-11-16): dar jen na bezplatné doméně, bez „Z jedné rodiny“, zdroje dat jen na /licence."""
    monkeypatch.setenv("SITES", "podkladarna.kibos.link=cs:free:CZ,example.eu=en:paid:*")
    en = client.get("/", headers={"host": "example.eu"}).text
    for gone in ("/podpora", "footer-family", "stavitel.kibos.link", "geoportal.cuzk.gov.cz", "support-note"):
        assert gone not in en, gone
    assert 'href="/licence"' in en and "Karttapullautin" in en and "openstreetmap.org/copyright" in en
    assert "{{" not in en
    redirect = client.get("/podpora", headers={"host": "example.eu"}, follow_redirects=False)
    assert redirect.status_code == 302 and redirect.headers["location"] == "/"
    cs = client.get("/", headers={"host": "podkladarna.kibos.link"}).text
    for kept in ('href="/podpora"', "footer-family", "stavitel.kibos.link", "geoportal.cuzk.gov.cz", "support-note"):
        assert kept in cs, kept
    assert "Číslo účtu" in client.get("/podpora", headers={"host": "podkladarna.kibos.link"}).text


def test_licence_lists_sources_of_offered_countries(client, monkeypatch):
    """Anglická licence: citace a zdroje zemí, které doména nabízí; česká stránka beze změny."""
    monkeypatch.setenv("SITES", "podkladarna.kibos.link=cs:free:CZ,example.eu=en:paid:*")
    monkeypatch.setenv("COUNTRIES_BETA", "CH")
    en = client.get("/licence", headers={"host": "example.eu"}).text
    assert "<h3>Switzerland</h3>" in en and "© swisstopo · AV cantons (geodienste.ch)" in en
    assert "PODKLADARNA_COUNTRY_SOURCES" not in en and "<h3>Austria</h3>" not in en  # AT bez bety ne
    cs = client.get("/licence", headers={"host": "podkladarna.kibos.link"}).text
    assert "swisstopo" not in cs and "PODKLADARNA_COUNTRY_SOURCES" not in cs


def test_api_errors_follow_page_language(client, monkeypatch):
    monkeypatch.setenv("SITES", "podkladarna.kibos.link=cs:free:CZ,example.eu=en:paid:*")
    en = client.get("/api/jobs/neexistuje", headers={"host": "example.eu"})
    assert en.status_code == 404
    assert en.json() == {"detail": "Job not found", "code": "api.job_not_found"}
    # Neošetřená výjimka: anglicky obecná hláška, česky text výjimky beze změny.
    import asyncio
    import json as _json

    from starlette.requests import Request

    import app.main as main

    def _500(host: str) -> dict:
        req = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                       "headers": [(b"host", host.encode())]})
        return _json.loads(asyncio.run(main.unhandled_exception_handler(req, RuntimeError("Chyba při čtení"))).body)

    assert _500("example.eu")["code"] == "api.internal_error"
    assert _500("podkladarna.kibos.link") == {"detail": "Chyba při čtení"}
    cs = client.get("/api/jobs/neexistuje", headers={"host": "podkladarna.kibos.link"})
    assert cs.json()["detail"] == "Job nenalezen"
    form = {"name": "x", "preset_id": "sprint_2m", "bbox": "14.40,50.08,14.44,50.10"}
    big = client.post("/api/jobs", data=form, headers={"host": "example.eu"}).json()
    assert big["code"] == "api.hint.sprint_area" and "can only be generated" in big["detail"]


def test_msg_formats_numbers_by_language():
    msg = i18n.Msg("api.job.bbox_too_big", width=5.25, height=4.0, area=21.0, max=25.0)
    assert str(msg) == "Výřez je moc velký (5,2 × 4 km, 21 km²; max 25 km²)."
    assert msg.render("en") == "The area is too large (5.2 × 4 km, 21 km²; max 25 km²)."
    assert i18n.localize({"a": [msg]}, "en") == {"a": [msg.render("en")]}


def test_sample_zips_listed_only_when_present(client, monkeypatch, tmp_path):
    """Ukázkové ZIPy: odkaz jen u souboru, který na serveru je, v jazyce stránky; jiná jména 404."""
    from app import settings as app_settings

    monkeypatch.setattr(app_settings, "SAMPLES_DIR", tmp_path)
    monkeypatch.setenv("LANGS", "cs,en")
    assert "about-samples" not in client.get("/?lang=cs").text
    (tmp_path / "sprint.zip").write_bytes(b"PK" + b"0" * 1_500_000)
    (tmp_path / "les-en.zip").write_bytes(b"PK")
    cs = client.get("/?lang=cs").text
    assert 'href="/samples/sprint.zip"' in cs and "sprint (Barrandov)" in cs and "1,5 MB" in cs
    assert "les-en.zip" not in cs
    en = client.get("/?lang=en").text
    assert 'href="/samples/les-en.zip"' in en and "sprint.zip" not in en
    res = client.get("/samples/sprint.zip")
    assert res.status_code == 200 and "podkladarna-ukazka-sprint.zip" in res.headers["content-disposition"]
    assert "omapic-sample-les.zip" in client.get("/samples/les-en.zip").headers["content-disposition"]
    for bad in ("/samples/mtbo.zip", "/samples/..%2Fpodkladarna.db", "/samples/x.zip", "/samples/reference.zip"):
        assert client.get(bad).status_code == 404
    # Mezinárodní stránka: česká reference jen v angličtině a pravdivé popisky ze samples.json.
    (tmp_path / "reference-en.zip").write_bytes(b"PK")
    (tmp_path / "reference.zip").write_bytes(b"PK")
    (tmp_path / "samples.json").write_text('{"les-en.zip": "forest – Tallinn <EE> {{x}}"}', encoding="utf-8")
    en = client.get("/?lang=en").text
    assert "reference: Czech Republic" in en and "forest – Tallinn &lt;EE&gt; x" in en
    assert "reference" not in client.get("/?lang=cs").text.split("about-samples")[1].split("</p>")[0]
    assert "omapic-sample-reference.zip" in client.get("/samples/reference-en.zip").headers["content-disposition"]


def test_intl_site_czech_uses_intl_brand_and_texts(client, monkeypatch):
    """Česky na mezinárodní doméně (Omapic): název domény a texty jako v angličtině; česká doména beze změny
    (Petr 2026-11-19)."""
    monkeypatch.setenv("SITES", "pt.kibos.link=cs:free:CZ,example.eu=en:paid:*")
    monkeypatch.setenv("LANGS", "cs,en")
    for path in ("/?lang=cs", "/licence?lang=cs"):
        client.cookies.clear()
        res = client.get(path, headers={"host": "example.eu"})
        assert '<html lang="cs"' in res.text, path
        assert "Omapic" in res.text, path
        assert not re.search(r"Podkladárn|Petr", res.text), path
        assert "{brand}" not in res.text and "{{t:" not in res.text, path
    payload = json.loads(re.search(r"window\.I18N = (\{.*?\});</script>", client.get(
        "/?lang=cs", headers={"host": "example.eu"}).text).group(1))
    assert payload["messages"]["web.brand"] == "Omapic"
    assert payload["messages"]["web.js.feedback.sent"] == "Díky — zpráva je u autora."
    assert not any(k.endswith("_intl") for k in payload["messages"])
    client.cookies.clear()
    free = client.get("/", headers={"host": "pt.kibos.link"})
    assert "<title>Podkladárna</title>" in free.text and "Omapic" not in free.text
    free_payload = json.loads(re.search(r"window\.I18N = (\{.*?\});</script>", free.text).group(1))
    assert free_payload["messages"]["web.js.feedback.sent"] == "Díky — zpráva je u Petra."
    assert not any(k.endswith("_intl") for k in free_payload["messages"])
    # Mimo požadavek (job, e-mail) se nic nemění.
    assert i18n.brand("cs") == "Podkladárna"
