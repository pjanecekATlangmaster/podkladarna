"""Sonda zdrojů výřezu před jobem (app.area_probe)."""

from __future__ import annotations

import threading

import pytest

from app import area_probe, countries, i18n
from app.pipeline import elevation_raster

LILLE = (3.050, 50.625, 3.070, 50.640)
MARAIS = (2.350, 48.855, 2.362, 48.861)
LYON = (4.825, 45.754, 4.840, 45.762)


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


@pytest.mark.parametrize(
    "bbox, share, needle",
    [
        (MARAIS, 1.0, None),  # Paříž: Plan de voirie celý výřez
        (LYON, 0.0, "není technická mapa"),  # mimo Paříž jen OSM
    ],
)
def test_technical_map_area(fr, monkeypatch, bbox, share, needle):
    monkeypatch.setitem(elevation_raster.COVERAGE, "fr_lidarhd", lambda _b: 1.0)
    result = area_probe.probe(fr, bbox)
    assert result["technical_map"]["share"] == share
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
    assert body["probe"]["verdict"] == "warn" and body["probe"]["technical_map"]["share"] == 0.0
    assert any("OSM" in w for w in body["probe"]["warnings"])
    monkeypatch.setattr(main.worker, "enqueue", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "queue_position", lambda *_a, **_k: 0)
    r = client.post("/api/jobs", data={"preset_id": "sprint_2m", "output_mode": "png", "name": "lyon", "bbox": bbox})
    assert r.status_code == 200
    stored = r.json()["options"]["area_probe"]
    assert stored["verdict"] == "warn" and stored["technical_map"]["share"] == 0.0
