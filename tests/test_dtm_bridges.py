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
        line("road_4", [-980, -1040], [-980, -960]),  # OSM silnice kolmo pod lávkou
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
    assert any("chodník 501.0" in n for n in names) and not any("silnice" in n for n in names)
    # chodník na úrovni 0 pod mostem se z plochy chodníků vystřihne
    side = di.sidewalk_geometry(work, "mixed", cut_decks=True)
    assert side.intersection(di.deck_footprint_geometry(work)).area < 1e-6
    paths = op.build_osm_path_parts(work, **kw)
    # lávka uvnitř mostu odpadla, cesta pod mostem je přerušená (2 kusy) + cesta mimo = 3 objekty
    assert paths and paths[0].count == 3
    # les a MTBO mosty z DTM nekreslí
    assert di.build_dtm_bridge_parts(work, **{**kw, "preset_id": "forest_10000"}) == []
    assert di.build_dtm_bridge_parts(work, **{**kw, "preset_id": "mtbo_10000"}) == []
