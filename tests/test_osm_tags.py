"""Tagy OSM u objektů .omap (volba osm_tags): párování podle tvaru nad hotovou mapou."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import OomObjectPart
from app.pipeline.osm_tags import attach_osm_tags, merge_tags

SCALE = 10000
REF = (1000.0, 2000.0)


def _xy(x, y):
    return projected_to_map_coord(x, y, ref_x=REF[0], ref_y=REF[1], scale=SCALE, grivation_deg=0.0)


def _line(pts, symbol=3):
    body = ";".join("%d %d" % _xy(x, y) for x, y in pts) + ";"
    return f'<object type="1" symbol="{symbol}"><coords count="{len(pts)}">{body}</coords></object>'


def _point(x, y):
    return '<object type="0" symbol="5"><coords count="1">%d %d;</coords></object>' % _xy(x, y)


def _tags(obj_xml: str) -> dict:
    el = ET.fromstring(obj_xml)
    return {t.get("k"): t.text for t in el.iter("t")}


@pytest.fixture
def work(tmp_path):
    recs = [
        # Dvě OSM cesty spojené v mapě do jedné čáry: shodný surface zůstane, jiné jméno ne.
        {"id": "way/1", "tags": {"highway": "track", "surface": "gravel", "name": "A"}, "lines": [[(1000, 2000), (1100, 2000)]]},
        {"id": "way/2", "tags": {"highway": "track", "surface": "gravel", "name": "B & <C>"}, "lines": [[(1100, 2000), (1200, 2001)]]},
        {"id": "node/3", "tags": {"amenity": "bench"}, "point": (1050.0, 2030.0)},
        # Přechod vedle lavičky: lavička si vezme nejbližší bod, přechod zůstane nespárovaný.
        {"id": "node/5", "tags": {"highway": "crossing"}, "point": (1055.0, 2030.0)},
        # Souběžný chodník mimo kresbu u spojené silnice: jiná třída, tagy silnice nezkazí.
        {"id": "way/6", "tags": {"highway": "footway"}, "lines": [[(1000, 2001), (1100, 2001)]]},
        # Nenakreslená cesta daleko od objektů.
        {"id": "way/4", "tags": {"highway": "path"}, "lines": [[(1000, 2300), (1100, 2300)]]},
    ]
    (tmp_path / "osm_paths").mkdir()
    (tmp_path / "osm_paths" / "osm_tagged.json").write_text(json.dumps(recs), encoding="utf-8")
    return tmp_path


def test_tags_paired_by_shape_and_reported(work):
    road = _line([(1000, 2000), (1100, 2000), (1200, 2001)])
    bench = _point(1051, 2030.5)  # odsunutý bod
    zab = _line([(1000, 2000), (1200, 2000)])
    parts = [
        OomObjectPart("OSM cesty", road, 1),
        OomObjectPart("OSM lavička", bench, 1),
        OomObjectPart("ZABAGED – Ulice", zab, 1),
    ]
    st: dict = {}
    out = attach_osm_tags(parts, work, scale=SCALE, ref_x=REF[0], ref_y=REF[1], grivation_deg=0.0, stats=st)
    assert _tags(out[0].objects_xml) == {"osm_id": "way/1;way/2", "highway": "track", "surface": "gravel"}
    assert _tags(out[1].objects_xml) == {"osm_id": "node/3", "amenity": "bench"}
    assert out[2] is parts[2]  # jiné zdroje beze změny
    assert st["unpaired"] == {"highway=path": 1, "highway=crossing": 1, "highway=footway": 1}
    assert st["dropped_keys"] == {"name": 1}
    assert (work / "osm_tagged_report.json").is_file()


def test_missing_prepared_elements_leave_parts(tmp_path):
    parts = [OomObjectPart("OSM cesty", _line([(1000, 2000), (1100, 2000)]), 1)]
    st: dict = {}
    assert attach_osm_tags(parts, tmp_path, scale=SCALE, ref_x=0, ref_y=0, grivation_deg=0.0, stats=st) == parts
    assert st["missing"]


def test_merge_tags_escaping_and_conflicts():
    kept, dropped = merge_tags([{"a": "1", "b": "x"}, {"a": "1"}])
    assert kept == {"a": "1"} and dropped == {"b"}
