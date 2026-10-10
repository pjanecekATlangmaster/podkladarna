"""Zaoblení luk: zjednodušení bez přelití přes překážky (cesty, budovy, voda)."""

from shapely.geometry import LineString, Point, Polygon

from app.pipeline.job_options import resolve_veg_smooth
from app.pipeline.veg_smooth import SmoothBarriers, smooth_area


def _stair_diagonal(n: int = 40, step: float = 1.0):
    """Čtverec, jehož pravý horní okraj tvoří diagonální schody po 1 m."""
    pts = [(0.0, 0.0), (100.0, 0.0)]
    x, y = 100.0, 0.0
    for _ in range(n):
        y += step
        pts.append((x, y))
        x -= step
        pts.append((x, y))
    pts.append((0.0, y))
    pts.append((0.0, 0.0))
    return pts


def test_smooth_reduces_vertices():
    ring = _stair_diagonal()
    out = smooth_area(ring, [], 2.0)
    assert len(out) == 1
    assert len(out[0][0]) < len(ring) / 3


def test_smooth_does_not_grow_into_barrier():
    ring = _stair_diagonal()
    poly = Polygon(ring)
    # cesta těsně podél diagonály schodů – zjednodušení by do ní zasáhlo
    road = LineString([(100, 0), (60, 40)]).buffer(1.5)
    free = Polygon(*smooth_area(ring, [], 2.0)[0])
    guarded = Polygon(*smooth_area(ring, [], 2.0, SmoothBarriers([road]))[0])
    base = poly.intersection(road).area
    assert guarded.intersection(road).area <= base + 1e-6
    assert free.intersection(road).area >= guarded.intersection(road).area


def test_smooth_drops_island_behind_barrier():
    ring = [(0, 0), (50, 0), (50, 50), (0, 50), (0, 0)]
    out = smooth_area(ring, [], 2.0, SmoothBarriers([]))
    assert len(out) == 1


def test_resolve_veg_smooth_default_and_values():
    assert resolve_veg_smooth({}) == 2
    assert resolve_veg_smooth({"veg_smooth": "3"}) == 3
    assert resolve_veg_smooth({"veg_smooth": "ROUND"}) == 1  # starý přepínač
    assert resolve_veg_smooth({"veg_smooth": "off"}) == 0
    assert resolve_veg_smooth({"veg_smooth": "9"}) == 2
    assert resolve_veg_smooth({"veg_smooth": "x"}) == 2
    assert resolve_veg_smooth({"veg_smooth": "0"}) == 0  # vypnuto
    assert resolve_veg_smooth({"veg_smooth": 0}) == 0


def test_higher_level_removes_small_notches():
    # čtverec 80 m s úzkou zátokou (3 m) – stupeň 2 ji zalije
    ring = [(0, 0), (80, 0), (80, 80), (41, 80), (41, 40), (39, 40), (39, 80), (0, 80), (0, 0)]
    notch = Point(40, 60)
    lvl1 = [Polygon(*p) for p in smooth_area(ring, [], 1)]
    lvl2 = [Polygon(*p) for p in smooth_area(ring, [], 2)]
    assert not any(p.contains(notch) for p in lvl1)
    assert any(p.contains(notch) for p in lvl2)
