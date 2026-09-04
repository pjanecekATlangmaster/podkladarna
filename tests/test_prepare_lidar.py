from __future__ import annotations

import subprocess

from app.pipeline.prepare_lidar import (
    ensure_contains_bounds,
    expand_crop_bounds,
    is_kp_heightmap_oob,
    kp_pad_crop_bounds,
    kp_safe_crop_bounds,
)


def test_kp_pad_expands_sprint_scale():
    xmin, ymin, xmax, ymax = kp_pad_crop_bounds((0.0, 0.0, 2000.0, 1000.0), 0.4)
    # scale=0.8 m → pad cca 0.85 m ven
    assert xmin < -0.5
    assert ymin < -0.5
    assert xmax > 2000.5
    assert ymax > 1000.5


def test_kp_safe_crop_is_expand_alias():
    """Dřívější inset by zmenšoval výběr – alias musí expandovat."""
    xmin, ymin, xmax, ymax = kp_safe_crop_bounds(
        (0.0, 0.0, 2000.0, 1000.0), 0.4, extra_inset_m=2.0
    )
    assert xmin < -2.0
    assert xmax > 2002.0


def test_kp_oob_pads_stay_near_user_crop():
    """Retry pad musí zůstat u výběru – ne skok na celé SM5 (km)."""
    crop = (0.0, 0.0, 2000.0, 1000.0)
    for extra in (50.0, 150.0, 300.0, 600.0):
        xmin, ymin, xmax, ymax = kp_pad_crop_bounds(crop, 0.4, extra_pad_m=extra)
        assert xmin >= -extra - 2.0
        assert xmax <= 2000.0 + extra + 2.0
        assert (xmax - xmin) < 4000.0


def test_expand_and_ensure_contains():
    assert expand_crop_bounds((0.0, 0.0, 10.0, 10.0), 5.0) == (-5.0, -5.0, 15.0, 15.0)
    assert ensure_contains_bounds((-100.0, -50.0, 0.0, 0.0), (0.0, 0.0, 10.0, 20.0)) == (
        -100.0,
        -50.0,
        10.0,
        20.0,
    )


def test_is_kp_heightmap_oob():
    err = subprocess.CalledProcessError(
        101,
        ["pullauta"],
        output="",
        stderr=(
            "thread 'main' panicked at src/contours.rs:95:49:\n"
            "index out of bounds: the len is (2052, 1079) but the index is (1974, 1079)\n"
        ),
    )
    assert is_kp_heightmap_oob(err)
    assert not is_kp_heightmap_oob(subprocess.CalledProcessError(1, ["x"], stderr="boom"))
