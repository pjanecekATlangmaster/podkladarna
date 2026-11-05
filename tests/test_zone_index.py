"""Výřez velké zóny v okolí čáry (``zone_index``)."""

import pytest

shapely = pytest.importorskip("shapely")

from shapely.geometry import LineString, Point

from app.pipeline import zone_index


@pytest.mark.parametrize("geos_fails", [False, True])
def test_local_zone_same_as_whole_zone(monkeypatch, geos_fails):
    # „Jehla“ ve validní zóně shodí GEOS clip_by_rect (LinearRing 3 points, les 1:10 000 Barrandov 2026-11-05):
    # výřez se pak udělá průnikem, výsledek ořezu je stejný.
    if geos_fails:

        def boom(*a, **k):
            raise shapely.errors.GEOSException("IllegalArgumentException: Invalid number of points in LinearRing found 3")

        monkeypatch.setattr(shapely, "clip_by_rect", boom)
    zone_index.clear()
    zone = Point(0, 0).buffer(1000.0, quad_segs=6000)  # > MIN_COORDS vrcholů
    line = LineString([(900.0, -50.0), (1100.0, 50.0)])
    local = zone_index.local_zone(zone, line, 1.0)
    assert line.difference(local).length == pytest.approx(line.difference(zone).length, abs=1e-6)
    zone_index.clear()
