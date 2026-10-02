"""Tests for GDAL CLI raster helpers (no system osgeo required)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.pipeline.gdal_cli_raster import (
    polygonize_byte_raster,
    read_float32_geotiff,
    write_uint8_geotiff,
)
from app.tool_env import which_tool


@pytest.mark.skipif(not which_tool("gdalinfo"), reason="gdalinfo not on PATH")
def test_read_float32_geotiff_job_dem():
    dem = Path("data/jobs/743135094e4a/work/dem/dem_filled.tif")
    if not dem.is_file():
        pytest.skip("sample dem missing")
    arr, gt, nodata = read_float32_geotiff(dem)
    assert arr.ndim == 2
    assert arr.shape[0] > 10 and arr.shape[1] > 10
    assert len(gt) == 6
    assert nodata == -9999.0 or nodata is None


@pytest.mark.skipif(
    not (which_tool("gdal_translate") and which_tool("gdal_polygonize")),
    reason="gdal_translate/polygonize missing",
)
def test_write_and_polygonize_roundtrip(tmp_path: Path):
    dem = Path("data/jobs/743135094e4a/work/dem/dem_filled.tif")
    if not dem.is_file():
        pytest.skip("sample dem missing")
    arr, gt, _nd = read_float32_geotiff(dem)
    sub = arr[:40, :40]
    cls = (sub > 400).astype(np.uint8)
    tif = tmp_path / "cls.tif"
    write_uint8_geotiff(tif, cls, gt, nodata=0)
    assert tif.is_file()
    shp = tmp_path / "poly.shp"
    polygonize_byte_raster(tif, shp, layer_name="poly", field_name="cls")
    assert shp.is_file()
