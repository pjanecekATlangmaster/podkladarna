"""Tests for shade stack + preview compose without KP."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.pipeline.georef import png_pixel_size, read_pgw
from app.pipeline.job_grid import write_job_grid
from app.pipeline.preview import (
    compose_job_preview,
    ensure_georef_template,
    has_preview,
    resolve_preview_png,
    write_solid_gray_png,
)
from app.pipeline.shade import (
    SHADE_PNG_NAME,
    build_hillshade_from_dem,
    build_job_shade,
    dem_filled_path,
    resolve_shade_png,
    shade_work_dir,
)


def test_resolve_preview_prefers_preview_png(tmp_path: Path):
    out = tmp_path / "output"
    out.mkdir()
    (out / "pullautus.png").write_bytes(b"kp")
    (out / "preview.png").write_bytes(b"own")
    assert resolve_preview_png(out).name == "preview.png"
    assert has_preview(out)


def test_ensure_georef_from_job_grid_placeholder(tmp_path: Path):
    write_job_grid(
        tmp_path,
        (-700005.0, -1050005.0, -700000.0, -1050000.0),
        resolution_m=1.0,
    )
    pair = ensure_georef_template(tmp_path)
    assert pair is not None
    png, pgw = pair
    assert png.name == "preview.png"
    assert png.is_file() and pgw.is_file()
    w, h = png_pixel_size(png)
    assert w == 5 and h == 5
    georef = read_pgw(pgw)
    assert georef.pixel_x == 1.0
    assert georef.pixel_y == -1.0


def test_ensure_georef_prefers_existing_pullautus(tmp_path: Path):
    write_solid_gray_png(tmp_path / "pullautus.png", 4, 4)
    (tmp_path / "pullautus.pgw").write_text(
        "1\n0\n0\n-1\n-700000\n-1050000\n", encoding="utf-8"
    )
    write_job_grid(
        tmp_path,
        (-700010.0, -1050010.0, -700000.0, -1050000.0),
        resolution_m=1.0,
    )
    png, pgw = ensure_georef_template(tmp_path)
    assert png.name == "pullautus.png"
    assert pgw.name == "pullautus.pgw"


def test_build_job_shade_from_dem_mock(tmp_path: Path):
    write_job_grid(
        tmp_path,
        (-700005.0, -1050005.0, -700000.0, -1050000.0),
        resolution_m=1.0,
    )
    dem_dir = tmp_path / "dem"
    dem_dir.mkdir()
    dem = dem_dir / "dem_filled.tif"
    dem.write_bytes(b"dem" * 200)
    assert dem_filled_path(tmp_path) == dem

    def fake_from_dem(dem_tif, dest_png, dest_pgw, grid, *, log=None, **_kw):
        write_solid_gray_png(dest_png, grid.width, grid.height, gray=120)
        grid.to_pgw().write(dest_pgw)
        return True

    with patch(
        "app.pipeline.shade.build_hillshade_from_dem", side_effect=fake_from_dem
    ):
        out = build_job_shade(tmp_path, prefer_local=True)

    assert out is not None
    assert out.name == SHADE_PNG_NAME
    assert resolve_shade_png(tmp_path) == out
    assert (shade_work_dir(tmp_path) / "hillshade.pgw").is_file()


def test_build_job_shade_wms_fallback(tmp_path: Path):
    write_job_grid(
        tmp_path,
        (-700005.0, -1050005.0, -700000.0, -1050000.0),
        resolution_m=1.0,
    )

    def fake_wms(bounds, grid, dest_png, dest_pgw, *, log=None, **_kw):
        write_solid_gray_png(dest_png, grid.width, grid.height, gray=90)
        grid.to_pgw().write(dest_pgw)
        return True

    with (
        patch("app.pipeline.shade.dem_filled_path", return_value=None),
        patch(
            "app.pipeline.shade.fetch_hillshade_wms_for_grid", side_effect=fake_wms
        ),
    ):
        out = build_job_shade(tmp_path, prefer_local=True)

    assert out is not None
    assert out.is_file()


def test_compose_preview_without_kp(tmp_path: Path):
    write_job_grid(
        tmp_path,
        (-700005.0, -1050005.0, -700000.0, -1050000.0),
        resolution_m=1.0,
    )
    shade_dir = shade_work_dir(tmp_path)
    write_solid_gray_png(shade_dir / SHADE_PNG_NAME, 5, 5, gray=100)
    (shade_dir / "hillshade.pgw").write_text(
        "1\n0\n0\n-1\n-700005\n-1050000\n", encoding="utf-8"
    )

    pair = compose_job_preview(tmp_path, force=True, prefer_kp_pullautus=False)
    assert pair is not None
    png, pgw = pair
    assert png.name == "preview.png"
    assert png.is_file() and pgw.is_file()
    assert png_pixel_size(png) == (5, 5)


def test_compose_preview_keeps_pullautus_when_hybrid(tmp_path: Path):
    write_job_grid(
        tmp_path,
        (-700005.0, -1050005.0, -700000.0, -1050000.0),
        resolution_m=1.0,
    )
    write_solid_gray_png(tmp_path / "pullautus.png", 5, 5, gray=50)
    (tmp_path / "pullautus.pgw").write_text(
        "1\n0\n0\n-1\n-700005\n-1050000\n", encoding="utf-8"
    )
    pair = compose_job_preview(tmp_path, prefer_kp_pullautus=True)
    assert pair is not None
    assert pair[0].name == "pullautus.png"


def test_compose_preview_placeholder_when_no_shade(tmp_path: Path):
    write_job_grid(
        tmp_path,
        (-700005.0, -1050005.0, -700000.0, -1050000.0),
        resolution_m=1.0,
    )
    with patch("app.pipeline.shade.build_job_shade", return_value=None):
        pair = compose_job_preview(tmp_path, force=True)
    assert pair is not None
    assert pair[0].name == "preview.png"
    assert png_pixel_size(pair[0]) == (5, 5)


def test_build_hillshade_from_dem_invokes_gdal(tmp_path: Path):
    """Mock gdaldem/gdalwarp – ověří volání a zápis PNG+PGW."""
    from app.pipeline.job_grid import JobGrid

    grid = JobGrid(
        xmin=-10.0,
        ymin=-10.0,
        xmax=0.0,
        ymax=0.0,
        resolution_m=1.0,
        width=10,
        height=10,
    )
    dem = tmp_path / "dem.tif"
    dem.write_bytes(b"x" * 600)
    dest_png = tmp_path / "out.png"
    dest_pgw = tmp_path / "out.pgw"
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append([str(c) for c in cmd])
        # Po gdaldem vznikne tif; po gdalwarp PNG.
        if "hillshade" in cmd:
            Path(cmd[3]).write_bytes(b"tif" * 200)
        elif any("PNG" == str(c) for c in cmd) or "-of" in cmd:
            # poslední arg = dest
            write_solid_gray_png(Path(cmd[-1]), grid.width, grid.height)

    with (
        patch("app.pipeline.shade._gdal_tool", side_effect=lambda n: n),
        patch("app.pipeline.shade.run_cmd", side_effect=fake_run),
    ):
        ok = build_hillshade_from_dem(dem, dest_png, dest_pgw, grid)

    assert ok
    assert dest_png.is_file() and dest_pgw.is_file()
    assert any("gdaldem" in c[0] or c[0] == "gdaldem" for c in calls)
    assert any("gdalwarp" in c[0] or c[0] == "gdalwarp" for c in calls)
