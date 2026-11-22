"""Nový Zéland: profil, hranice, sonda a výšky LINZ z COG listů Topo50."""

import json

import pytest

from app import countries, source_status
from app.pipeline import elevation_raster as er
from app.pipeline import reference_nz as nz


def test_profile_and_probe():
    c = countries.load_countries()["NZ"]
    assert c.elevation == "nz_linz" and c.crs == "EPSG:2193" and not c.cz_sources and c.status == "beta"
    assert "nz_linz" in er.PROVIDERS and "nz_linz" in er.COVERAGE
    p = next(p for p in source_status.PROBES if p.key == "nz_elevation")
    assert p.countries == ("NZ",) and p.fatal and p.method == "HEAD" and p.urls == (nz.NZ_ELEVATION_PROBE,)


@pytest.mark.parametrize(
    "bbox,inside",
    [
        ((174.77, -41.30, 174.79, -41.28), True),  # Wellington
        ((168.65, -45.04, 168.67, -45.02), True),  # Queenstown
        ((172.62, -43.54, 172.64, -43.52), True),  # Christchurch
        ((14.40, 50.07, 14.42, 50.09), False),  # Praha
    ],
)
def test_boundary(bbox, inside):
    assert (countries.bbox_share(bbox, countries.load_countries()["NZ"]) > 0.9) is inside


# Okraje listů z hlaviček COG (gdalinfo 2026-11-22).
@pytest.mark.parametrize(
    "name,bounds",
    [
        ("AS21", (1492000, 6198000, 1516000, 6234000)),
        ("BQ31", (1732000, 5406000, 1756000, 5442000)),
        ("BX12", (1276000, 5154000, 1300000, 5190000)),
    ],
)
def test_sheet_bounds(name, bounds):
    assert nz.sheet_bounds(name) == tuple(float(v) for v in bounds)
    x0, y0, x1, y1 = bounds
    assert nz.sheets_in((x0 + 10, y0 + 10, x1 - 10, y1 - 10)) == [name]


def test_sheets_in_across_corner():
    # roh čtyř listů BQ31/BQ32/BR31/BR32
    assert sorted(nz.sheets_in((1755000, 5405000, 1757000, 5407000))) == ["BQ31", "BQ32", "BR31", "BR32"]
    assert nz.sheet_bounds("IO12") is None and nz.sheet_bounds("X1") is None


def test_index_and_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(nz.settings, "CACHE_DIR", tmp_path)
    calls = []

    def fetch(url):
        calls.append(url)
        links = [{"rel": "root", "href": "../catalog.json"}, {"rel": "item", "href": "./BQ31.json"}]
        if "dem_1m" in url:
            links.append({"rel": "item", "href": "./BQ32.json"})
        return json.dumps({"links": links})

    idx = nz.nz_index(fetch=fetch)
    assert idx == {"dtm": ["BQ31", "BQ32"], "dsm": ["BQ31"]} and len(calls) == 2
    assert nz.nz_index(fetch=fetch) == idx and len(calls) == 2  # z cache
    win = (1755000, 5420000, 1757000, 5421000)
    assert nz.sheet_urls("dtm", win, idx) == [
        f"{nz.NZ_MOSAIC}/dem_1m/2193/BQ31.tiff",
        f"{nz.NZ_MOSAIC}/dem_1m/2193/BQ32.tiff",
    ]
    assert nz.sheet_urls("dsm", win, idx) == [f"{nz.NZ_MOSAIC}/dsm_1m/2193/BQ31.tiff"]


def test_provider_reads_dem_and_dsm(tmp_path, monkeypatch):
    import numpy as np

    monkeypatch.setattr(nz, "nz_index", lambda **k: {"dtm": ["BQ31"], "dsm": ["BQ31"]})
    monkeypatch.setattr(er, "_buffered", lambda b, crs, m: (1740000, 5420000, 1741000, 5421000))
    monkeypatch.setattr(er, "sct_valid_mask", lambda src, win: np.ones((2, 2), bool))
    calls = []

    def fake(urls, window, crs, out_dir, prefix, **kw):
        calls.append((urls, crs, prefix))
        return tmp_path / f"{prefix}.vrt"

    monkeypatch.setattr(er, "cog_windows", fake)
    res = er.PROVIDERS["nz_linz"]((0, 0, 1, 1), tmp_path)
    assert [c[0][0].rsplit("/", 3)[-3] for c in calls] == ["dem_1m", "dsm_1m"]
    assert all(c[1] == "EPSG:2193" for c in calls)
    assert res.dtm.name == "nz_linz_dtm.vrt" and res.dsm.name == "nz_linz_dsm.vrt"


@pytest.mark.parametrize("case", ["no_sheet", "low_share"])
def test_provider_outside_raises(tmp_path, monkeypatch, case):
    import numpy as np

    monkeypatch.setattr(nz, "nz_index", lambda **k: {"dtm": [] if case == "no_sheet" else ["BQ31"], "dsm": []})
    monkeypatch.setattr(er, "_buffered", lambda b, crs, m: (1740000, 5420000, 1741000, 5421000))
    monkeypatch.setattr(er, "sct_valid_mask", lambda src, win: np.zeros((2, 2), bool))
    monkeypatch.setattr(er, "cog_windows", lambda *a, **k: tmp_path / "x.vrt")
    with pytest.raises(er.ElevationUnavailable):
        er.PROVIDERS["nz_linz"]((0, 0, 1, 1), tmp_path)
