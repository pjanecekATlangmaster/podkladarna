from __future__ import annotations

import pytest

from app.pipeline.geom_clip import (
    clip_polyline,
    clip_ring,
    expand,
    point_inside,
)

BOX = (0.0, 0.0, 100.0, 100.0)


def _area(pts) -> float:
    s = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def test_point_inside_and_expand():
    assert point_inside(50.0, 50.0, BOX)
    assert not point_inside(-1.0, 50.0, BOX)
    assert point_inside(-1.0, 50.0, expand(BOX, 25.0))


def test_line_fully_inside_is_untouched():
    pts = [(10.0, 10.0), (50.0, 50.0), (90.0, 20.0)]
    assert clip_polyline(pts, BOX) == [pts]


def test_line_fully_outside_disappears():
    assert clip_polyline([(-50.0, -50.0), (-10.0, -20.0)], BOX) == []


def test_line_crossing_border_is_cut_at_edge():
    got = clip_polyline([(-20.0, 50.0), (60.0, 50.0)], BOX)
    assert len(got) == 1
    assert got[0][0] == pytest.approx((0.0, 50.0))
    assert got[0][1] == pytest.approx((60.0, 50.0))


def test_line_leaving_and_returning_splits_in_two():
    """Vrstevnice, co vyběhne z mapy a vrátí se, nesmí zůstat propojená."""
    pts = [(10.0, 50.0), (40.0, 50.0), (40.0, 150.0), (70.0, 150.0), (70.0, 50.0), (90.0, 50.0)]
    got = clip_polyline(pts, BOX)
    assert len(got) == 2
    assert all(y <= 100.0 + 1e-9 for part in got for _, y in part)


def test_line_along_border_survives():
    got = clip_polyline([(0.0, 0.0), (100.0, 0.0)], BOX)
    assert len(got) == 1


def test_ring_fully_inside_is_untouched():
    ring = [(10.0, 10.0), (40.0, 10.0), (40.0, 40.0), (10.0, 40.0)]
    assert _area(clip_ring(ring, BOX)) == pytest.approx(900.0)


def test_ring_fully_outside_disappears():
    ring = [(200.0, 200.0), (240.0, 200.0), (240.0, 240.0)]
    assert clip_ring(ring, BOX) == []


def test_ring_overlapping_border_keeps_only_inside_area():
    ring = [(50.0, 50.0), (150.0, 50.0), (150.0, 150.0), (50.0, 150.0)]
    got = clip_ring(ring, BOX)
    assert _area(got) == pytest.approx(2500.0)
    assert all(x <= 100.0 + 1e-9 and y <= 100.0 + 1e-9 for x, y in got)


def test_ring_larger_than_box_becomes_the_box():
    ring = [(-50.0, -50.0), (150.0, -50.0), (150.0, 150.0), (-50.0, 150.0)]
    assert _area(clip_ring(ring, BOX)) == pytest.approx(10000.0)


def test_closed_ring_input_is_not_duplicated():
    ring = [(10.0, 10.0), (40.0, 10.0), (40.0, 40.0), (10.0, 40.0), (10.0, 10.0)]
    got = clip_ring(ring, BOX)
    assert _area(got) == pytest.approx(900.0)
    assert got[0] != got[-1]


def test_degenerate_inputs_do_not_crash():
    assert clip_ring([(1.0, 1.0), (2.0, 2.0)], BOX) == []
    assert clip_polyline([], BOX) == []
    assert clip_polyline([(5.0, 5.0)], BOX) == [[(5.0, 5.0)]]
    assert clip_polyline([(-5.0, 5.0)], BOX) == []
