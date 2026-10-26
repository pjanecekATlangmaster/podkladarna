import json

from app.pipeline import dtm_bridges as db
from app.pipeline import dtm_import as di
from app.pipeline import dtm_sidewalks as ds


def _rect(x0, y0, x1, y1):
    return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]


def _deck(level, typ, rect):
    return {"l": level, "t": typ, "p": _rect(*rect)}


def test_level_decks_structure_takes_color_of_surface_and_upper_level_cuts_lower():
    decks = [
        _deck(1, "PlochaMostniKonstrukcePlocha", (0, 0, 20, 6)),
        _deck(1, "ChodnikPlocha", (0, 1, 20, 5)),  # povrch: chodník → konstrukce je chodníková
        _deck(1, "ProvozniPlochaPozemniKomunikacePlocha", (30, 0, 50, 8)),  # silniční most bez konstrukce
        _deck(2, "ChodnikPlocha", (5, 0, 10, 6)),  # druhá úroveň nad první
    ]
    levels = db.level_decks(decks)
    assert set(levels) == {1, 2}
    assert levels[1]["heavy"].area == 160.0 and levels[1]["light"].intersection(levels[1]["heavy"]).area < 1e-6
    # úroveň 2 vystřihne úroveň 1 pod sebou (5 × 6 = 30 m²)
    assert abs(levels[1]["light"].area - (120.0 - 30.0)) < 1e-6
    foot = db.footprint(levels)
    assert abs(foot.area - (120.0 + 160.0)) < 1e-6  # půdorys: nic se nezdvojuje


def test_entrance_edges_mark_sidewalks_passing_under_and_skip_landing_and_marked_roads():
    # most 40 × 10 m přes silnici (y 3…7) a dva chodníky podél ní (y 0…3 a 7…10), které se kříží svisle
    foot = ds.dtm_union([_rect(0, 0, 40, 10)])
    walk_a = ds.dtm_union([_rect(10, -20, 13, 30)])  # chodník ve směru y přes celý most
    heavy = ds.dtm_union([_rect(-20, 3, 60, 7)])  # silnice pod mostem
    edges = db.entrance_edges(foot, walk_a, heavy)
    # chodník prochází (2 hrany přes jeho šířku 3 m); silnice leží do 5 m od chodníku → neznačí se
    assert len(edges) == 2 and all(2.5 < abs(e[0][0] - e[-1][0]) + abs(e[0][1] - e[-1][1]) < 3.5 for e in edges)
    # bez chodníku se značí silnice (2 hrany přes její šířku 4 m)
    road_edges = db.entrance_edges(foot, None, heavy)
    assert len(road_edges) == 2
    # chodník, na který most jen přistává (zasahuje 1 m do mostu): neznačí se
    landing = ds.dtm_union([_rect(-10, 0, 1, 10)])
    assert db.entrance_edges(foot, landing, None) == []


def test_entrance_edges_are_oriented_with_deck_on_the_left():
    from shapely.geometry import Point

    foot = ds.dtm_union([_rect(0, 0, 40, 10)])
    walk = ds.dtm_union([_rect(10, -20, 13, 30)])
    edges = db.entrance_edges(foot, walk, None)
    assert len(edges) == 2
    for e in edges:
        (x0, y0), (x1, y1) = e[0], e[-1]
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        tx, ty = x1 - x0, y1 - y0
        n = (tx * tx + ty * ty) ** 0.5
        left = Point(mx - ty / n * 0.4, my + tx / n * 0.4)  # bod vlevo od směru
        assert foot.contains(left)


def test_lower_level_areas_cover_the_whole_bridge_that_crosses_paved_or_water():
    bridge_road = ds.dtm_union([_rect(0, 0, 40, 10)])  # most přes silnici
    bridge_water = ds.dtm_union([_rect(100, 0, 140, 10)])  # most přes vodu
    bridge_none = ds.dtm_union([_rect(200, 0, 240, 10)])  # most přes nic
    foot = ds.dtm_union([_rect(0, 0, 40, 10), _rect(100, 0, 140, 10), _rect(200, 0, 240, 10)])
    paved = ds.dtm_union([_rect(10, -5, 20, 15)])  # silnice pod prvním mostem (10 × 10)
    water = ds.dtm_union([_rect(110, -5, 118, 15)])  # řeka pod druhým mostem (8 × 10)
    out = db.lower_level_areas(foot, paved, water)
    # celý most (ne jen průnik se spodní plochou): 400 m² každý; třetí most bez spodního povrchu se nevrací
    assert abs(out["paved"].area - bridge_road.area) < 1e-6 and abs(out["water"].area - bridge_water.area) < 1e-6
    assert out["paved"].intersection(bridge_none).area < 1e-6 and out["water"].intersection(bridge_none).area < 1e-6
    assert db.lower_level_areas(foot, None, None) == {"paved": None, "water": None}
    tiny = ds.dtm_union([_rect(300, 0, 300.5, 0.5)])
    assert db.lower_level_areas(foot, tiny)["paved"] is None  # mimo most


def test_bridge_parts_draw_lower_paved_with_512_311_and_hide_lower_stairs(tmp_path):
    from app.pipeline.oom_symbol_map import symbol_index_for_code

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "decks.json").write_text(
        json.dumps({"decks": [{"l": 1, "t": "ChodnikPlocha", "p": [[list(p) for p in ring] for ring in _rect(-1000, -1004, -960, -996)]}]}),
        encoding="utf-8",
    )
    (work / "dtm" / "roads.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-990, -1020, -970, -980)]]}),  # silnice pod mostem
        encoding="utf-8",
    )
    (work / "dtm" / "stairs.json").write_text(
        json.dumps(
            {
                "polygons": [
                    [[list(p) for p in ring] for ring in _rect(-988, -1001, -984, -999)],  # schodiště pod mostem (úr. 0)
                    [[list(p) for p in ring] for ring in _rect(-1030, -1001, -1026, -999)],  # schodiště mimo most
                ]
            }
        ),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-980.0, ref_y=-1000.0, grivation_deg=0.0)
    parts = di.build_dtm_bridge_parts(work, **kw)
    lower = [p for p in parts if "512.311" in p.name]
    assert lower and lower[0].count == 1  # celý most (i s částí mimo silnici), ne jen průnik
    assert f'symbol="{symbol_index_for_code("sprint_4000", 4000, "512.311")}"' in lower[0].objects_xml
    stairs = di.build_dtm_stairs_parts(work, **kw)
    assert stairs and stairs[0].count == 1  # schodiště pod mostem se nekreslí, mimo most ano


def test_lower_area_outline_is_not_drawn_along_the_bridge_edge():
    import re

    deck = ds.dtm_union([_rect(15, -5, 25, 15)])
    lower = ds.dtm_union([_rect(0, 0, 15, 10), _rect(25, 0, 40, 10)])  # chodník vystřižený o most
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0, clip_bounds=None)
    parts = di._geometry_part(lower, name="Chodníky", code="501.0", edge_exclude=deck, **kw)
    assert [p.name for p in parts] == ["Chodníky", "Chodníky – obrys (501.100)"]  # polygony u mostu: obrys zvlášť
    pts = [(int(x), int(y)) for x, y in re.findall(r"(-?\d+) (-?\d+)[ ;]", parts[1].objects_xml)]
    # 1 m terénu = 250 jednotek; hrana mostu x = 15 m → 3750, 25 m → 6250; y mezi 1,6 a 8,4 m = −400 … −2100
    on_bridge_edge = [p for p in pts if (abs(p[0] - 3750) < 20 or abs(p[0] - 6250) < 20) and -2100 < p[1] < -400]
    assert not on_bridge_edge and pts  # podél hrany mostu žádná čára, vnější obrys je
    # bez půdorysu mostu zůstává plocha se symbolem s obrysem (jedna část)
    assert [p.name for p in di._geometry_part(lower, name="Chodníky", code="501.0", **kw)] == ["Chodníky"]


def test_bridge_over_nothing_keeps_surface_color_without_512_window(tmp_path):
    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "decks.json").write_text(
        json.dumps({"decks": [{"l": 1, "t": "ChodnikPlocha", "p": [[list(p) for p in ring] for ring in _rect(-1000, -1004, -960, -996)]}]}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-980.0, ref_y=-1000.0, grivation_deg=0.0)
    names = [p.name for p in di.build_dtm_bridge_parts(work, **kw)]
    assert names == ["Mosty (DTM) úr. +1 – chodník 501.0"]  # nic pod ním → žádné 512.3xx, mostovka barvou povrchu


def test_osm_footbridge_partly_inside_dtm_deck_draws_no_edges_and_nothing_on_dtm_road(tmp_path):
    import re

    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "decks.json").write_text(
        json.dumps({"decks": [{"l": 1, "t": "ChodnikPlocha", "p": [[list(p) for p in ring] for ring in _rect(-990, -1004, -970, -1000)]}]}),
        encoding="utf-8",
    )
    (work / "dtm" / "roads.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1040, -1020, -940, -980)]]}),  # silnice pod lávkou
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()

    def line(hw, a, b):
        return {
            "type": "Feature",
            "properties": {"highway": hw},
            "geometry": {"type": "LineString", "coordinates": [a, b]},
        }

    feats = [
        line("bridge:footway", [-1030, -1002], [-940, -1002]),  # OSM lávka: 20 m uvnitř DTM mostu, zbytek na DTM silnici
        line("road_4", [-980, -1015], [-980, -985]),  # OSM silnice kolmo pod lávkou (celá v ploše silnice z DTM)
    ]
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-980.0, ref_y=-1002.0, grivation_deg=0.0)
    paths = op.build_osm_path_parts(work, **kw)
    # lávka uvnitř mostu z DTM i na DTM silnici se nekreslí, OSM silnice je plochou z DTM, hrany 512.100 z OSM žádné
    assert paths == []
    assert re.search(r"symbol", "".join(p.objects_xml for p in paths)) is None


def test_bridge_parts_cut_lower_levels_and_osm_lines_under_deck(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "decks.json").write_text(
        json.dumps(
            {
                "decks": [
                    {"l": 1, "t": "PlochaMostniKonstrukcePlocha", "p": [[list(p) for p in ring] for ring in _rect(-1000, -1010, -960, -1000)]},
                    {"l": 1, "t": "ChodnikPlocha", "p": [[list(p) for p in ring] for ring in _rect(-1000, -1008, -960, -1002)]},
                ]
            }
        ),
        encoding="utf-8",
    )
    (work / "dtm" / "sidewalks.json").write_text(
        json.dumps(
            {
                "polygons": [
                    [[list(p) for p in ring] for ring in _rect(-990, -1040, -987, -970)],  # chodník pod mostem
                ]
            }
        ),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()

    def line(hw, a, b):
        return {
            "type": "Feature",
            "properties": {"highway": hw},
            "geometry": {"type": "LineString", "coordinates": [a, b]},
        }

    feats = [
        line("bridge:sidewalk", [-1000, -1005], [-960, -1005]),  # OSM lávka uvnitř DTM mostu → nekreslí se
        line("path", [-950, -1040], [-950, -970]),  # cesta mimo most zůstává
        line("path", [-980, -1040], [-980, -970]),  # cesta pod mostem → jen části mimo půdorys
    ]
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-980.0, ref_y=-1005.0, grivation_deg=0.0)
    assert di.dtm_owns_decks(work)
    parts = di.build_dtm_bridge_parts(work, **kw)
    names = [p.name for p in parts]
    # most nad spodním chodníkem: celý 512.311 + obrys 501.100, mostovka 501.0 se zvlášť nekreslí (jeden plošný symbol)
    assert any("512.311" in n for n in names) and any("obrys mostu" in n for n in names)
    assert not any("chodník 501.0" in n for n in names) and not any("silnice" in n for n in names)
    # chodník na úrovni 0 pod mostem se z plochy chodníků vystřihne
    side = di.sidewalk_geometry(work, "mixed", cut_decks=True)
    assert side.intersection(di.deck_footprint_geometry(work)).area < 1e-6
    paths = op.build_osm_path_parts(work, **kw)
    # lávka uvnitř mostu odpadla, cesta pod mostem je přerušená (2 kusy) + cesta mimo = 3 objekty
    assert paths and paths[0].count == 3
    # les a MTBO mosty z DTM nekreslí
    assert di.build_dtm_bridge_parts(work, **{**kw, "preset_id": "forest_10000"}) == []
    assert di.build_dtm_bridge_parts(work, **{**kw, "preset_id": "mtbo_10000"}) == []
