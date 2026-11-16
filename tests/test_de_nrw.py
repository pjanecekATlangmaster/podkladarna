"""Severní Porýní-Vestfálsko: dlaždice WCS a povrch DGM + nDOM (letní, olistěný)."""

from __future__ import annotations

import numpy as np
import pytest

from app.pipeline import elevation_raster as er
from app.pipeline.reference_de_nw import de_nw_coverage_url


def test_coverage_url_scales_ndom_to_1m():
    url = de_nw_coverage_url("ndom", (500000, 5716000, 502000, 5718000))
    assert "wcs_nw_ndom?" in url and "COVERAGEID=nw_ndom" in url and "SCALEFACTOR=0.5" in url
    assert "SUBSET=x(500000,502000)&SUBSET=y(5716000,5718000)" in url
    assert "SCALEFACTOR=1" in de_nw_coverage_url("dtm", (0, 0, 1, 1))


def test_tiles_at_most_2km_on_whole_metres():
    urls = er.de_nw_tile_urls("dtm", (500000.4, 5716000.0, 503500.0, 5717000.0))
    assert len(urls) == 2  # 3,5 km + 2 × 20 m buffer → dvě dlaždice ≤ 2 km
    for u in urls:
        x = u.split("SUBSET=x(")[1].split(")")[0].split(",")
        assert float(x[1]) - float(x[0]) <= 2000 and all(v == str(int(float(v))) for v in x)


@pytest.mark.parametrize(
    "ndom, dsm, expected",
    [
        # pole: obilí jen v letním nDOM → 0; holina: les jen ve starším zimním DOM → 0; les v obou → menší
        ([2.5, 0.0, 15.0], [300.0, 330.0, 328.0], [300.0, None, 325.0]),
        ([2.5, 5.0, -3.0], None, [302.5, None, 310.0]),  # bez DOM jen nDOM (záporné = 0)
        (None, [300.0, 330.0, 328.0], [300.0, None, 328.0]),  # bez nDOM jen DOM
    ],
)
def test_surface_takes_lower_of_winter_als_and_summer_ndom(tmp_path, ndom, dsm, expected):
    gdal = pytest.importorskip("osgeo.gdal")

    def tif(name, arr, nodata=None):
        if arr is None:
            return None
        ds = gdal.GetDriverByName("GTiff").Create(str(tmp_path / name), 3, 1, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((500000, 1, 0, 5716001, 0, -1))
        if nodata is not None:
            ds.GetRasterBand(1).SetNoDataValue(nodata)
        ds.GetRasterBand(1).WriteArray(np.array([arr], dtype="float32"))
        ds = None
        return tmp_path / name

    dtm = tif("dtm.tif", [300.0, -9999.0, 310.0], nodata=-9999.0)
    out = er._de_nw_surface(dtm, tif("ndom.tif", ndom), tif("dsm.tif", dsm), tmp_path / "s.tif")
    got = gdal.Open(str(out)).ReadAsArray()[0].tolist()
    assert got == [er.NODATA if v is None else v for v in expected]


def test_surface_skips_source_on_other_grid(tmp_path):
    """Povrch v jiné mřížce než DGM se vynechá a vezme se druhý (review PR 350)."""
    gdal = pytest.importorskip("osgeo.gdal")

    def tif(name, arr, width=3):
        ds = gdal.GetDriverByName("GTiff").Create(str(tmp_path / name), width, 1, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((500000, 1, 0, 5716001, 0, -1))
        ds.GetRasterBand(1).WriteArray(np.array([arr], dtype="float32"))
        ds = None
        return tmp_path / name

    issues = []
    out = er._de_nw_surface(tif("dtm.tif", [300.0, 300.0, 300.0]), tif("ndom.tif", [5.0, 5.0], width=2),
                            tif("dsm.tif", [310.0, 300.0, 320.0]), tmp_path / "s.tif", log=issues.append)
    assert gdal.Open(str(out)).ReadAsArray()[0].tolist() == [310.0, 300.0, 320.0]
    assert any("jinou mřížku" in str(m) for m in issues)


def test_download_refuses_oversized_response(tmp_path, monkeypatch):
    """Tvrdý strop odpovědi výškové služby (sdílený ``_download``)."""
    import io
    import urllib.request

    class Resp(io.BytesIO):
        headers = {"Content-Type": "image/tiff", "Content-Length": ""}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: Resp(b"x" * 5000))
    with pytest.raises(RuntimeError, match="odmítnuta"):
        er._download("https://example.org/wcs", tmp_path / "t.tif", max_bytes=1000)
    assert not list(tmp_path.iterdir())
