"""Rozřezání velkých ploch (OCAD a objekty s tisíci vrcholy)."""

from __future__ import annotations

import math

from shapely.geometry import Polygon

from app.pipeline.oom_import import _geom_parts_to_objects, split_area_by_vertices


def _wiggly_ring(n: int, r: float = 500.0) -> list[tuple[float, float]]:
    """Kruh s ``n`` vrcholy a zubatým okrajem (jako polygonize z rastru)."""
    pts = [
        (
            (r + 3.0 * ((-1) ** i)) * math.cos(2 * math.pi * i / n),
            (r + 3.0 * ((-1) ** i)) * math.sin(2 * math.pi * i / n),
        )
        for i in range(n)
    ]
    return pts + [pts[0]]


def test_small_area_is_untouched():
    ring = _wiggly_ring(100)
    assert split_area_by_vertices(ring, [], 1500) == [(ring, [])]


def test_big_area_is_split_under_limit_and_keeps_area():
    ring = _wiggly_ring(6000)
    hole = _wiggly_ring(400, r=100.0)
    pieces = split_area_by_vertices(ring, [hole], 1500)
    assert len(pieces) > 1
    for outer, holes in pieces:
        assert len(outer) + sum(len(h) for h in holes) <= 1500
    total = sum(Polygon(o, hs).area for o, hs in pieces)
    assert math.isclose(total, Polygon(ring, [hole]).area, rel_tol=1e-6)


def test_geom_parts_to_objects_splits_only_when_asked():
    ring = _wiggly_ring(6000)
    kw = dict(ref_x=0.0, ref_y=0.0, scale=10000, grivation_deg=0.0, as_area=True)
    whole = _geom_parts_to_objects([("line", ring, True)], 7, **kw)
    split = _geom_parts_to_objects([("line", ring, True)], 7, max_area_vertices=1500, **kw)
    assert len(whole) == 1
    assert len(split) > 1
    assert all(o.count(";") <= 1500 + 2 for o in split)
