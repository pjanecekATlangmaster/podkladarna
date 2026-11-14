from __future__ import annotations

import pytest

from app import dtm_coverage
from app.pipeline import dtm_sources


@pytest.mark.parametrize(
    "opl, kr, quality",
    [
        (141_000, 1_671_000, "sparse"),  # Středočeská obec s neúplnými plochami (kalibrace 0,085)
        (268_000, 928_000, "sparse"),  # 0,29
        (2_388_000, 3_544_000, "good"),  # Praha MOMC 0,67
        (5_567_000, 4_704_000, "good"),  # Liberecký kraj 1,18
        (None, 400_000, "none"),  # obec bez balíku ploch
        (None, None, "none"),
        (300_000, None, "unknown"),
    ],
)
def test_unit_quality(opl, kr, quality):
    assert dtm_coverage.unit_quality(opl, kr)[0] == quality


@pytest.mark.parametrize(
    "qualities, level",
    [
        (["good", "good"], "good"),
        (["good", "sparse"], "partial"),
        (["sparse", "none"], "sparse"),
        (["none"], "none"),
        (["unknown"], "unknown"),
        ([], "none"),
    ],
)
def test_overall_level(qualities, level):
    assert dtm_coverage.overall_level([{"quality": q} for q in qualities]) == level


def _pkg(kind, code, set_name):
    return dtm_sources.DtmPackage(
        url=f"https://example.test/VS_CZ020_{kind}_{code}_20260930_{set_name}.jvf.zip",
        kraj="CZ020",
        unit_kind=kind,
        unit_code=code,
        date="20260930",
        set_name=set_name,
        fmt="jvf",
    )


def test_coverage_for_bbox_uses_head_sizes_and_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(dtm_coverage.settings, "DOWNLOADS_DIR", tmp_path)
    units = [
        dtm_sources.DtmUnit("OBEC", 1, "Chudá", "Středočeský kraj"),
        dtm_sources.DtmUnit("OBEC", 2, "Úplná", "Středočeský kraj"),
    ]
    pkgs = [_pkg("OBEC", c, s) for c in (1, 2) for s in ("ZPS_OPL", "ZPS_KR")]
    sizes = {
        pkgs[0].url: 100_000, pkgs[1].url: 1_000_000,  # obec 1: 0,1
        pkgs[2].url: 800_000, pkgs[3].url: 1_000_000,  # obec 2: 0,8
    }
    calls = []
    monkeypatch.setattr(dtm_sources, "units_for_bbox", lambda bbox: units)
    monkeypatch.setattr(dtm_sources, "fetch_regions", lambda: [{"kod": "CZ020"}])
    monkeypatch.setattr(dtm_sources, "region_code_for_name", lambda name, regions: "CZ020")
    monkeypatch.setattr(dtm_sources, "fetch_url_list", lambda kraj: pkgs)
    monkeypatch.setattr(dtm_coverage, "_head_size", lambda url: calls.append(url) or sizes[url])
    out = dtm_coverage.coverage_for_bbox((14.2, 49.9, 14.3, 50.0))
    assert out["available"] and out["level"] == "partial"
    assert [(u["name"], u["quality"]) for u in out["units"]] == [("Chudá", "sparse"), ("Úplná", "good")]
    assert len(calls) == 4
    # Velikosti jsou uložené (název balíku nese datum), druhý dotaz se HEAD neptá.
    dtm_coverage.coverage_for_bbox((14.2, 49.9, 14.3, 50.0))
    assert len(calls) == 4


def test_coverage_skipped_when_dmvs_down(monkeypatch):
    monkeypatch.setattr(dtm_coverage, "_dmvs_down", lambda: True)
    monkeypatch.setattr(dtm_sources, "units_for_bbox", lambda bbox: pytest.fail("bez sítě"))
    assert dtm_coverage.coverage_for_bbox((14.2, 49.9, 14.3, 50.0))["available"] is False


def test_api_dtm_coverage(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main.dtm_coverage, "coverage_cached", lambda bbox: {"available": True, "units": [], "level": "none"})
    assert client.get("/api/dtm_coverage", params={"bbox": "14.40,50.08,14.42,50.09"}).json()["level"] == "none"
    assert client.get("/api/dtm_coverage", params={"bbox": "16.35,48.18,16.40,48.22"}).status_code == 400
