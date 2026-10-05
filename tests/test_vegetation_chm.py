"""Tests for CHM vegetation classification."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from app.pipeline.vegetation_chm import (
    CHM_GREEN_DENSE_MAX_M,
    CHM_GREEN_LIGHT_MAX_M,
    CHM_GREEN_MID_MAX_M,
    CHM_OPEN_MAX_M,
    OPEN_REINFORCE_ITERS,
    WHITE_EDGE_STRICT_M,
    WHITE_MORPH_OPEN_ITERS,
    ChmVegeThresholds,
    classify_chm_array,
    cleanup_white_forest,
    generate_job_vegetation_chm,
    reinforce_open_land,
)


def test_default_thresholds_calibrated_vs_kp():
    """Po opravě CHM (louka = 0 m): pásy zeleně v zónách 1–5.5 m."""
    assert CHM_OPEN_MAX_M <= 1.5
    assert CHM_OPEN_MAX_M < CHM_GREEN_LIGHT_MAX_M < CHM_GREEN_MID_MAX_M
    assert 5.0 <= CHM_GREEN_DENSE_MAX_M <= 8.0
    assert WHITE_EDGE_STRICT_M >= CHM_GREEN_DENSE_MAX_M + 5.0
    assert WHITE_MORPH_OPEN_ITERS >= 1
    assert OPEN_REINFORCE_ITERS >= 0


def test_green_min_area_constants_legacy():
    """Legacy soft_min konstanty (dokumentace); keep_veg_polygon je off."""
    from app.pipeline.vegetation_chm import (
        _MIN_AREA_M2,
        _MIN_GREEN_AREA_M2,
        _min_area_for_code,
    )
    from app.pipeline.veg_size_filter import VEG_SIZE_FILTER_ENABLED, keep_veg_polygon
    from shapely.geometry import box

    assert VEG_SIZE_FILTER_ENABLED is False
    assert _MIN_AREA_M2 == pytest.approx(12.0)
    assert _MIN_GREEN_AREA_M2 == pytest.approx(25.0)
    assert _min_area_for_code("401") == pytest.approx(12.0)
    assert _min_area_for_code("408") == pytest.approx(25.0)
    assert keep_veg_polygon("408", box(0, 0, 3, 3))[0] is True


def test_classify_open_vs_white_thresholds():
    """Louka (nízké CHM) → 401; vysoký porost → bílý les (0), ne bleed."""
    thr = ChmVegeThresholds(median_size=0, open_reinforce_iters=0)
    # meadow – CHM residual trávy pod open_max
    assert thr.classify_height(0.3) == 1
    assert thr.classify_height(CHM_OPEN_MAX_M - 0.01) == 1
    # scrub / greens (úzké pásy nad open)
    assert thr.classify_height(CHM_OPEN_MAX_M + 0.2) == 2
    assert thr.classify_height((CHM_GREEN_LIGHT_MAX_M + CHM_GREEN_MID_MAX_M) / 2) == 3
    assert thr.classify_height((CHM_GREEN_MID_MAX_M + CHM_GREEN_DENSE_MAX_M) / 2) == 4
    # white forest – above dense max, not open
    assert thr.classify_height(CHM_GREEN_DENSE_MAX_M) == 0
    assert thr.classify_height(20.0) == 0


def test_classify_chm_array_bands():
    thr = ChmVegeThresholds(median_size=0, open_reinforce_iters=0)
    chm = np.array(
        [
            [0.5, thr.open_max_m + 0.3, thr.green_light_max_m + 0.3],
            [thr.green_mid_max_m + 0.5, thr.green_dense_max_m + 2.0, -9999.0],
        ],
        dtype=np.float32,
    )
    out = classify_chm_array(chm, nodata=-9999.0, thresholds=thr)
    assert out[0, 0] == 1  # open
    assert out[0, 1] == 2  # light green
    assert out[0, 2] == 3  # mid
    assert out[1, 0] == 4  # dense
    # white-range single pixel u open → 401 (strom v louce) nebo 0/4
    assert out[1, 1] in (0, 1, 4)
    assert out[1, 2] == 0  # nodata


def test_white_tree_in_meadow_becomes_open():
    """Izolovaná koruna uprostřed louky → 401 (ne bílý les / 410)."""
    h = np.full((5, 5), 0.5, dtype=np.float32)
    h[2, 2] = 16.0  # above dense max, below edge-strict
    classified = np.ones((5, 5), dtype=np.uint8)
    classified[2, 2] = 0
    thr = ChmVegeThresholds(
        green_dense_max_m=12.0,
        white_edge_strict_m=22.0,
        white_morph_iters=2,
        median_size=0,
        open_reinforce_iters=0,
    )
    out = cleanup_white_forest(classified, h, thresholds=thr)
    assert out[2, 2] == 1


def test_white_forest_edge_demoted_to_dense_not_open():
    """Okraj bílého lesa do louky → 410, ne roztažení 401 do lesa."""
    h = np.full((7, 7), 0.5, dtype=np.float32)
    h[:, 3:] = 16.0  # right half = canopy below edge-strict
    classified = np.ones((7, 7), dtype=np.uint8)
    classified[:, 3:] = 0
    thr = ChmVegeThresholds(
        green_dense_max_m=12.0,
        white_edge_strict_m=22.0,
        white_morph_iters=1,
        median_size=0,
        open_reinforce_iters=0,
    )
    out = cleanup_white_forest(classified, h, thresholds=thr)
    # na styku louka|les: demoted → 410; hlouběji v „lese“ může zůstat 0/4
    assert out[3, 3] in (0, 4)
    assert out[3, 0] == 1


def test_white_core_kept_when_strict_and_clustered():
    """Souvislý vysoký porost (≥ edge strict) zůstane bílý."""
    h = np.full((9, 9), 0.5, dtype=np.float32)
    h[2:7, 2:7] = 25.0
    classified = np.ones((9, 9), dtype=np.uint8)
    classified[2:7, 2:7] = 0
    thr = ChmVegeThresholds(
        green_dense_max_m=12.0,
        white_edge_strict_m=22.0,
        white_morph_iters=2,
        median_size=0,
        open_reinforce_iters=0,
    )
    out = cleanup_white_forest(classified, h, thresholds=thr)
    assert out[4, 4] == 0


def test_reinforce_open_pulls_scrub_in_meadow():
    """Nízký 406 uprostřed louky → 401."""
    h = np.full((7, 7), 1.0, dtype=np.float32)
    classified = np.ones((7, 7), dtype=np.uint8)
    classified[3, 3] = 2  # light green speck
    h[3, 3] = 4.5
    thr = ChmVegeThresholds(
        open_max_m=4.0,
        green_light_max_m=5.5,
        green_mid_max_m=8.5,
        open_reinforce_iters=2,
        median_size=0,
    )
    out = reinforce_open_land(classified, h, thresholds=thr)
    assert out[3, 3] == 1


def test_median_smooth_reduces_salt(tmp_path: Path):
    """Median filtr: izolovaný pixel zeleně v louce zmizí."""
    del tmp_path
    chm = np.full((9, 9), 0.3, dtype=np.float32)  # open
    chm[4, 4] = CHM_OPEN_MAX_M + 0.8  # single light-green speck
    out = classify_chm_array(
        chm, nodata=None, thresholds=ChmVegeThresholds(median_size=5)
    )
    assert out[4, 4] == 1  # smoothed / reinforced back to open


def test_generate_job_vegetation_chm_missing(tmp_path: Path):
    assert generate_job_vegetation_chm(tmp_path) is None


def test_generate_job_vegetation_chm_calls_generator(tmp_path: Path):
    dem = tmp_path / "dem"
    dem.mkdir()
    chm = dem / "chm.tif"
    chm.write_bytes(b"chm" * 200)
    dest_marker = tmp_path / "vegetation" / "vegetation.shp"

    def fake_gen(
        src,
        dest,
        *,
        thresholds=None,
        tint_png=None,
        log=None,
        veg_size_profile="default",
    ):
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
