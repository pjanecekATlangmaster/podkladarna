"""Sonda zdrojů výřezu před jobem (app.area_probe)."""

from __future__ import annotations

import threading

import pytest

from app import area_probe, countries, i18n
from app.pipeline import elevation_raster

LILLE = (3.050, 50.625, 3.070, 50.640)
MARAIS = (2.350, 48.855, 2.362, 48.861)
LYON = (4.825, 45.754, 4.840, 45.762)
NANTES = (-1.560, 47.210, -1.545, 47.218)  # jen pro chybu služby (LILLE může držet běžící pomalou sondu)


@pytest.fixture
def fr(monkeypatch):
    monkeypatch.setattr(elevation_raster, "_COVERAGE_CACHE", {})
    return countries.load_countries()["FR"]


def _texts(result):
    return [i18n.localize(w, "cs") for w in result["warnings"]]


@pytest.mark.parametrize(
    "share, verdict, warn",
    [
        (1.0, "ok", None),
        (0.7, "warn", "70 %"),  # část výřezu bez výšek: varovat, nekreslit naslepo
        (0.1, "block", None),  # většina bez výšek: odmítnout (jako dřív /api/sheets)
    ],
)
def test_elevation_share_verdict(fr, monkeypatch, share, verdict, warn):
    monkeypatch.setitem(elevation_raster.COVERAGE, "fr_lidarhd", lambda _b: share)
    result = area_probe.probe(fr, MARAIS)
    assert result["verdict"] == verdict
    assert result["elevation"]["share"] == share
    if warn:
        assert any(warn in t and "LiDAR HD MNT a MNS" in t for t in _texts(result))  # česky = SOURCE_LABELS
        assert any("LiDAR HD MNT and MNS" in i18n.localize(w, "en") for w in result["warnings"])
        assert all(i18n.t(f"api.elevation_source.{k}", "cs") == v for k, v in elevation_raster.SOURCE_LABELS.items())
    if verdict == "block":
        assert "10 %" in i18n.localize(result["block"], "cs")


def test_slow_source_does_not_block(fr, monkeypatch):
    gate = threading.Event()

    def slow(_b):
        gate.wait(5)
        return 0.0

    monkeypatch.setitem(elevation_raster.COVERAGE, "fr_lidarhd", slow)
    try:
        result = area_probe.probe(fr, LILLE, deadline_s=0.05)
    finally:
        gate.set()
    assert result["block"] is None and result["elevation"]["status"] == "unknown"


def test_elevation_pending_and_unverified(fr, monkeypatch):
    """Nulové pokrytí s nasnímaným, ale nezveřejněným projektem má vlastní hlášku; chyba služby jen varuje."""
    monkeypatch.setitem(elevation_raster.COVERAGE, "fr_lidarhd", lambda _b: 0.0)
    monkeypatch.setitem(elevation_raster.COVERAGE_PENDING, "fr_lidarhd", lambda _b: ["NY_NHGaps_1_D24 (2025)"])
    result = area_probe.probe(fr, LYON)
    assert result["verdict"] == "block"
    assert "NY_NHGaps_1_D24 (2025)" in i18n.localize(result["block"], "en")
    assert "not been published" in i18n.localize(result["block"], "en")

    def broken(_b):
        raise OSError("timeout")

    monkeypatch.setattr(elevation_raster, "_COVERAGE_CACHE", {})
    monkeypatch.setitem(elevation_raster.COVERAGE, "fr_lidarhd", broken)
    result = area_probe.probe(fr, NANTES)
    assert result["block"] is None and result["elevation"]["status"] == "failed"
    assert any("nepodařilo ověřit" in t for t in _texts(result))


@pytest.mark.parametrize(
    "bbox, outside, key, share, needle",
    [
        (MARAIS, "fr_bdtopo", "fr_pdv", 1.0, None),  # Paříž: Plan de voirie celý výřez
        (LYON, "fr_bdtopo", "fr_bdtopo", 1.0, None),  # mimo Paříž BD TOPO
        (LYON, "", "fr_pdv", 0.0, "není technická mapa"),  # země bez náhradního zdroje: mimo území jen OSM
    ],
)
def test_technical_map_area(fr, monkeypatch, bbox, outside, key, share, needle):
    import dataclasses

    monkeypatch.setitem(elevation_raster.COVERAGE, "fr_lidarhd", lambda _b: 1.0)
    result = area_probe.probe(dataclasses.replace(fr, technical_map_outside=outside), bbox)
    assert result["technical_map"]["share"] == share and result["technical_map"]["key"] == key
    texts = _texts(result)
    assert (any(needle in t for t in texts)) if needle else not texts


def test_cz_dtm_sparse_units(monkeypatch):
    from app import dtm_coverage

    cz = countries.load_countries()["CZ"]
    monkeypatch.setattr(
        dtm_coverage,
        "coverage_cached",
        lambda _b: {
            "available": True,
            "level": "partial",
            "units": [{"name": "Řitka", "quality": "sparse"}, {"name": "Praha 5", "quality": "good"}],
        },
    )
    box = (14.30, 49.90, 14.31, 49.91)
    result = area_probe.probe(cz, box)
    assert result["verdict"] == "warn" and result["technical_map"]["level"] == "partial"
    assert any("Řitka" in t and "Praha 5" not in t for t in _texts(result))
    assert area_probe.probe(cz, box, include_dtm=False)["verdict"] == "ok"


def test_job_summary_and_log():
    result = {"verdict": "warn", "elevation": {"share": 0.7}, "technical_map": {"key": "fr_pdv", "share": 0.0},
              "warnings": [i18n.Msg("api.probe.techmap_none", source="fr_pdv")]}
    summary = area_probe.job_summary(result)
    assert summary["elevation_share"] == 0.7 and isinstance(summary["warnings"][0], str)
    assert area_probe.log_lines(summary)[0].startswith("Sonda výřezu: Ve výřezu není")
    assert area_probe.log_lines(None) == []


def test_api_sheets_and_job_store_probe(client, monkeypatch):
    import app.main as main

    monkeypatch.setenv("COUNTRIES_BETA", "FR")
    monkeypatch.setenv("SITES", "testserver=cs:free:*")  # beta jen na mezinárodní doméně
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setitem(elevation_raster.COVERAGE, "fr_lidarhd", lambda _b: 1.0)
    monkeypatch.setattr(elevation_raster, "_COVERAGE_CACHE", {})
    bbox = ",".join(str(v) for v in LYON)
    body = client.get("/api/sheets", params={"bbox": bbox}).json()
    probe = body["probe"]  # Lyon: mimo Paříž BD TOPO, celý výřez s technickou mapou
    assert probe["verdict"] == "ok" and probe["technical_map"] == {**probe["technical_map"], "key": "fr_bdtopo", "share": 1.0}
    monkeypatch.setattr(main.worker, "enqueue", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "queue_position", lambda *_a, **_k: 0)
    r = client.post("/api/jobs", data={"preset_id": "sprint_2m", "output_mode": "png", "name": "lyon", "bbox": bbox})
    assert r.status_code == 200
    stored = r.json()["options"]["area_probe"]
    assert stored["verdict"] == "ok" and stored["technical_map"]["share"] == 1.0


# (země, podíl výřezu v zemi, verdikt) – celoplošné národní výšky končí na hranici: přesah za ni varuje;
# Česko beze změny (podíl v polygonu sonda nebere).
@pytest.mark.parametrize(
    "code, inside, verdict",
    [("NL", 1.0, "ok"), ("NL", 0.7, "warn"), ("CZ", 0.7, "ok")],
)
def test_national_elevation_ends_at_border(monkeypatch, code, inside, verdict):
    monkeypatch.setattr(countries, "bbox_share", lambda _box, _c: inside)
    country = countries.load_countries()[code]
    result = area_probe.probe(country, (5.0, 52.0, 5.01, 52.01), include_dtm=False)
    assert result["verdict"] == verdict
    if verdict == "warn":
        assert result["elevation"]["share"] == 0.7
        assert "70 %" in i18n.localize(result["warnings"][0], "en")
