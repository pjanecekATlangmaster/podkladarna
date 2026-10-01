"""Unit tests for preview / georef seam without KP hard-dep."""

from __future__ import annotations

from pathlib import Path

from app.pipeline.georef import png_pixel_size, read_pgw
from app.pipeline.job_grid import write_job_grid
from app.pipeline.preview import (
    ensure_georef_template,
    has_preview,
    resolve_preview_png,
    write_solid_gray_png,
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
