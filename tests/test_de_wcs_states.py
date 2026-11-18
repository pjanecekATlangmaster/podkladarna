"""Německé země s DGM1 + DOM1 z WCS 2.0.1 (``reference_de_states``): dlaždice, multipart odpověď, provider."""

from __future__ import annotations

import pytest

from app.pipeline import elevation_raster as er
from app.pipeline.reference_de_states import DE_WCS_STATES, elevation_probe, ortho_probe, wcs_coverage_url


def test_every_state_is_a_country_with_provider_and_probes():
    from app import countries, source_status

    loaded = countries.load_countries()
    keys = {p.key for p in source_status.PROBES}
    for key, st in DE_WCS_STATES.items():
        assert loaded[st.code].elevation == key and loaded[st.code].crs == st.crs
        assert key in er.PROVIDERS and key in er.SOURCE_LABELS
        assert {f"{key}_elevation", f"{key}_dop"} <= keys
        assert "REQUEST=GetCoverage" in elevation_probe(key)
        assert f"CRS={st.crs}" in ortho_probe(key)


def test_tiles_at_most_2km_on_whole_metres():
    urls = er.de_wcs_tile_urls("de_st", "dsm", (623000.4, 5743000.0, 626500.0, 5744000.0))
    assert len(urls) == 2  # 3,5 km + 2 × 20 m → dvě dlaždice ≤ 2 km
    for u in urls:
        assert "ows_WCS_ST_DOM1?" in u and "COVERAGEID=Coverage1" in u and "VERSION=2.0.1" in u
        x = u.split("SUBSET=x(")[1].split(")")[0].split(",")
        assert float(x[1]) - float(x[0]) <= 2000 and all(v == str(int(float(v))) for v in x)
    assert "SUBSET=y(5743000,5743016)" in wcs_coverage_url(DE_WCS_STATES["de_st"].dtm, (623000, 5743000, 623016, 5743016))


@pytest.mark.parametrize("nl", [b"\n", b"\r\n"])
def test_multipart_response_keeps_only_tiff(tmp_path, nl):
    tiff = b"II*\x00" + b"\x01" * 600 + nl + b"tail"
    raw = nl.join([b"--wcs", b"Content-Type: text/xml", b"", b"<gml/>", b"--wcs", b"Content-Type: image/tiff",
                   b"Content-ID: 1.tif", b"", tiff, b"--wcs--", b""])
    path = tmp_path / "t.tif"
    path.write_bytes(raw)
    assert er._multipart_tiff(path) == "image/tiff"
    assert path.read_bytes() == tiff
    plain = tmp_path / "p.tif"
    plain.write_bytes(b"II*\x00" + b"\x00" * 10)
    assert er._multipart_tiff(plain) is None  # obyčejný GeoTIFF beze změny


def test_provider_without_surface_keeps_terrain(tmp_path, monkeypatch):
    calls = []

    def fake_download(url, dest, log=None, **_kw):
        if "DOM1" in url:
            raise RuntimeError("503")
        dest.write_bytes(b"II*\x00")
        calls.append(url)
        return dest

    monkeypatch.setattr(er, "_download_retry", fake_download)
    monkeypatch.setattr(er, "run_cmd", lambda cmd, log=None: None)
    monkeypatch.setattr(er, "_gdal_tool", lambda name: name)
    issues = []
    bounds = er._bounds_in((623000, 5743000, 624000, 5744000), "EPSG:25832", er.CRS_WKT)
    out = er.PROVIDERS["de_st"](bounds, tmp_path, issues.append)
    assert out.dtm.name == "de_st_dtm.vrt" and out.dsm is None and calls
    assert any("výška porostu chybí" in str(m) for m in issues)
