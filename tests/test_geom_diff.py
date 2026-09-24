"""Testy odečtu KP 401 minus masky."""

import pytest

from app.pipeline.geom_diff import (
    difference_polygon_wkb,
    rings_to_polygon_wkb,
    union_polygon_wkbs,
)


def _sq(x0, y0, size):
    return [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size)]


def _area(wkb: bytes) -> float:
    try:
        from osgeo import ogr

        return float(ogr.CreateGeometryFromWkb(wkb).GetArea())
    except ImportError:
        from shapely import from_wkb

        return float(from_wkb(wkb).area)


def test_difference_cuts_hole_out_of_subject():
    subject = rings_to_polygon_wkb([_sq(0, 0, 100)])
    mask_wkb = rings_to_polygon_wkb([_sq(20, 20, 30)])
    assert subject and mask_wkb
    mask = union_polygon_wkbs([mask_wkb])
    pieces = difference_polygon_wkb(subject, mask)
    assert len(pieces) == 1
    # 100×100 − 30×30 = 9100
    assert _area(pieces[0]) == pytest.approx(9100.0, abs=1.0)


def test_difference_without_mask_keeps_subject():
    subject = rings_to_polygon_wkb([_sq(0, 0, 50)])
    assert subject
    pieces = difference_polygon_wkb(subject, None)
    assert len(pieces) == 1
    assert _area(pieces[0]) == pytest.approx(2500.0)


def test_full_cover_mask_drops_subject():
    subject = rings_to_polygon_wkb([_sq(10, 10, 20)])
    mask = union_polygon_wkbs([rings_to_polygon_wkb([_sq(0, 0, 100)])])
    assert difference_polygon_wkb(subject, mask) == []


def test_tiny_sliver_after_difference_is_dropped():
    # Subject jen o něco větší než maska → zbytek pod 12 m².
    subject = rings_to_polygon_wkb([_sq(0, 0, 10.1)])
    mask = union_polygon_wkbs([rings_to_polygon_wkb([_sq(0, 0, 10)])])
    assert difference_polygon_wkb(subject, mask) == []


def test_union_survives_invalid_overlap():
    """Neplatný / konfliktní překryv nesmí shodit Union na None."""
    a = rings_to_polygon_wkb([_sq(0, 0, 50)])
    b = rings_to_polygon_wkb([_sq(25, 25, 50)])
    mask = union_polygon_wkbs([a, b])
    assert mask is not None and not mask.IsEmpty()
    subject = rings_to_polygon_wkb([_sq(0, 0, 100)])
    pieces = difference_polygon_wkb(subject, mask)
    assert pieces
    assert _area(pieces[0]) < 10000.0
