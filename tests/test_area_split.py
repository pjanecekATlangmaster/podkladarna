"""Rozřezání velkých ploch (OCAD a objekty s tisíci vrcholy)."""

from __future__ import annotations

import math

from shapely.geometry import Polygon

from app.pipeline.area_split import split_area
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


def _seg(ring, step=5.0):
    """Zahustí obrys (jako schodovitý okraj z rastru – body každých pár metrů)."""
    from shapely.geometry import LinearRing

    import shapely

    return list(shapely.segmentize(LinearRing(ring), step).coords)


def _dumbbell():
    # Dva čtverce 200 m spojené krčkem 100 × 20 m.
    return _seg(
        [
            (0, 0), (200, 0), (200, 90), (300, 90), (300, 0), (500, 0),
            (500, 200), (300, 200), (300, 110), (200, 110), (200, 200), (0, 200),
        ]
    )


def _check_partition(pieces, ring, holes=()):
    total = sum(Polygon(o, hs).area for o, hs in pieces)
    assert math.isclose(total, Polygon(ring, list(holes)).area, rel_tol=1e-9)
    for o, hs in pieces:
        assert Polygon(o, hs).is_valid


def test_dumbbell_is_cut_in_the_neck():
    ring = _dumbbell()
    pieces = split_area(ring, [], 10_000, max_area=50_000)
    assert len(pieces) == 2
    _check_partition(pieces, ring)
    left, right = sorted(pieces, key=lambda pc: Polygon(pc[0]).bounds[0])
    # Řez leží v krčku (x 200–300), ne napříč čtvercem.
    assert 200 <= Polygon(left[0]).bounds[2] <= 300
    assert 200 <= Polygon(right[0]).bounds[0] <= 300
    assert Polygon(left[0]).area > 40_000 and Polygon(right[0]).area > 40_000


def test_dumbbell_under_limits_is_not_cut():
    ring = _dumbbell()
    assert split_area(ring, [], 10_000, max_area=100_000) == [(ring, [])]
    assert split_area(ring, [], 10_000) == [(ring, [])]


def test_vertex_limit_alone_also_prefers_the_neck():
    ring = _dumbbell()
    pieces = split_area(ring, [], len(ring) - 10)
    assert len(pieces) == 2
    _check_partition(pieces, ring)
    for o, _ in pieces:
        assert len(o) <= len(ring) - 10


def test_neck_between_outline_and_hole():
    # Obdélník 600 × 200 m, díra (lesní ostrůvek) blízko pravého konce nechává
    # nad i pod sebou jen pruhy 15 m → řez dvěma krátkými úsečkami přes díru.
    ring = _seg([(0, 0), (600, 0), (600, 200), (0, 200)])
    hole = _seg([(450, 15), (520, 15), (520, 185), (450, 185)])
    pieces = split_area(ring, [hole], 10_000, max_area=100_000)
    assert len(pieces) == 2
    _check_partition(pieces, ring, [hole])
    small = min(pieces, key=lambda pc: Polygon(*pc).area)
    assert 450 <= Polygon(small[0]).bounds[0] <= 520
    assert all(not hs for _, hs in pieces)  # díra je otevřená řezy


def test_compact_area_over_limit_gets_straight_cuts_without_shards():
    ring = _wiggly_ring(2000, r=300.0)
    hole = _wiggly_ring(200, r=40.0)
    area = Polygon(ring, [hole]).area
    pieces = split_area(ring, [hole], 1500, max_area=60_000)
    assert len(pieces) >= area / 60_000
    _check_partition(pieces, ring, [hole])
    areas = [Polygon(o, hs).area for o, hs in pieces]
    assert max(areas) <= 60_000
    assert min(areas) >= 2_500  # žádné střepy
    # Díra (uprostřed) přežila – buď celá v jednom dílu, nebo rozříznutá řezy.
    assert math.isclose(sum(areas), area, rel_tol=1e-9)


def test_compact_area_under_limits_is_untouched():
    ring = _wiggly_ring(800, r=100.0)
    hole = _wiggly_ring(100, r=20.0)
    assert split_area(ring, [hole], 1500, max_area=60_000) == [(ring, [hole])]


def test_small_hole_is_kept_in_piece():
    ring = _seg([(0, 0), (500, 0), (500, 200), (0, 200)])
    hole = _wiggly_ring(60, r=10.0)
    hole = [(x + 100, y + 100) for x, y in hole]
    pieces = split_area(ring, [hole], 10_000, max_area=60_000)
    assert len(pieces) >= 2
    _check_partition(pieces, ring, [hole])
    assert sum(len(hs) for _, hs in pieces) == 1


def test_geom_parts_to_objects_passes_area_limit():
    ring = _dumbbell()
    kw = dict(ref_x=0.0, ref_y=0.0, scale=10000, grivation_deg=0.0, as_area=True)
    one = _geom_parts_to_objects([("line", ring, True)], 7, max_area_vertices=1500, **kw)
    two = _geom_parts_to_objects(
        [("line", ring, True)], 7, max_area_vertices=1500, max_area_m2=50_000, **kw
    )
    assert len(one) == 1
    assert len(two) == 2