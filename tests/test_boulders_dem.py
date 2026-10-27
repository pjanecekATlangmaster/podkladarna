"""Balvany (204) z DEM."""

from __future__ import annotations

import numpy as np
import pytest

from app.pipeline.boulders_dem import (
    detect_boulders,
    filter_boulder_points,
    parse_point_dxf,
)
from app.pipeline.knolls_dem import write_knoll_points_dxf
from app.pipeline.oom_symbol_map import oom_code_for_dxf

GT = (0.0, 1.0, 0.0, 120.0, 0.0, -1.0)


def _slope(grade: float = 0.4) -> np.ndarray:
    yy, xx = np.mgrid[0:120, 0:120]
    return (300.0 + grade * xx).astype(np.float32)


def _bump(height: float, sigma: float, cy: int = 60, cx: int = 60) -> np.ndarray:
    yy, xx = np.mgrid[0:120, 0:120]
    return (height * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * sigma**2))).astype(np.float32)


def _ridge(height: float) -> np.ndarray:
    """Násep / hřbet: zvednutý jen napříč (S–J), podél (V–Z) rovný."""
    yy, _xx = np.mgrid[0:120, 0:120]
    return (height * np.exp(-((yy - 60) ** 2) / (2 * 1.5**2))).astype(np.float32)


@pytest.mark.parametrize(
    "terrain,expected",
    [
        ("rovina", 0),
        ("svah", 0),
        ("balvan 2 m ve svahu", 1),
        ("nízký balvan 0,8 m", 0),
        ("skalní věž 6 m", 0),
        ("násep 2 m", 0),
    ],
)
def test_detect_boulders(terrain, expected):
    arr = {
        "rovina": np.full((120, 120), 100.0, dtype=np.float32),
        "svah": _slope(),
        "balvan 2 m ve svahu": _slope() + _bump(2.0, 1.2),
        "nízký balvan 0,8 m": _slope() + _bump(0.8, 1.2),
        "skalní věž 6 m": _slope() + _bump(6.0, 1.2),
        "násep 2 m": _slope() + _ridge(2.0),
    }[terrain]
    pts = detect_boulders(arr, GT)
    assert len(pts) == expected
    for x, y in pts:
        assert abs(x - 60.5) <= 1.5 and abs(y - (120 - 60.5)) <= 1.5


def test_nodata_edge_is_not_a_boulder():
    arr = _slope()
    arr[:, :60] = -9999.0
    assert detect_boulders(arr, GT, nodata=-9999.0) == []


def test_dxf_roundtrip(tmp_path):
    dest = write_knoll_points_dxf([(1.5, -2.25), (3.0, 4.0)], tmp_path / "b.dxf", layer="BOULDER")
    assert parse_point_dxf(dest) == [(1.5, -2.25), (3.0, 4.0)]


@pytest.mark.parametrize(
    "point,kept",
    [
        ((0.0, 0.0), False),  # 10 m od cesty
        ((0.0, 30.0), True),
        ((51.0, 30.0), False),  # 1 m od skalní plochy
        ((30.0, 60.0), False),  # 5 m od kupky
        ((80.0, 80.0), False),  # 10 m od budovy
    ],
)
def test_filter_boulder_points(point, kept):
    got, _stats = filter_boulder_points(
        [point],
        rock_rings=[[(52.0, 25.0), (60.0, 25.0), (60.0, 35.0), (52.0, 35.0)]],
        blocker_rings=[[(90.0, 70.0), (100.0, 70.0), (100.0, 90.0), (90.0, 90.0)]],
        blocker_lines=[[(-50.0, -10.0), (50.0, -10.0)]],
        knolls=[(30.0, 65.0)],
    )
    assert (got == [point]) is kept


def test_boulder_symbol_only_forest_and_mtbo():
    assert oom_code_for_dxf("dotboulders.dxf", preset_id="forest_10000") == "204"
    assert oom_code_for_dxf("dotboulders.dxf", preset_id="mtbo_10000") == "204"
    assert oom_code_for_dxf("dotboulders.dxf", preset_id="sprint_2m") is None
    assert oom_code_for_dxf("dotboulders.dxf", preset_id="forest_10000", cliff_symbol="off") is None
