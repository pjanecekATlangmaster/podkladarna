"""Les a MTBO: plocha silnic z DTM jen u složitých křižovatek (``app.pipeline.junction_areas``)."""

import json

import pytest
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
    out = ja.shape_mouths(area, ja.mouth_entries(area, [(line, 4.0)]), 10.0)
    assert out.contains(Point(0.5, 3)) and not out.contains(Point(0.5, 10))
    assert out.contains(Point(5, 8)) and not out.contains(Point(5, 10))
    assert out.contains(Point(11, 19)) and out.contains(Point(40, 19))
    # bez čáry beze změny
    assert ja.shape_mouths(area, ja.mouth_entries(area, []), 10.0).equals(area)


def test_funnel_mouth_ends_area_square_to_line():
    # Šikmý konec plochy z DTM (Retest 5): plocha končí kolmo na čáru v místě vstupu, nic z ní nepřesahuje za konec
    # kresby silnice; ústí jiné čáry (její pás) se neořízne.
    from shapely.geometry import LineString, Polygon

    area = Polygon([(0, -20), (60, -20), (60, 20), (-8, 20)])  # levý okraj šikmo, osa protne v x = -4
    line = LineString([(-50, 0), (30, 0)])
    out = ja.shape_mouths(area, ja.mouth_entries(area, [(line, 4.0)]), 10.0)
    assert not out.contains(Point(-6, 15)) and not out.contains(Point(-5, 2))
    assert out.contains(Point(-3.5, 2)) and out.contains(Point(20, 15))
    # na druhé straně šikmý konec nedosahoval ke konci kresby (x = -4): plocha se k němu doplní
    assert out.contains(Point(-3.5, -3))
    # druhá čára vstupuje shora vedle ústí: její pás zůstane
    other = LineString([(-6, 40), (-6, -10)])
    out2 = ja.shape_mouths(area, ja.mouth_entries(area, [(line, 4.0), (other, 3.0)]), 10.0)
    assert out2.contains(Point(-6, 15))


def test_restore_mouths_after_generalization():
    # Plocha po generalizaci sítě (Petr 2026-10-29, K Barrandovu): roh ústí zaoblený a výběžek za ústím; obnova vrátí
    # kolmý konec plochy na vstupu silnice (okraj navazuje na okrajovou linku), plocha křižovatky vedle ústí zůstane.
    from shapely.geometry import LineString, box

    line = LineString([(-50, 0), (30, 0)])
    entries = ja.mouth_entries(box(0, -20, 60, 20), [(line, 4.0)])
    generalized = box(0, -20, 60, 20).buffer(2.5)  # zaoblené rohy, přesah za ústí
    out = ja.restore_mouths(generalized, entries, 10000)
    assert out.contains(Point(0.2, 3.8)) and out.contains(Point(0.2, -3.8))  # roh ústí ostrý
    assert not out.contains(Point(-1.0, 0)) and not out.contains(Point(-1.0, 10))  # nic za ústím
    assert out.contains(Point(-1.0, -18))  # plocha za ústím daleko do strany (u jiného ústí) zůstane
    assert not out.contains(Point(3, 9))  # trychtýř 45°
    # vstup daleko od plochy ji nezmění
    far = ja.mouth_entries(box(200, -20, 260, 20), [(LineString([(150, 0), (230, 0)]), 4.0)])
    assert ja.restore_mouths(generalized, far, 10000).equals(generalized)


def test_square_mouth_oblique_entry_fills_wedge_and_cuts_behind():
    # Parkoviště se šikmou levou hranou (sklon 45°), silnice (ústí ± 4 m) vstupuje zleva po ose y = 0 v x = 0: pás kresby
    # se doplní dovnitř (bílý klín vedle kolmého konce silnice pryč) a plocha za vstupem v pásu zmizí (okraj nekřižoval
    # okrajovou linku). Vedle pásu je hrana dál šikmá, bez trychtýře.
    from shapely.geometry import LineString, Polygon

    area = Polygon([(0, 0), (-20, 20), (60, 20), (60, -20), (20, -20)])  # x = -y nad osou, x = y pod osou
    line = LineString([(-50, 0), (30, 0)])
    out = ja.square_mouths(area, ja.road_mouth_entries(area, [(line, 4.0)]), 10.0)
    assert out.contains(Point(1, -3)) and out.contains(Point(1, 3))  # pás kresby vyplněný hned za vstupem
    assert not out.contains(Point(-3, 3.5)) and not out.contains(Point(-10, 3.5))  # za vstupem v pásu nic
    assert out.contains(Point(-10, 12)) and out.contains(Point(8, 6))  # šikmá hrana vedle pásu i trychtýř beze změny
    assert ja.square_mouths(area, [], 10.0) is area


def test_road_mouth_entries_skip_grazing_and_corner_clips():
    from shapely.geometry import LineString, box

    area = box(0, 0, 40, 40)
    # tečný vstup přes horní hranu (úhel k hraně 2°) není ústí; zůstane jen kolmý výstup pravou hranou
    graze = LineString([(-50, 42), (90, 38)])
    assert [e[0][0] for e in ja.road_mouth_entries(area, [(graze, 4.0)])] == pytest.approx([40.0])
    # kolmý vstup je ústí
    assert len(ja.road_mouth_entries(area, [(LineString([(-50, 20), (30, 20)]), 4.0)])) == 1
    # silnice jen škrtne roh (výstup kolmo přes pravou hranu 1 m pod rohem): pás ústí by vyčníval, plocha se nemění
    entries = ja.mouth_entries(area, [(graze, 2.6)])
    assert ja.square_mouths(area, entries, 10.0).equals(area)


def test_entering_lines_take_stroke_symbol_and_network_applies_strokes(tmp_path, monkeypatch):
    # Půlka ústí podle symbolu po pravidle tahu (``stroke_code``), ne podle tagu; síť zpevněných ploch pravidlo tahu
    # na čáry silnic uplatní (oponentura 2026-10-31, mezera 7).
    from app.pipeline.oom_symbol_map import symbol_line_width_mm

    feat = {"type": "Feature", "properties": {"source": "osm", "highway": "road_2"}, "geometry": {"type": "LineString", "coordinates": [[0, 0], [50, 0]]}}
    (tmp_path / "osm_paths").mkdir()
    by_tag = ja._entering_lines(tmp_path, "forest_10000", 10000, features=[feat])
    by_stroke = ja._entering_lines(tmp_path, "forest_10000", 10000, features=[{**feat, "properties": {**feat["properties"], op.STROKE_CODE_PROP: "502"}}])
    assert by_stroke[0][1] > by_tag[0][1]
    assert by_stroke[0][1] == pytest.approx(op.mm_to_ground_m(symbol_line_width_mm("forest_10000", 10000, "502"), 10000) / 2.0 - op.paved_edge_width_m("forest_10000", 10000) / 2.0, abs=0.05)
    # prodloužené konce
    ext = ja._entering_lines(tmp_path, "forest_10000", 10000, features=[feat], extend_m=5.0)
    assert ext[0][0].length == pytest.approx(60.0)
    calls = []
    monkeypatch.setattr(op, "_apply_road_strokes", lambda feats, *a, **k: calls.append(len(feats)))
    _write(tmp_path, [], [[[0, 0], [50, 0]]])
    (tmp_path / "osm_paths" / "features.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
    op._PAVED_NETWORK_CACHE.clear()
    op.paved_network_geometry(tmp_path, "forest_10000", 10000)
    assert calls == [1]
