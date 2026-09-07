"""Díry v plochách: ZABAGED vyřezává les z louky, OOM to musí zachovat."""

import struct

from app.pipeline.oom_import import (
    _area_object_with_holes,
    _geom_parts_to_objects,
    _wkb_parts,
)

MAP_KW = dict(ref_x=0.0, ref_y=0.0, scale=10000, grivation_deg=0.0)


def _wkb_polygon(rings: list[list[tuple[float, float]]]) -> bytes:
    buf = struct.pack("<BI", 1, 3) + struct.pack("<I", len(rings))
    for ring in rings:
        closed = ring + [ring[0]]
        buf += struct.pack("<I", len(closed))
        for x, y in closed:
            buf += struct.pack("<dd", x, y)
    return buf


def _square(x0, y0, size):
    return [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size)]


def _coord_count(xml: str) -> int:
    start = xml.index('count="') + len('count="')
    return int(xml[start : xml.index('"', start)])


def test_wkb_keeps_interior_rings_as_holes():
    parts, _ = _wkb_parts(_wkb_polygon([_square(0, 0, 100), _square(20, 20, 30)]))
    assert [p[0] for p in parts] == ["line", "hole"]
    assert parts[0][2] is True


def test_multipolygon_keeps_holes():
    poly_a = _wkb_polygon([_square(0, 0, 100), _square(20, 20, 30)])
    poly_b = _wkb_polygon([_square(500, 500, 50)])
    multi = struct.pack("<BI", 1, 6) + struct.pack("<I", 2) + poly_a + poly_b
    parts, _ = _wkb_parts(multi)
    assert [p[0] for p in parts] == ["line", "hole", "line"]


def test_area_with_hole_emits_two_closed_parts():
    outer = [(0, 0), (100, 0), (100, 100), (0, 100)]
    hole = [(20, 20), (50, 20), (50, 50), (20, 50)]
    xml = _area_object_with_holes(7, [outer, hole])
    # Každý prstenec je ukončený opakovaným prvním bodem s flagem 18.
    assert xml.count(" 18;") == 2
    assert _coord_count(xml) == 10


def test_area_without_holes_matches_single_ring():
    outer = [(0, 0), (100, 0), (100, 100), (0, 100)]
    xml = _area_object_with_holes(7, [outer])
    assert xml.count(" 18;") == 1
    assert _coord_count(xml) == 5


def test_degenerate_hole_is_dropped_but_area_survives():
    outer = [(0, 0), (100, 0), (100, 100), (0, 100)]
    xml = _area_object_with_holes(7, [outer, [(5, 5), (6, 6)]])
    assert xml.count(" 18;") == 1


def test_degenerate_outer_ring_drops_whole_object():
    assert _area_object_with_holes(7, [[(0, 0), (1, 1)], [(2, 2), (3, 3), (4, 4)]]) == ""


def test_polygon_with_hole_survives_import():
    parts, _ = _wkb_parts(_wkb_polygon([_square(0, 0, 100), _square(20, 20, 30)]))
    objects = _geom_parts_to_objects(parts, 7, **MAP_KW)
    assert len(objects) == 1
    assert objects[0].count(" 18;") == 2


def test_hole_outside_clip_bounds_is_dropped():
    # Díra leží mimo výřez, obrys jen částečně – zůstane plná plocha.
    rings = [_square(0, 0, 100), _square(200, 200, 30)]
    parts, _ = _wkb_parts(_wkb_polygon(rings))
    objects = _geom_parts_to_objects(
        parts, 7, **MAP_KW, clip_bounds=(-10.0, -10.0, 150.0, 150.0)
    )
    assert len(objects) == 1
    assert objects[0].count(" 18;") == 1


def test_hole_inside_clip_bounds_is_kept():
    parts, _ = _wkb_parts(_wkb_polygon([_square(0, 0, 100), _square(20, 20, 30)]))
    objects = _geom_parts_to_objects(
        parts, 7, **MAP_KW, clip_bounds=(-10.0, -10.0, 150.0, 150.0)
    )
    assert objects[0].count(" 18;") == 2


def test_orphan_hole_without_outline_is_ignored():
    assert _geom_parts_to_objects([("hole", _square(0, 0, 10))], 7, **MAP_KW) == []


def test_open_line_is_untouched_by_hole_logic():
    parts = [("line", [(0.0, 0.0), (10.0, 0.0)], False)]
    objects = _geom_parts_to_objects(parts, 7, **MAP_KW)
    assert len(objects) == 1
    assert " 18;" not in objects[0]
