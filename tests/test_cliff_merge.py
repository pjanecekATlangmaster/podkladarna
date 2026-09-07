from __future__ import annotations

import math

import pytest

from app.pipeline.cliff_merge import merge_cliff_ticks
from app.pipeline.oom_symbol_map import KP_CLIFF_DENSE_CODE, symbol_index_for_code


def _tick(x0: float, y0: float, x1: float, y1: float):
    return ((x0, y0), (x1, y1))


def _wall(n: int, *, x0: float = 0.0, y: float = 0.0, step: float = 1.0):
    """Stěna: KP kreslí čárku kolmo na spád, tedy podél stěny; nahusto za sebou."""
    return [_tick(x0 + i * step, y, x0 + i * step + 2.9, y) for i in range(n)]


def _field(width_m: float, height_m: float, *, x0: float = 0.0, y0: float = 0.0):
    """Skalní pole: čárky nahusto a v obou směrech (2D útvar, ne stěna)."""
    ticks = []
    step = 1.5
    nx = int(width_m / step)
    ny = int(height_m / step)
    for i in range(nx):
        for j in range(ny):
            x, y = x0 + i * step, y0 + j * step
            if (i + j) % 2 == 0:
                ticks.append(_tick(x, y, x + 1.4, y + 0.3))
            else:
                ticks.append(_tick(x, y, x + 0.3, y + 1.4))
    return ticks


def _length(pts) -> float:
    return sum(
        math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
        for i in range(1, len(pts))
    )


def _ring_area(pts) -> float:
    s = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def test_collinear_ticks_merge_to_one_line():
    got = merge_cliff_ticks(
        [_tick(i * 3.0, 0.0, i * 3.0 + 2.9, 0.0) for i in range(8)],
        as_polygons=False,
    )
    assert len(got.lines) == 1
    assert not got.polygons
    assert _length(got.lines[0]) >= 18.0


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


def test_long_wall_never_becomes_polygon():
    """Hlavní regrese: souvislá stěna musí zůstat linií i u volby skála."""
    got = merge_cliff_ticks(_wall(60), as_polygons=True)
    assert not got.polygons
    assert got.lines


def test_double_wall_band_still_no_polygon():
    """I dvojitý pás (terasa) je 1D útvar – pořád linie."""
    ticks = _wall(40, y=0.0) + _wall(40, y=4.0)
    got = merge_cliff_ticks(ticks, as_polygons=True)
    assert not got.polygons


def test_rock_field_becomes_polygon_close_to_real_extent():
    got = merge_cliff_ticks(_field(21.0, 21.0), as_polygons=True)
    assert got.polygons
    area = sum(_ring_area(p) for p in got.polygons)
    # Reálný rozsah ~21×21 m; obrys po buňkách nesmí nafouknout o víc než ~30 %.
    assert 250.0 <= area <= 21.0 * 21.0 * 1.3


def test_two_rock_fields_stay_separate_polygons():
    """Dvě pole 60 m od sebe nesmí splynout do jedné obálky."""
    ticks = _field(18.0, 18.0) + _field(18.0, 18.0, x0=60.0)
    got = merge_cliff_ticks(ticks, as_polygons=True)
    assert len(got.polygons) == 2
    assert sum(_ring_area(p) for p in got.polygons) <= 2 * 18.0 * 18.0 * 1.3


def test_l_shaped_field_keeps_concavity():
    """Záliv v L nesmí být vyplněný – jinak plocha přeteče přes realitu."""
    ticks = _field(30.0, 12.0) + _field(12.0, 30.0)
    got = merge_cliff_ticks(ticks, as_polygons=True)
    assert got.polygons
    area = sum(_ring_area(p) for p in got.polygons)
    bbox = 30.0 * 30.0
    assert area <= bbox * 0.75


def test_wall_touching_field_keeps_wall_as_line():
    """Stěna vybíhající z pole zůstane linií, plocha se na ni nenatáhne."""
    ticks = _field(18.0, 18.0) + _wall(40, x0=21.0, y=9.0)
    got = merge_cliff_ticks(ticks, as_polygons=True)
    assert got.polygons
    assert got.lines
    # Plocha nesmí zasahovat daleko za pole do stěny.
    assert max(x for poly in got.polygons for x, _ in poly) <= 34.0


def test_dense_field_stays_lines_for_earth_bank():
    got = merge_cliff_ticks(_field(21.0, 21.0), as_polygons=False)
    assert not got.polygons
    assert got.lines


def test_object_count_drops_far_below_tick_count():
    ticks = _field(24.0, 24.0) + _wall(60, y=-40.0)
    got = merge_cliff_ticks(ticks, as_polygons=True)
    assert len(got.lines) + len(got.polygons) < len(ticks) / 10


def test_min_line_length_scales_with_map():
    from app.pipeline.cliff_merge import min_line_length_m

    assert min_line_length_m(4000) == pytest.approx(2.4)
    assert min_line_length_m(10000) == pytest.approx(6.0)
    assert min_line_length_m(15000) < 10.0


def test_min_line_drops_stubs_but_keeps_real_cliffs():
    """Kompromis proti šumu: krátké nálezy pryč, dlouhá stěna zůstane celá."""
    wall = _wall(60, y=0.0)
    stubs = [_tick(200.0 + i * 40.0, 300.0, 202.9 + i * 40.0, 300.0) for i in range(12)]
    got = merge_cliff_ticks(wall + stubs, as_polygons=False, min_line_m=6.0)
    assert len(got.lines) == 1
    assert _length(got.lines[0]) >= 55.0


def test_min_line_zero_keeps_everything():
    ticks = [_tick(200.0 + i * 40.0, 300.0, 202.9 + i * 40.0, 300.0) for i in range(5)]
    assert len(merge_cliff_ticks(ticks, as_polygons=False).lines) == 5


def test_min_line_does_not_touch_rock_areas():
    """Délkový práh se týká jen linií – plocha 210 musí zůstat."""
    got = merge_cliff_ticks(_field(21.0, 21.0), as_polygons=True, min_line_m=6.0)
    assert got.polygons


def test_trace_rings_splits_diagonally_touching_lobes():
    from app.pipeline.cliff_merge import _trace_cell_rings

    rings = _trace_cell_rings({(0, 0), (1, 1)}, 3.0)
    assert len(rings) == 2
    for ring in rings:
        assert _ring_area(ring) == 9.0


def test_trace_ring_keeps_hole_as_separate_clockwise_ring():
    from app.pipeline.cliff_merge import _signed_ring_area, _trace_cell_rings

    donut = {(i, j) for i in range(3) for j in range(3)} - {(1, 1)}
    rings = _trace_cell_rings(donut, 3.0)
    assert len(rings) == 2
    assert sorted(round(_signed_ring_area(r), 3) for r in rings) == [-9.0, 81.0]


def test_dense_symbol_exists_in_both_sets():
    assert symbol_index_for_code("sprint_2m", 4000, KP_CLIFF_DENSE_CODE) is not None
    assert symbol_index_for_code("forest_10000", 10000, KP_CLIFF_DENSE_CODE) is not None
