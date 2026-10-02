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

    def fake_pdal(laz, bounds, dest, *, resolution_m=1.0, log=None, **kw):
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

    pdal_calls: list[tuple[str, dict]] = []
    chm_inputs: list[str] = []
    fill_calls: list[str] = []

    def fake_pdal(laz, bounds, dest, *, resolution_m=1.0, log=None, **kw):
        pdal_calls.append((dest.name, kw))
        dest.write_bytes(b"tif" * 200)

    def fake_fill(src, dest, *, log=None):
        fill_calls.append(src.name)
        dest.write_bytes(b"filled" * 50)

    def fake_chm(dem, dsm, dest, *, log=None):
        chm_inputs.append(dsm.name)
        dest.write_bytes(b"chm" * 50)
        return dest

    bounds = (0.0, 0.0, 10.0, 10.0)
    with (
        patch("app.pipeline.dem_prep._pdal_dem_from_laz", side_effect=fake_pdal),
        patch("app.pipeline.dem_prep._fill_dem_nodata", side_effect=fake_fill),
        patch("app.pipeline.dem_prep._gdal_chm", side_effect=fake_chm),
        patch("app.pipeline.dem_prep._raster_grid", return_value=(0.0, 0.0, 10, 10)),
    ):
        result = prepare_job_surfaces(work, bounds)

    # dsm_filled se už negeneruje (fillnodata by kazil CHM); CHM z raw DSM.
    assert result.dsm_filled is None
    assert not (dem_work_dir(work) / "dsm_filled.tif").exists()
    assert result.chm is not None and result.chm.is_file()
    assert result.surface_laz == "veg_merged.laz"
    # DSM na mřížce DEM; CHM z raw DSM (ne fillnodata přes louky)
    assert dict(pdal_calls)["dsm_raw.tif"] == {"grid": (0.0, 0.0, 10, 10)}
    assert chm_inputs == ["dsm_raw.tif"]
    # fillnodata jen pro DEM, ne pro DSM
    assert fill_calls == ["dem_raw.tif"]


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


def _write_asc(path: Path, rows: list[list[float]]) -> None:
    lines = [
        f"ncols {len(rows[0])}",
        f"nrows {len(rows)}",
        "xllcorner 0",
        "yllcorner 0",
        "cellsize 1",
        "NODATA_value -9999",
    ] + [" ".join(str(v) for v in r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def test_gdal_chm_open_cells_are_zero_not_interpolated(tmp_path: Path):
    """DSM NoData (žádný DMP vegetační bod) = louka → CHM 0, záporné → 0."""
    from app.pipeline.dem_prep import _gdal_chm
    from app.pipeline.gdal_cli_raster import read_float32_geotiff
    from app.tool_env import which_tool

    if not which_tool("gdal_calc") or not which_tool("gdal_translate"):
        return
    from app.pipeline.prepare_lidar import run_cmd

    n = 20
    dem_rows = [[100.0] * n for _ in range(n)]
    dsm_rows = [[-9999.0] * n for _ in range(n)]
    dem_rows[1][1] = -9999.0
    dsm_rows[1][1] = 130.0
    dsm_rows[0][1] = 120.0
    dsm_rows[1][0] = 99.5
    tifs = {}
    for name, rows in (("dem", dem_rows), ("dsm", dsm_rows)):
        asc = tmp_path / f"{name}.asc"
        _write_asc(asc, rows)
        tif = tmp_path / f"{name}.tif"
        run_cmd([which_tool("gdal_translate"), "-of", "GTiff", "-ot", "Float32", str(asc), str(tif)])
        tifs[name] = tif
    out = _gdal_chm(tifs["dem"], tifs["dsm"], tmp_path / "chm.tif")
    arr, _gt, nodata = read_float32_geotiff(out)
    assert arr[0, 0] == 0.0
    assert arr[0, 1] == 20.0
    assert arr[1, 0] == 0.0
    assert arr[1, 1] == nodata
