"""Tests for CHM vegetation classification (bez KP)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.pipeline.vegetation_chm import (
    CHM_GREEN_DENSE_MAX_M,
    CHM_OPEN_MAX_M,
    ChmVegeThresholds,
    classify_chm_array,
    generate_job_vegetation_chm,
)


def test_classify_open_vs_white_thresholds():
    """Louka (nízké CHM) → 401; vysoký porost → bílý les (0), ne bleed."""
    thr = ChmVegeThresholds()
    # meadow
    assert thr.classify_height(0.3) == 1
    assert thr.classify_height(CHM_OPEN_MAX_M - 0.01) == 1
    # scrub / greens
    assert thr.classify_height(2.0) == 2
    assert thr.classify_height(5.0) == 3
    assert thr.classify_height(10.0) == 4
    # white forest – above dense max, not open
    assert thr.classify_height(CHM_GREEN_DENSE_MAX_M) == 0
    assert thr.classify_height(20.0) == 0


def test_classify_chm_array_bands():
    chm = np.array(
        [
            [0.5, 2.0, 5.0],
            [10.0, 15.0, -9999.0],
        ],
        dtype=np.float32,
    )
    out = classify_chm_array(chm, nodata=-9999.0)
    assert out[0, 0] == 1  # open
    assert out[0, 1] == 2  # light green
    assert out[0, 2] == 3  # mid
    assert out[1, 0] == 4  # dense
    assert out[1, 1] == 0  # white
    assert out[1, 2] == 0  # nodata


def test_generate_job_vegetation_chm_missing(tmp_path: Path):
    assert generate_job_vegetation_chm(tmp_path) is None


def test_generate_job_vegetation_chm_calls_generator(tmp_path: Path):
    dem = tmp_path / "dem"
    dem.mkdir()
    chm = dem / "chm.tif"
    chm.write_bytes(b"chm" * 200)
    dest_marker = tmp_path / "vegetation" / "vegetation.shp"

    def fake_gen(src, dest, *, thresholds=None, tint_png=None, log=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"shp")
        if tint_png:
            tint_png.write_bytes(b"png")
        return dest

    with patch(
        "app.pipeline.vegetation_chm.generate_vegetation_from_chm",
        side_effect=fake_gen,
    ):
        out = generate_job_vegetation_chm(tmp_path)

    assert out == dest_marker
    assert dest_marker.is_file()
