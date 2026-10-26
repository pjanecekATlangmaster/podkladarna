"""Les a MTBO: plocha silnic z DTM jen u složitých křižovatek (``app.pipeline.junction_areas``)."""

import json

from shapely.geometry import Point

from app.pipeline import junction_areas as ja
from app.pipeline import osm_paths as op


def _write(work, roads_polys, lines, oneway=()):
    (work / "dtm").mkdir(exist_ok=True)
    (work / "osm_paths").mkdir(exist_ok=True)
    (work / "dtm" / "roads.json").write_text(json.dumps({"polygons": roads_polys}), encoding="utf-8")
    feats = [
        {"type": "Feature", "properties": {"source": "osm", "highway": "road_3"}, "geometry": {"type": "LineString", "coordinates": c}}
        for c in lines
    ]
    for name in ("paths_osm.geojson", "paths_mixed.geojson"):
        (work / "osm_paths" / name).write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    ones = [
        {
            "type": "Feature",
            "properties": {"source": "osm", "highway": "road_3", "name": "X", "roundabout": rb, "lanes": "2"},
            "geometry": {"type": "LineString", "coordinates": c},
        }
        for c, rb in oneway
    ]
    (work / "osm_paths" / op.ONEWAY_ROADS_NAME).write_text(json.dumps({"type": "FeatureCollection", "features": ones}), encoding="utf-8")
    ja._CACHE.clear()


def _rect(x0, y0, x1, y1, holes=()):
    return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]], *[[[a, b], [c, b], [c, d], [a, d], [a, b]] for a, b, c, d in holes]]


def test_simple_cross_is_not_an_area(tmp_path):
    # Křižovatka X bez ostrůvků a jednosměrek: žádná plocha, silnice zůstávají čarami.
    _write(tmp_path, [_rect(-200, -5, 200, 5), _rect(-5, -200, 5, 200)], [[[-200, 0], [0, 0], [200, 0]], [[0, -200], [0, 0], [0, 200]]])
    assert len(ja.junction_clusters(tmp_path)) == 1
    assert ja.complex_junction_hulls(tmp_path) == []
    assert ja.junction_area_geometry(tmp_path, "forest_10000", 10000) is None


def test_island_makes_junction_complex(tmp_path):
    # Tatáž křižovatka s ostrůvkem (díra v ploše silnic z DTM) → plocha z DTM v okolí shluku, daleká silnice čarou.
    _write(
        tmp_path,
        [_rect(-200, -15, 200, 15, holes=[(6, 3, 9, 9)]), _rect(-5, -200, 5, 200)],  # ostrůvek mimo příčnou silnici
        [[[-200, 0], [0, 0], [200, 0]], [[0, -200], [0, 0], [0, 200]]],
    )
    hulls = ja.complex_junction_hulls(tmp_path)
    assert len(hulls) == 1
    area = ja.junction_area_geometry(tmp_path, "forest_10000", 10000)
    assert area is not None and area.contains(Point(0, -2)) and not area.contains(Point(150, 0))


def test_roundabout_and_many_oneway_lanes_are_complex(tmp_path):
    lines = [[[-200, 0], [0, 0], [200, 0]], [[0, -200], [0, 0], [0, 200]]]
    _write(tmp_path, [_rect(-200, -5, 200, 5)], lines, oneway=[([[-20, 0], [20, 0]], True)])
    assert len(ja.complex_junction_hulls(tmp_path)) == 1
    _write(tmp_path, [_rect(-200, -5, 200, 5)], lines, oneway=[([[-20, 0], [20, 0]], False)] * 4)
    assert len(ja.complex_junction_hulls(tmp_path)) == 1
    _write(tmp_path, [_rect(-200, -5, 200, 5)], lines, oneway=[([[-20, 0], [20, 0]], False)] * 3)
    assert ja.complex_junction_hulls(tmp_path) == []
