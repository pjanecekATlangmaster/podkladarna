"""Zahraniční build: beta země, profil zdrojů jobu a výšky z rastru."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import countries
from app.pipeline import country_profile


@pytest.mark.parametrize(
    "beta, bbox, code",
    [
        ("SK", (17.09, 48.165, 17.10, 48.175), "SK"),  # Bratislava, Kamzík
        ("*", (19.95, 49.20, 19.97, 49.21), "SK"),  # Vysoké Tatry
        ("", (14.40, 50.08, 14.42, 50.09), "CZ"),  # Praha beze změny
    ],
)
def test_beta_country_allowed_with_flag(monkeypatch, beta, bbox, code):
    monkeypatch.setenv("COUNTRIES_BETA", beta)
    country, share = countries.check_bbox(bbox)
    assert country.code == code and share > 0.99


def test_beta_country_hidden_without_flag(monkeypatch):
    monkeypatch.setenv("COUNTRIES_BETA", "")
    assert [c.code for c in countries.allowed_countries(None)] == ["CZ"]
    with pytest.raises(countries.CountryError):
        countries.check_bbox((17.09, 48.165, 17.10, 48.175))
    # NL je připravené, ale vypnuté (čeká na souřadnice jobu podle země).
    monkeypatch.setenv("COUNTRIES_BETA", "*")
    assert "NL" not in [c.code for c in countries.allowed_countries(None)]


@pytest.mark.parametrize(
    "options, cz, elevation",
    [
        ({}, True, "cz_openzu"),  # starší joby bez země = Česko
        ({"country": "CZ"}, True, "cz_openzu"),
        ({"country": "sk"}, False, countries.load_countries()["SK"].elevation),
        ({"country": "NL"}, False, "nl_ahn"),
        ({"country": "XX"}, False, "none"),
    ],
)
def test_job_profile(options, cz, elevation):
    assert country_profile.uses_cz_sources(options) is cz
    assert country_profile.elevation_source(options) == elevation


def _write_tif(path: Path, values, origin, nodata=-9999.0, epsg_wkt=None):
    import numpy as np
    from osgeo import gdal

    from app.pipeline.crs_5514 import CRS_WKT

    arr = np.asarray(values, dtype="float32")
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((origin[0], 1.0, 0, origin[1], 0, -1.0))
    ds.SetProjection(epsg_wkt or CRS_WKT)
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(nodata)
    band.WriteArray(arr)
    ds = None


def test_raster_surfaces_from_local_tiles(tmp_path, monkeypatch):
    pytest.importorskip("osgeo.gdal_array")
    from app.pipeline import elevation_raster

    try:
        elevation_raster._gdal_tool("gdalwarp")
    except RuntimeError:
        pytest.skip("gdalwarp není k dispozici")
    import numpy as np

    monkeypatch.setattr(elevation_raster.settings, "DATA_ROOT", tmp_path)
    x0, y0 = -600000.0, -1160000.0  # Bratislava v S-JTSK
    dtm = np.full((40, 40), 200.0)
    dtm[10:14, 10:14] = -9999.0  # díra (budova) – CHM tam 0
    dsm = dtm.copy()
    dsm[20:30, 20:30] = 215.0  # porost 15 m
    dsm[10:14, 10:14] = 210.0
    _write_tif(tmp_path / "elev" / "dtm" / "a.tif", dtm, (x0, y0 + 40))
    _write_tif(tmp_path / "elev" / "dsm" / "a.tif", dsm, (x0, y0 + 40))
    work = tmp_path / "work"
    res = elevation_raster.prepare_raster_surfaces(
        work, (x0 + 5, y0 + 5, x0 + 35, y0 + 35), source="local:elev", resolution_m=1.0
    )
    chm = elevation_raster._read(res.chm)
    filled = elevation_raster._read(res.dem_filled)
    assert chm.shape == (30, 30)
    assert (filled > 150).all()  # díra vyplněná
    assert chm.max() == pytest.approx(15.0, abs=0.01)
    assert chm[5:9, 5:9].max() == 0.0  # pod dírou DTM žádný porost
    assert (work / "dem" / "dem_meta.json").is_file()


def test_raster_surfaces_unknown_source(tmp_path):
    from app.pipeline import elevation_raster

    with pytest.raises(elevation_raster.ElevationUnavailable):
        elevation_raster.prepare_raster_surfaces(tmp_path, (0, 0, 10, 10), source="none")


def test_api_beta_country_without_sheets(client, monkeypatch):
    import app.main as main

    def _no_sheets(*_a, **_k):
        raise AssertionError("mimo Česko se listy SM5 nedotazují")

    monkeypatch.setenv("COUNTRIES_BETA", "SK")
    monkeypatch.setattr(main, "query_sm5_sheets", _no_sheets)
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "enqueue", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "queue_position", lambda *_a, **_k: 0)
    bbox = "17.085,48.165,17.100,48.175"
    body = client.get("/api/sheets", params={"bbox": bbox}).json()
    assert body["country"] == "SK" and body["count"] == 0 and body["estimate_minutes"]
    r = client.post("/api/jobs", data={"preset_id": "sprint_2m", "output_mode": "png_zip", "name": "ba", "bbox": bbox})
    assert r.status_code == 200, r.text
    opts = r.json()["options"]
    assert opts["country"] == "SK" and opts["sm5_sheets"] == []


def test_ahn_coverage_urls_tiling():
    from app.pipeline.elevation_raster import AHN_MAX_SIDE_M, ahn_coverage_urls

    small = ahn_coverage_urls("dtm_05m", (117000.2, 480000.0, 118000.0, 481000.7))
    assert len(small) == 1
    assert "COVERAGEID=dtm_05m" in small[0] and "SUBSET=x(116980.0,118020.0)" in small[0]
    assert "SUBSET=y(479980.0,481021.0)" in small[0]
    big = ahn_coverage_urls("dsm_05m", (100000.0, 400000.0, 100000.0 + 2.5 * AHN_MAX_SIDE_M, 401000.0))
    assert len(big) == 3


def test_krovak_source_not_shifted(tmp_path, monkeypatch):
    """Rastr v oficiálním S-JTSK (bez TOWGS84, SK data) se na mřížku mapy nepřevádí přes datum."""
    pytest.importorskip("osgeo.gdal_array")
    from osgeo import osr

    from app.pipeline import elevation_raster

    try:
        elevation_raster._gdal_tool("gdalwarp")
    except RuntimeError:
        pytest.skip("gdalwarp není k dispozici")
    import numpy as np

    srs = osr.SpatialReference()
    srs.ImportFromProj4(
        "+proj=krovak +lat_0=49.5 +lon_0=24.83333333333333 +alpha=30.28813972222222 +k=0.9999 +x_0=0 +y_0=0 "
        "+ellps=bessel +units=m +no_defs"
    )
    monkeypatch.setattr(elevation_raster.settings, "DATA_ROOT", tmp_path)
    x0, y0 = -600000.0, -1160000.0
    dtm = np.tile(np.arange(40, dtype="float32"), (40, 1))  # výška = sloupec → posun je vidět
    _write_tif(tmp_path / "elev" / "dtm" / "a.tif", dtm, (x0, y0 + 40), epsg_wkt=srs.ExportToWkt())
    res = elevation_raster.prepare_raster_surfaces(
        tmp_path / "work", (x0 + 10, y0 + 10, x0 + 30, y0 + 30), source="local:elev"
    )
    filled = elevation_raster._read(res.dem_filled)
    assert filled[5, 0] == pytest.approx(10.0, abs=0.6)


def test_sk_wcs_urls_and_geoid(tmp_path):
    pytest.importorskip("osgeo.gdal_array")
    import numpy as np
    from osgeo import gdal, osr

    from app.pipeline import elevation_raster as er

    urls = er.sk_wcs_urls((208000.3, 5341500.0, 210000.0, 5343500.0))
    assert len(urls) == 1 and "subset=E(207980,210020)" in urls[0] and "el__EL.GridCoverage" in urls[0]
    assert "subset=N(5341480,5343520)" in urls[0] and "e+" not in urls[0]  # ne vědecký zápis (GeoServer 400)
    assert len(er.sk_wcs_urls((200000.0, 5300000.0, 209000.0, 5301000.0))) == 3
    # Mřížka kvazigeoidu: lineární v délce → bilineární interpolace přesná.
    grid = tmp_path / "geoid.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(grid), 4, 3, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((16.0, 1.0, 0, 50.0, 0, -1.0))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4258)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(np.tile(np.array([40.0, 41.0, 42.0, 43.0], dtype="float32"), (3, 1)))
    ds = None
    assert er.geoid_offsets(grid, [(17.0, 48.5)])[0] == pytest.approx(40.5)  # mezi středy pixelů
    assert er.geoid_offsets(grid, [(17.5, 49.0)])[0] == pytest.approx(41.0)


def test_raster_surfaces_without_osgeo_and_vertical_offset(tmp_path, monkeypatch):
    """Lokální Windows Python bez ``osgeo``: vše přes GDAL CLI; elipsoidické výšky se srovnají kvazigeoidem."""
    pytest.importorskip("osgeo.gdal_array")
    import sys

    import numpy as np

    from app.pipeline import elevation_raster

    try:
        elevation_raster._gdal_tool("gdalwarp")
    except RuntimeError:
        pytest.skip("gdalwarp není k dispozici")
    x0, y0 = -600000.0, -1160000.0
    dtm = np.full((40, 40), 244.0)
    dsm = dtm.copy()
    dsm[20:30, 20:30] = 259.0
    _write_tif(tmp_path / "dtm.tif", dtm, (x0, y0 + 40))
    _write_tif(tmp_path / "dsm.tif", dsm, (x0, y0 + 40))
    monkeypatch.setitem(
        elevation_raster.PROVIDERS,
        "test_ell",
        lambda b, d, log=None: elevation_raster.SourceRasters(tmp_path / "dtm.tif", tmp_path / "dsm.tif", lambda lon, lat: 44.0),
    )
    monkeypatch.setitem(sys.modules, "osgeo", None)  # import osgeo → ImportError
    res = elevation_raster.prepare_raster_surfaces(
        tmp_path / "work", (x0 + 5, y0 + 5, x0 + 35, y0 + 35), source="test_ell", resolution_m=1.0
    )
    monkeypatch.delitem(sys.modules, "osgeo")
    filled = elevation_raster._read(res.dem_filled)
    chm = elevation_raster._read(res.chm)
    assert filled.mean() == pytest.approx(200.0, abs=0.01)
    assert chm.max() == pytest.approx(15.0, abs=0.01)


def test_process_country_gates_cuzk_orthophoto(tmp_path, monkeypatch):
    from app.pipeline import orthophoto

    import os

    os.environ.pop(country_profile.JOB_COUNTRY_ENV, None)
    assert country_profile.process_uses_cz_sources()
    country_profile.set_process_country({"country": "SK"})
    try:
        assert not country_profile.process_uses_cz_sources()
        monkeypatch.setattr(orthophoto, "_fetch_orthophoto", lambda *a, **k: pytest.fail("ČÚZK mimo Česko"))
        assert orthophoto.fetch_orthophoto(tmp_path, (0, 0, 10, 10)) is None
    finally:
        os.environ.pop(country_profile.JOB_COUNTRY_ENV, None)  # monkeypatch.delenv by „SK“ po testu vrátil
