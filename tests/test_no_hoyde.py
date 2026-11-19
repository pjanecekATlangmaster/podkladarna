"""Norsko: výšky høydemodell z WCS Geonorge – dlaždice po 1 m, provider skládá VRT, sonda a nabídka bez klíče."""

import urllib.parse

from app import countries, source_status
from app.pipeline import elevation_raster, reference_no


def _query(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


def test_urls_tile_window_at_1m():
    urls = elevation_raster.no_hoyde_urls("dsm", (261000.0, 6656000.0, 263500.0, 6657000.0))
    assert len(urls) == 2  # 2,5 km + okraj → dvě dlaždice nejvýš 2 km
    assert all(u.startswith(reference_no.NO_WCS["dsm"] + "?") for u in urls)
    q = _query(urls[0])
    x0, y0, x1, y1 = map(float, q["BBOX"][0].split(","))
    assert q["COVERAGE"] == ["nhm_dom_topo_25833"] and q["VERSION"] == ["1.0.0"] and q["FORMAT"] == ["GeoTIFF"]
    assert q["CRS"] == ["EPSG:25833"] and x0 == 261000.0 - 20 and y0 == 6656000.0 - 20
    assert int(q["WIDTH"][0]) == x1 - x0 and int(q["HEIGHT"][0]) == y1 - y0


def test_provider_builds_dtm_and_dsm_vrt(tmp_path, monkeypatch):
    got, cmds = [], []
    monkeypatch.setattr(elevation_raster, "_download_retry", lambda url, dest, **_kw: got.append(url) or dest)
    monkeypatch.setattr(elevation_raster, "run_cmd", lambda cmd, **_kw: cmds.append(cmd))
    monkeypatch.setattr(elevation_raster, "_gdal_tool", lambda name: name)
    monkeypatch.setattr(elevation_raster, "_bounds_in", lambda b, f, t: (261000.0, 6656000.0, 262000.0, 6657000.0))
    out = elevation_raster.PROVIDERS["no_hoyde"]((0, 0, 1, 1), tmp_path)
    assert out.dtm.name == "no_hoyde_dtm.vrt" and out.dsm.name == "no_hoyde_dsm.vrt"
    assert sorted(_query(u)["COVERAGE"][0] for u in got) == ["nhm_dom_topo_25833", "nhm_dtm_topo_25833"]
    assert all("EPSG:25833" in c for c in cmds) and len(cmds) == 2


def test_norway_profile_offered_without_key():
    no = countries.load_countries()["NO"]
    assert no.elevation == "no_hoyde" and no.crs == "EPSG:25833" and not no.cz_sources
    assert no.technical_map == "none" and countries.elevation_ready(no)
    assert "©Kartverket" in no.citation


def test_probe_registered_for_norway():
    probe = next(p for p in source_status.PROBES if p.key == "no_elevation")
    assert probe.countries == ("NO",) and probe.fatal and probe.urls == (reference_no.NO_ELEVATION_PROBE,)
    q = _query(probe.urls[0])
    assert q["COVERAGE"] == ["nhm_dtm_topo_25833"] and q["WIDTH"] == ["8"]
