from __future__ import annotations

import math

from app.pipeline.cliff_merge import merge_cliff_ticks
from app.pipeline.oom_symbol_map import KP_CLIFF_DENSE_CODE, symbol_index_for_code


def _tick(x0: float, y0: float, x1: float, y1: float):
    return ((x0, y0), (x1, y1))


def test_collinear_ticks_merge_to_one_line():
    # Řada KP čárek podél stěny (směr X, středy po 3 m).
    ticks = [
        _tick(i * 3.0, 0.0, i * 3.0 + 2.9, 0.0) for i in range(8)
    ]
    got = merge_cliff_ticks(ticks, as_polygons=False)
    assert len(got.lines) == 1
    assert not got.polygons
    length = sum(
        math.hypot(
            got.lines[0][i][0] - got.lines[0][i - 1][0],
            got.lines[0][i][1] - got.lines[0][i - 1][1],
        )
        for i in range(1, len(got.lines[0]))
    )
    assert length >= 18.0
    assert len(got.lines[0]) < len(ticks) + 3


def test_parallel_cliffs_stay_two_lines():
    a = [_tick(i * 3.0, 0.0, i * 3.0 + 2.9, 0.0) for i in range(5)]
    b = [_tick(i * 3.0, 12.0, i * 3.0 + 2.9, 12.0) for i in range(5)]
    got = merge_cliff_ticks(a + b, as_polygons=False)
    assert len(got.lines) == 2


def test_isolated_tick_stays_short_line():
    got = merge_cliff_ticks([_tick(0.0, 0.0, 2.9, 0.0)], as_polygons=False)
    assert len(got.lines) == 1
    assert len(got.lines[0]) == 2
    assert not got.polygons


def test_perpendicular_neighbor_does_not_join():
    along = [_tick(i * 3.0, 0.0, i * 3.0 + 2.9, 0.0) for i in range(4)]
    across = _tick(4.5, -1.45, 4.5, 1.45)
    got = merge_cliff_ticks(along + [across], as_polygons=False)
    assert len(got.lines) == 2


def test_dense_blob_becomes_polygon_when_rock():
    ticks = []
    for i in range(5):
        for j in range(5):
            # Křížící se krátké čárky v 12×12 m – žádný dlouhý sráz.
            x, y = i * 2.4, j * 2.4
            if (i + j) % 2 == 0:
                ticks.append(_tick(x, y, x + 2.5, y + 0.2))
            else:
                ticks.append(_tick(x, y, x + 0.2, y + 2.5))
    got = merge_cliff_ticks(ticks, as_polygons=True)
    assert got.polygons
    assert sum(_ring_area(p) for p in got.polygons) >= 45.0
    # Místo 25 objektů jedna plocha, bez dlouhého hada z čárek.
    assert len(got.lines) <= 4
    assert len(got.polygons) <= 3


def test_dense_blob_stays_lines_for_earth_bank():
    ticks = []
    for i in range(5):
        for j in range(5):
            x, y = i * 2.4, j * 2.4
            ticks.append(_tick(x, y, x + 2.5, y))
    got = merge_cliff_ticks(ticks, as_polygons=False)
    assert not got.polygons
    assert got.lines


def test_dense_symbol_exists_in_both_sets():
    assert symbol_index_for_code("sprint_2m", 4000, KP_CLIFF_DENSE_CODE) is not None
    assert symbol_index_for_code("forest_10000", 10000, KP_CLIFF_DENSE_CODE) is not None


def _ring_area(pts) -> float:
    s = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5
