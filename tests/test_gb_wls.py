"""Wales: profil země, hranice, sonda a výšky z celostátních COG DataMapWales."""

import pytest

from app import countries, source_status
from app.pipeline import elevation_raster as er
from app.pipeline import online_templates, reference_gb


def test_wales_profile_and_probe():
    wls = countries.load_countries()["GB-WLS"]
    assert wls.elevation == "gb_wls" and wls.crs == "EPSG:27700" and not wls.cz_sources
    assert "Welsh Government" in wls.citation and wls.status == "beta"
    assert "gb_wls" in er.PROVIDERS and "gb_wls" not in er.COVERAGE  # celoplošný model, bez kontroly pokrytí
    probe = next(p for p in source_status.PROBES if p.key == "wls_elevation")
    assert probe.countries == ("GB-WLS",) and probe.fatal and probe.method == "HEAD"
    assert probe.urls == (reference_gb.WLS_ELEVATION_PROBE,)
    assert online_templates.wms_layers_for("GB-WLS") == online_templates.wms_layers_for("GB-ENG")


@pytest.mark.parametrize(
    "bbox,code",
    [
        ((-4.09, 53.06, -4.07, 53.08), "GB-WLS"),  # Snowdon
        ((-3.19, 51.47, -3.17, 51.49), "GB-WLS"),  # Cardiff
        ((-2.60, 51.44, -2.58, 51.46), "GB-ENG"),  # Bristol
        ((-3.00, 53.40, -2.98, 53.42), "GB-ENG"),  # Liverpool
    ],
)
def test_wales_boundary(bbox, code):
    all_c = countries.load_countries()
    best = max(("GB-WLS", "GB-ENG"), key=lambda c: countries.bbox_share(bbox, all_c[c]))
    assert best == code


def test_provider_reads_both_cogs(tmp_path, monkeypatch):
    calls = []

    def fake(urls, window, crs, out_dir, prefix, **kw):
        calls.append((urls, crs, prefix, kw.get("nodata")))
        return tmp_path / f"{prefix}.vrt"

    monkeypatch.setattr(er, "cog_windows", fake)
    res = er.gb_wls_provider((-3.19, 51.47, -3.17, 51.49), tmp_path)
    assert [c[0] for c in calls] == [[reference_gb.WLS_DTM_URL], [reference_gb.WLS_DSM_URL]]
    assert all(c[1] == "EPSG:27700" and c[3] == er.NODATA for c in calls)
    assert res.dtm.name == "gb_wls_dtm.vrt" and res.dsm.name == "gb_wls_dsm.vrt"


def test_provider_outside_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(er, "cog_windows", lambda *a, **k: None)
    with pytest.raises(er.ElevationUnavailable):
        er.gb_wls_provider((-3.19, 51.47, -3.17, 51.49), tmp_path)
