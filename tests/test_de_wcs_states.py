"""Německé země s DGM1 + DOM1 z WCS 2.0.1 (``reference_de_states``): dlaždice, multipart odpověď, provider."""

from __future__ import annotations

import pytest
from pathlib import Path

from app.pipeline import elevation_raster as er
from app.pipeline.reference_de_states import DE_WCS_STATES, elevation_probe, ortho_probe, sn_zip_url, wcs_coverage_url


def test_every_state_is_a_country_with_provider_and_probes():
    from app import countries, source_status

    loaded = countries.load_countries()
    keys = {p.key for p in source_status.PROBES}
    for key, st in DE_WCS_STATES.items():
        assert loaded[st.code].elevation == key and loaded[st.code].crs == st.crs
        assert key in er.PROVIDERS and key in er.SOURCE_LABELS
        assert {f"{key}_elevation", f"{key}_dop"} <= keys
        assert ("REQUEST=GetCoverage" in elevation_probe(key)) is (st.tiles == "wcs")
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


def test_meta4_index_maps_tiles_by_lower_left_km():
    text = (
        '<file name="dgm1_32_337_5553_1_rp_2025.tif"><url>https://x.example/dgm1/current/tif/'
        'dgm1_32_337_5553_1_rp_2025.tif</url></file><file name="dgm1_32_337_5553_1_rp_2025.tfw"><url>'
        'https://x.example/dgm1/current/tif/dgm1_32_337_5553_1_rp_2025.tfw</url></file>'
    )
    assert er.parse_meta4_tiles(text, "dgm1") == {(337, 5553): "https://x.example/dgm1/current/tif/dgm1_32_337_5553_1_rp_2025.tif"}
    assert er.parse_meta4_tiles(text, "dom1") == {}


def test_meta4_provider_downloads_only_tiles_in_window(tmp_path, monkeypatch):
    index = {(417, 5485): "https://x.example/a.tif", (418, 5485): "https://x.example/b.tif",
             (500, 5500): "https://x.example/far.tif"}
    got = []
    monkeypatch.setattr(er, "de_meta4_index", lambda url, prefix, log=None: index)
    monkeypatch.setattr(er, "_download_retry", lambda url, dest, log=None, **_k: got.append(url) or dest)
    monkeypatch.setattr(er, "run_cmd", lambda cmd, log=None: None)
    monkeypatch.setattr(er, "_gdal_tool", lambda name: name)
    bounds = er._bounds_in((417500, 5485200, 418500, 5485800), "EPSG:25832", er.CRS_WKT)
    out = er.PROVIDERS["de_rp"](bounds, tmp_path, None)
    assert out.dtm.name == "de_rp_dtm.vrt" and out.dsm.name == "de_rp_dsm.vrt"
    assert sorted(set(got)) == ["https://x.example/a.tif", "https://x.example/b.tif"]


def test_bw_tiles_on_odd_east_even_north_grid():
    assert er.bw_tiles((489500, 5402500, 492100, 5403100)) == [(489, 5402), (491, 5402)]
    assert er.bw_tiles((488100, 5401900, 488900, 5402100)) == [(487, 5400), (487, 5402)]


def test_xyz_grid_rows_from_north():
    text = b"10.5 21.5 1\r\n11.5 21.5 2\r\n10.5 20.5 3\r\n11.5 20.5 4\r\n"
    grid, left, top = er.xyz_grid(text)
    assert grid.tolist() == [[1.0, 2.0], [3.0, 4.0]] and (left, top) == (10.0, 22.0)
    with pytest.raises(RuntimeError, match="úplná mřížka"):
        er.xyz_grid(b"10.5 21.5 1\n11.5 21.5 2\n10.5 20.5 3\n")


def test_bw_tile_rejects_oversized_member(tmp_path, monkeypatch):
    import io
    import urllib.request
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("dgm1_32_489_5402_1_bw_2023.xyz", b"0" * 5000)  # malý ZIP, velký rozbalený soubor
    monkeypatch.setattr(er, "bw_tile_dir", lambda e, n: tmp_path / f"{e}_{n}")
    monkeypatch.setattr(er, "BW_MAX_MEMBER_BYTES", 1000)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(buf.getvalue()))
    with pytest.raises(RuntimeError, match="přes limit"):
        er._bw_tile("de_bw", 489, 5402)
    assert not (tmp_path / "489_5402" / ".complete").exists()


def test_bw_provider_uses_cached_tiles_in_window(tmp_path, monkeypatch):
    folder = tmp_path / "489_5402"
    folder.mkdir()
    for e, n in ((489, 5402), (490, 5402), (489, 5403), (490, 5403)):
        (folder / f"dtm_{e}_{n}.tif").touch()
    (folder / "ndom_489_5402.tif").touch()
    monkeypatch.setattr(er, "_bw_tile", lambda key, e, n, log=None: folder if (e, n) == (489, 5402) else None)
    monkeypatch.setattr(er, "_de_nw_surface", lambda dtm, ndom, dsm, dest, log=None, label="": dest)
    vrts = {}
    monkeypatch.setattr(er, "run_cmd", lambda cmd, log=None: vrts.setdefault(cmd[3], cmd[4:]))
    monkeypatch.setattr(er, "_gdal_tool", lambda name: name)
    issues = []
    bounds = er._bounds_in((489400, 5402400, 489600, 5402600), "EPSG:25832", er.CRS_WKT)
    out = er.PROVIDERS["de_bw"](bounds, tmp_path, issues.append)
    assert out.dtm.name == "de_bw_dtm.vrt" and out.dsm.name == "de_bw_dsm.vrt"
    assert [Path(p).name for p in vrts[str(out.dtm)]] == ["dtm_489_5402.tif"]  # jen km² ve výřezu (+20 m)
    assert [Path(p).name for p in vrts[str(out.dsm)]] == ["de_bw_surface_489_5402.tif"]
    assert not issues


def test_sn_tiles_even_grid():
    assert er.sn_tiles((409500, 5656500, 412500, 5657500)) == [(408, 5656), (410, 5656), (412, 5656)]
    assert sn_zip_url(DE_WCS_STATES["de_sn"].dtm, 410, 5656).endswith(
        "/dgm1_33410_5656_2_sn_tiff.zip")


def test_zip_tif_extracts_single_geotiff_and_skips_missing(tmp_path, monkeypatch):
    import zipfile

    src = tmp_path / "t.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("dgm1_33410_5656_2_sn.tif", b"II*\x00data")
        zf.writestr("dgm1_33410_5656_2_sn.tfw", b"1")
    dest = tmp_path / "out" / "dtm.tif"
    dest.parent.mkdir()
    assert er._zip_tif(src.as_uri(), dest, 10_000, "test")
    assert dest.read_bytes() == b"II*\x00data" and sorted(p.name for p in dest.parent.iterdir()) == ["dtm.tif"]
    monkeypatch.setattr(er, "SN_MAX_MEMBER_BYTES", 4)
    with pytest.raises(RuntimeError, match="přes limit"):
        er._zip_tif(src.as_uri(), dest, 10_000, "test")


def test_sn_provider_uses_cached_tiles(tmp_path, monkeypatch):
    with_dsm, without = tmp_path / "410_5656", tmp_path / "412_5656"
    for folder in (with_dsm, without):
        folder.mkdir()
        (folder / "dtm.tif").touch()
    (with_dsm / "dsm.tif").touch()
    tiles = {(410, 5656): with_dsm, (412, 5656): without}
    monkeypatch.setattr(er, "_sn_tile", lambda key, e, n, log=None: tiles.get((e, n)))
    vrts = {}
    monkeypatch.setattr(er, "run_cmd", lambda cmd, log=None: vrts.setdefault(cmd[3], cmd[4:]))
    monkeypatch.setattr(er, "_gdal_tool", lambda name: name)
    issues = []
    bounds = er._bounds_in((411500, 5656500, 412500, 5657500), "EPSG:25833", er.CRS_WKT)
    out = er.PROVIDERS["de_sn"](bounds, tmp_path, issues.append)
    assert len(vrts[str(out.dtm)]) == 2 and vrts[str(out.dsm)] == [str(with_dsm / "dsm.tif")]
    assert issues and "1 z 2" in issues[0]
