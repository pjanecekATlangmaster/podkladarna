from __future__ import annotations

import subprocess

from app.pipeline.prepare_lidar import is_kp_heightmap_oob, kp_safe_crop_bounds


def test_kp_safe_crop_insets_sprint_scale():
    xmin, ymin, xmax, ymax = kp_safe_crop_bounds((0.0, 0.0, 2000.0, 1000.0), 0.4)
    # scale=0.8 m → inset cca 0.46 m
    assert 0.4 < xmin < 0.6
    assert 0.4 < ymin < 0.6
    assert 1999.4 < xmax < 1999.6
    assert 999.4 < ymax < 999.6


def test_kp_safe_crop_extra_inset():
    xmin, ymin, xmax, ymax = kp_safe_crop_bounds(
        (0.0, 0.0, 2000.0, 1000.0), 0.4, extra_inset_m=2.0
    )
    assert xmin > 2.0
    assert xmax < 1998.0


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
