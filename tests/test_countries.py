from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import countries


@pytest.mark.parametrize(
    "bbox, low, high",
    [
        ((14.40, 50.08, 14.42, 50.09), 0.999, 1.0),  # Praha
        ((16.35, 48.18, 16.40, 48.22), 0.0, 0.0),  # Vídeň (v obdélníku kolem ČR, mimo ČR)
        ((13.70, 51.03, 13.76, 51.07), 0.0, 0.0),  # Drážďany
        ((19.00, 50.24, 19.05, 50.28), 0.0, 0.0),  # Katovice
        ((12.06, 50.24, 12.14, 50.28), 0.3, 0.6),  # Aš: výřez přes hranici s Německem
    ],
)
def test_bbox_share(bbox, low, high):
    share = countries.bbox_share(bbox, countries.load_countries()["CZ"])
    assert low <= share <= high


def test_check_bbox_majority_rule():
    cz, share = countries.check_bbox((14.40, 50.08, 14.42, 50.09))
    assert cz.code == "CZ" and share > 0.99
    with pytest.raises(countries.CountryError, match="mimo podporované"):
        countries.check_bbox((16.35, 48.18, 16.40, 48.22))
    # Většina v Německu → odmítnout; většina v Česku → projde s přesahem.
    with pytest.raises(countries.CountryError, match="aspoň polovina"):
        countries.check_bbox((12.03, 50.24, 12.11, 50.28))
    cz, share = countries.check_bbox((12.09, 50.24, 12.17, 50.28))
    assert 0.5 <= share < 0.99
    assert countries.outside_share(share) > 0
    assert "jen z OSM" in countries.outside_note(cz, share)


def test_allowed_countries_by_site():
    assert [c.code for c in countries.allowed_countries(None)] == ["CZ"]
    assert [c.code for c in countries.allowed_countries(SimpleNamespace(countries=("*",)))] == ["CZ"]
    assert countries.allowed_countries(SimpleNamespace(countries=("AT",))) == []
    with pytest.raises(countries.CountryError, match="není pro tuto doménu"):
        countries.check_bbox((14.40, 50.08, 14.42, 50.09), None, "AT")


def test_api_coverage_and_sheets_outside(client):
    body = client.get("/api/coverage").json()
    assert [c["code"] for c in body["countries"]] == ["CZ"]
    assert body["countries"][0]["geometry"]["type"] == "Polygon"
    r = client.get("/api/sheets", params={"bbox": "16.35,48.18,16.40,48.22"})
    assert r.status_code == 400
    assert "mimo podporované území (Česko)" in r.json()["detail"]


def test_api_country_errors_in_english(client, monkeypatch):
    monkeypatch.setenv("LANGS", "cs,en")
    r = client.get("/api/sheets", params={"bbox": "16.35,48.18,16.40,48.22", "lang": "en"})
    assert r.status_code == 400
    assert "outside the supported territory (Czechia)" in r.json()["detail"]
    r = client.get("/api/sheets", params={"bbox": "12.03,50.24,12.11,50.28", "lang": "en"})
    assert "Only 12 % of the area lies in Czechia" in r.json()["detail"]


def test_create_job_country_and_border(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "query_sm5_sheets", lambda *a, **k: [{"mapnom": "ASXX00", "name": "Aš"}])
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "enqueue", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "queue_position", lambda *_a, **_k: 0)
    base = {"preset_id": "sprint_2m", "output_mode": "png_zip"}
    # Většina v Německu: odmítnout, i když výřez protne český list (podvržený v sm5_sheets).
    r = client.post("/api/jobs", data={**base, "name": "de", "bbox": "12.03,50.24,12.05,50.26", "sm5_sheets": "ASXX00"})
    assert r.status_code == 400
    # Většina v Česku: projde, přesah se uloží a zaloguje.
    r = client.post("/api/jobs", data={**base, "name": "as", "bbox": "12.09,50.24,12.11,50.255"})
    assert r.status_code == 200, r.text
    opts = r.json()["options"]
    assert opts["country"] == "CZ" and 0 < opts["outside_share"] < 0.5
    log = client.get(f"/api/jobs/{r.json()['id']}/log").json()["lines"]
    assert any("jen z OSM" in line["line"] for line in log)
    import app.db as db

    with db.connect() as conn:
        row = conn.execute("SELECT country FROM job_origin WHERE job_id = ?", (r.json()["id"],)).fetchone()
    assert row["country"] == "CZ"


def test_api_bbox_errors_in_english(client, monkeypatch):
    monkeypatch.setenv("LANGS", "cs,en")
    r = client.get("/api/sheets", params={"bbox": "nan,50.0,14.5,50.1", "lang": "en"})
    assert r.status_code == 400
    assert "non-numeric" in r.json()["detail"]
    r = client.get("/api/sheets", params={"bbox": "nan,50.0,14.5,50.1"})
    assert "nečíselnou" in r.json()["detail"]
