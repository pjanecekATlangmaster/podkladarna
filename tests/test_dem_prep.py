"""Unit tests for shared DEM/DSM/CHM prep (PDAL mocked)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.pipeline.dem_prep import (
    DemPrepResult,
    dem_work_dir,
    load_dem_meta,
    prepare_job_surfaces,
)


def test_prepare_job_surfaces_dem_only(tmp_path: Path):
    work = tmp_path / "work"
    lidar = work / "lidar"
    lidar.mkdir(parents=True)
    ground = lidar / "ground_merged.laz"
    ground.write_bytes(b"laz" * 400)

    def fake_pdal(laz, bounds, dest, *, resolution_m=1.0, log=None):
        dest.write_bytes(b"dem" * 200)

    def fake_fill(src, dest, *, log=None):
        dest.write_bytes(src.read_bytes() + b"fill")

    bounds = (-700010.0, -1050010.0, -700000.0, -1050000.0)
    with (
        patch("app.pipeline.dem_prep._pdal_dem_from_laz", side_effect=fake_pdal),
        patch("app.pipeline.dem_prep._fill_dem_nodata", side_effect=fake_fill),
    ):
        result = prepare_job_surfaces(work, bounds, resolution_m=1.0)

    assert isinstance(result, DemPrepResult)
    assert result.dem_filled.is_file()
    assert result.dsm_filled is None
    assert result.chm is None
    assert result.ground_laz == "ground_merged.laz"
    meta = load_dem_meta(work)
    assert meta is not None
    assert meta["resolution_m"] == 1.0
    assert (dem_work_dir(work) / "dem_meta.json").is_file()


def test_prepare_job_surfaces_with_chm(tmp_path: Path):
    work = tmp_path / "work"
    lidar = work / "lidar"
    lidar.mkdir(parents=True)
    (lidar / "ground_merged.laz").write_bytes(b"laz" * 400)
    (lidar / "veg_merged.laz").write_bytes(b"veg" * 400)

    def fake_pdal(laz, bounds, dest, *, resolution_m=1.0, log=None):
        dest.write_bytes(b"tif" * 200)

    def fake_fill(src, dest, *, log=None):
        dest.write_bytes(b"filled" * 50)

    def fake_chm(dem, dsm, dest, *, log=None):
        dest.write_bytes(b"chm" * 50)
        return dest

    bounds = (0.0, 0.0, 10.0, 10.0)
    with (
        patch("app.pipeline.dem_prep._pdal_dem_from_laz", side_effect=fake_pdal),
        patch("app.pipeline.dem_prep._fill_dem_nodata", side_effect=fake_fill),
        patch("app.pipeline.dem_prep._gdal_chm", side_effect=fake_chm),
    ):
        result = prepare_job_surfaces(work, bounds)

    assert result.dsm_filled is not None and result.dsm_filled.is_file()
    assert result.chm is not None and result.chm.is_file()
    assert result.surface_laz == "veg_merged.laz"


def test_gdal_chm_via_qgis_tool(tmp_path: Path):
    """Na Windows/QGIS: gdal_calc z Scripts musí najít tool_env.which_tool."""
    from app.pipeline.dem_prep import _gdal_chm
    from app.tool_env import which_tool

    calc = which_tool("gdal_calc")
    if not calc:
        return
    dem_src = Path("data/jobs/743135094e4a/work/dem/dem_filled.tif")
    dsm_src = Path("data/jobs/743135094e4a/work/dem/dsm_filled.tif")
    if not dem_src.is_file() or not dsm_src.is_file():
        return
    import shutil

    dem = tmp_path / "dem.tif"
    dsm = tmp_path / "dsm.tif"
    shutil.copy2(dem_src, dem)
    shutil.copy2(dsm_src, dsm)
    dest = tmp_path / "chm.tif"
    out = _gdal_chm(dem, dsm, dest)
    assert out.is_file()
    assert out.stat().st_size > 1000
