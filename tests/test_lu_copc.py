"""Lucembursko: dlaždice mračna LiDAR 2024 v ZIPech a doplnění povrchu o body vegetace."""

import numpy as np
import pytest

from app.pipeline import lu_copc


# Ověřeno na seznamu členů ZIPů data.public.lu (2026-11-16).
@pytest.mark.parametrize(
    "tile, zip_name",
    [((49000, 100000), "lidar2024-c001-r029.zip"), ((49000, 99000), "lidar2024-c001-r029.zip"),
     ((77000, 74500), "lidar2024-c020-r012.zip"), ((77000, 73500), "lidar2024-c020-r012.zip"),
     ((77000, 75000), "lidar2024-c020-r013.zip"), ((78000, 76000), "lidar2024-c020-r013.zip")],
)
def test_zip_name(tile, zip_name):
    assert lu_copc.zip_name(*tile) == zip_name


def test_tile_names_cover_window():
    assert lu_copc.tile_names((77200.0, 74300.0, 77800.0, 74800.0)) == [
        (77000, 74500), (77000, 75000), (77500, 74500), (77500, 75000)]


def test_vegetation_surface_mosaic_and_cache(tmp_path, monkeypatch):
    """Mřížky dlaždic se složí do GeoTIFF (EPSG:2169, 2 m); podruhé se berou z cache, chybějící ZIP se přeskočí."""
    gdal = pytest.importorskip("osgeo.gdal")

    monkeypatch.setattr(lu_copc.settings, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(lu_copc, "zip_index", lambda: {"lidar2024-c020-r012.zip": "https://example.invalid/a.zip"})
    calls = []

    def fake_grid(url, member, e, n):
        calls.append(member)
        g = np.full((250, 250), np.nan, dtype="float32")
        g[0, 0] = 300.0
        return g, 1000

    monkeypatch.setattr(lu_copc, "_tile_grid", fake_grid)
    window = (77200.0, 74300.0, 77800.0, 74800.0)
    msgs = []
    out = lu_copc.vegetation_surface(window, tmp_path, log=msgs.append)
    ds = gdal.Open(str(out))
    assert ds.GetGeoTransform() == (77000.0, 2.0, 0.0, 74500.0, 0.0, -2.0)  # jen dlaždice ze ZIPu r012
    assert (ds.RasterXSize, ds.RasterYSize) == (500, 250)
    a = ds.GetRasterBand(1).ReadAsArray()
    assert a[0, 0] == 300.0 and a[0, 250] == 300.0 and a[1, 1] == -9999.0
    assert sorted(calls) == ["77000_74500.laz", "77500_74500.laz"] and "2/4 dlaždic" in msgs[0]
    (tmp_path / "x").mkdir()
    lu_copc.vegetation_surface(window, tmp_path / "x")
    assert len(calls) == 2  # z cache


def test_range_file_refuses_large_download(monkeypatch):
    """Čtení přes strop bajtů na dlaždici (stovky MB = celá dlaždice) se odmítne dřív, než se stáhne."""
    monkeypatch.setattr(lu_copc, "_get", lambda *a, **k: pytest.fail("nemá se stahovat"))
    f = lu_copc._RangeFile("https://example.invalid/a.zip", 0, 500_000_000)
    with pytest.raises(RuntimeError, match="odmítnuto"):
        f.read(300_000_000)
