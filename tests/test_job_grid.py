"""Unit tests for canonical job georef grid."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.pipeline.job_grid import (
    JobGrid,
    build_job_grid,
    resolve_job_extent,
    snap_bounds_to_grid,
    write_job_grid,
)


def test_snap_bounds_to_grid_aligns():
    xmin, ymin, xmax, ymax, w, h = snap_bounds_to_grid(
        (-700000.4, -1050000.6, -699998.2, -1049998.1), 1.0
    )
    assert xmin == -700001.0
    assert ymin == -1050001.0
    assert xmax == -699998.0
    assert ymax == -1049998.0
    assert w == 3
    assert h == 3


def test_build_and_roundtrip_job_grid(tmp_path: Path):
    grid = write_job_grid(
        tmp_path,
        (-700010.0, -1050010.0, -700000.0, -1050000.0),
        resolution_m=1.0,
    )
    assert grid.width == 10
    assert grid.height == 10
    assert (tmp_path / "job_grid.json").is_file()
    assert (tmp_path / "job.pgw").is_file()
    loaded = JobGrid.load(tmp_path)
    assert loaded is not None
    assert loaded.bounds() == grid.bounds()
    assert loaded.resolution_m == 1.0


def test_resolve_job_extent_prefers_grid(tmp_path: Path):
    write_job_grid(
        tmp_path,
        (-700010.0, -1050010.0, -700000.0, -1050000.0),
        resolution_m=1.0,
    )
    # Fake pullautus with different extent – grid must win.
    (tmp_path / "pullautus.png").write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x02"
        b"\x08\x02\x00\x00\x00\xfd\xd4\x9a\x73\x00\x00\x00\x12IDATx\x9cc\x60\x60"
        b"\x60\x00\x00\x00\x04\x00\x01\x5c\xcd\xff\x69\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    (tmp_path / "pullautus.pgw").write_text(
        "1\n0\n0\n-1\n0\n0\n", encoding="utf-8"
    )
    extent = resolve_job_extent(tmp_path)
    assert extent == (-700010.0, -1050010.0, -700000.0, -1050000.0)


def test_resolve_job_extent_crop_fallback(tmp_path: Path):
    crop = (-1.0, -2.0, 3.0, 4.0)
    assert resolve_job_extent(tmp_path, crop_bounds=crop) == crop
    with pytest.raises(RuntimeError):
        resolve_job_extent(tmp_path)


def test_build_job_grid_rejects_bad_resolution():
    with pytest.raises(ValueError):
        build_job_grid((0.0, 0.0, 10.0, 10.0), resolution_m=0.0)
