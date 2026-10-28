"""Les a MTBO: plocha silnic z DTM jen u složitých křižovatek (``app.pipeline.junction_areas``)."""

import json

from shapely.geometry import Point

from app.pipeline import junction_areas as ja
from app.pipeline import osm_paths as op


def test_small_pieces_of_junction_area_are_dropped():
    # Odřezky plochy silnic u hranice obalu (pod 300 m²) nejsou křižovatka; bez velkého kusu nic.
    from shapely.geometry import MultiPolygon, box

    big, small = box(0, 0, 50, 50), box(100, 0, 109, 9)
    out = ja.without_small_pieces(MultiPolygon([big, small]))
    assert out.area == big.area and out.contains(Point(25, 25)) and not out.intersects(small)
    assert ja.without_small_pieces(small) is None and ja.without_small_pieces(None) is None


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
        [_rect(-200, -15, 200, 15, holes=[(6, 3, 11, 10)]), _rect(-5, -200, 5, 200)],  # ostrůvek 35 m² mimo příčnou silnici
        [[[-200, 0], [0, 0], [200, 0]], [[0, -200], [0, 0], [0, 200]]],
    )
    hulls = ja.complex_junction_hulls(tmp_path)
    assert len(hulls) == 1
    area = ja.junction_area_geometry(tmp_path, "forest_10000", 10000)
    assert area is not None and area.contains(Point(0, -2)) and not area.contains(Point(150, 0))


def test_roundabout_and_many_oneway_lanes_are_complex(tmp_path, monkeypatch):
    lines = [[[-200, 0], [0, 0], [200, 0]], [[0, -200], [0, 0], [0, 200]]]
    _write(tmp_path, [_rect(-200, -5, 200, 5)], lines, oneway=[([[-20, 0], [20, 0]], True)])
    # kruhový objezd kreslí čáry (Petr 2026-10-28), plochu jen s ``ROUNDABOUT_AREAS``
    assert ja.complex_junction_hulls(tmp_path) == []
    monkeypatch.setattr(ja, "ROUNDABOUT_AREAS", True)
    assert len(ja.complex_junction_hulls(tmp_path)) == 1
    _write(tmp_path, [_rect(-200, -5, 200, 5)], lines, oneway=[([[-20, 0], [20, 0]], False)] * 4)
    assert len(ja.complex_junction_hulls(tmp_path)) == 1
    _write(tmp_path, [_rect(-200, -5, 200, 5)], lines, oneway=[([[-20, 0], [20, 0]], False)] * 3)
    assert ja.complex_junction_hulls(tmp_path) == []


def test_funnel_mouth_narrows_area_to_line_fill():
    # Plocha křižovatky 40 m široká, silnice (ústí ± 4 m) vstupuje zleva: v ústí má plocha šířku ústí, trychtýř 45°
    # (5 m dál ± 9 m), 10 m dál je plná.
    from shapely.geometry import LineString, box

    area = box(0, -20, 60, 20)
    line = LineString([(-50, 0), (30, 0)])
    out = ja.funnel_mouths(area, [(line, 4.0)], 10.0)
    assert out.contains(Point(0.5, 3)) and not out.contains(Point(0.5, 10))
    assert out.contains(Point(5, 8)) and not out.contains(Point(5, 10))
    assert out.contains(Point(11, 19)) and out.contains(Point(40, 19))
    # bez čáry beze změny
    assert ja.funnel_mouths(area, [], 10.0).equals(area)


def test_funnel_mouth_ends_area_square_to_line():
    # Šikmý konec plochy z DTM (Retest 5): plocha končí kolmo na čáru v místě vstupu, nic z ní nepřesahuje za konec
    # kresby silnice; ústí jiné čáry (její pás) se neořízne.
    from shapely.geometry import LineString, Polygon

    area = Polygon([(0, -20), (60, -20), (60, 20), (-8, 20)])  # levý okraj šikmo, osa protne v x = -4
    line = LineString([(-50, 0), (30, 0)])
    out = ja.funnel_mouths(area, [(line, 4.0)], 10.0)
    assert not out.contains(Point(-6, 15)) and not out.contains(Point(-5, 2))
    assert out.contains(Point(-3.5, 2)) and out.contains(Point(20, 15))
    # na druhé straně šikmý konec nedosahoval ke konci kresby (x = -4): plocha se k němu doplní
    assert out.contains(Point(-3.5, -3))
    # druhá čára vstupuje shora vedle ústí: její pás zůstane
    other = LineString([(-6, 40), (-6, -10)])
    out2 = ja.funnel_mouths(area, [(line, 4.0), (other, 3.0)], 10.0)
    assert out2.contains(Point(-6, 15))
