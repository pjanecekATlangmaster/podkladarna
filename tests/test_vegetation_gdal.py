"""Testy KP vegetace – dělení žluté mřížkou."""

from __future__ import annotations

import pytest

ogr = pytest.importorskip("osgeo.ogr")

from app.pipeline.vegetation_gdal import (  # noqa: E402
    _YELLOW_SPLIT_CELL_M,
    split_yellow_polygon,
)


def _square(x0: float, y0: float, size: float):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in (
        (x0, y0),
        (x0 + size, y0),
        (x0 + size, y0 + size),
        (x0, y0 + size),
        (x0, y0),
    ):
        ring.AddPoint(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    return poly


def test_small_yellow_is_not_split():
    geom = _square(0, 0, 20)
    pieces = split_yellow_polygon(geom, min_area_m2=2000)
    assert len(pieces) == 1
    assert pieces[0].GetArea() == pytest.approx(400.0)


def test_large_yellow_is_split_to_grid_cells():
    # 120×120 m → při 50 m mřížce 3×3 = až 9 buněk
    geom = _square(0, 0, 120)
    pieces = split_yellow_polygon(
        geom, cell_m=_YELLOW_SPLIT_CELL_M, min_area_m2=2000
    )
    assert len(pieces) >= 4
    assert sum(p.GetArea() for p in pieces) == pytest.approx(14400.0, abs=1.0)
    assert max(p.GetArea() for p in pieces) <= _YELLOW_SPLIT_CELL_M**2 + 1.0
