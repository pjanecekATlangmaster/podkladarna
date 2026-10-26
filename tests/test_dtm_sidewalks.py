import json

import pytest

from app.pipeline import dtm_import as di
from app.pipeline import dtm_sidewalks as ds


@pytest.fixture(autouse=True)
def _mtbo_dtm_on(monkeypatch):
    """MTBO má DTM zapnuté (``MTBO_USE_DTM``); fixtura ho drží zapnuté nezávisle na výchozí hodnotě."""
    monkeypatch.setattr(di, "MTBO_USE_DTM", True)


def test_mtbo_without_dtm_flag_draws_nothing_from_dtm(tmp_path, monkeypatch):
    monkeypatch.setattr(di, "MTBO_USE_DTM", False)
    work = tmp_path
    (work / "dtm").mkdir()
    ring = [[-1000, -980], [-950, -980], [-950, -940], [-1000, -940], [-1000, -980]]
    (work / "dtm" / "fences.json").write_text(json.dumps({"fences": [{"code": "518", "coords": ring}]}), encoding="utf-8")
    for name in ("sidewalks.json", "roads.json", "parking.json", "stairs.json"):
        (work / "dtm" / name).write_text(json.dumps({"polygons": [[[list(p) for p in _rect(0, 0, 100, 10)[0]]]]}), encoding="utf-8")
    kw = dict(preset_id="mtbo_10000", scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    assert di.mtbo_dtm_off("mtbo_10000") and not di.mtbo_dtm_off("forest_10000")
    assert di.mtbo_corridor_geometry(work, 10000) is None and di.mtbo_sidewalk_geometry(work, 10000) is None
    for fn in (
        di.build_dtm_fence_parts,
        di.build_dtm_sidewalk_parts,
        di.build_dtm_parking_parts,
        di.build_dtm_stairs_parts,
        di.build_mtbo_corridor_parts,
    ):
        assert fn(work, **kw) == []


def _rect(x0, y0, x1, y1):
    return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]


def test_unify_drops_osm_inside_dtm_and_keeps_remainder():
    dtm = [_rect(0, 0, 30, 2)]
    inside = [(2.0, 1.0), (28.0, 1.0)]  # celé uvnitř DTM chodníku → duplicitní
    extra = [(30.0, 1.0), (30.0, 21.0)]  # navazuje na konec DTM, DTM ho nezná
    geom = ds.unify_sidewalks(dtm, [inside, extra])
    assert geom.geom_type == "Polygon"  # zbytek se napojil na DTM
    assert geom.bounds[3] > 20.0  # zbytek z OSM je v ploše
    assert geom.area < 30 * 2 + 20 * 1.4 + 5


def test_unify_without_osm_remainder_is_dtm_only():
    geom = ds.unify_sidewalks([_rect(0, 0, 30, 2)], [[(2.0, 1.0), (28.0, 1.0)]])
    assert abs(geom.area - 60.0) < 1.0


def test_unify_filters_short_remainders_and_small_holes():
    ring = _rect(0, 0, 20, 20)
    ring.append([(5, 5), (5.5, 5), (5.5, 5.5), (5, 5.5), (5, 5)])  # díra 0,25 m²
    geom = ds.unify_sidewalks([ring], [[(20.0, 10.0), (21.5, 10.0)]])  # zbytek 1,5 m → zahodit
    assert not list(geom.interiors)
    assert geom.bounds[2] < 20.5


def test_unify_osm_only_when_no_dtm_polygon():
    geom = ds.unify_sidewalks([], [[(0.0, 0.0), (20.0, 0.0)]])
    assert abs(geom.area - 20 * ds.OSM_WIDTH_M) < 3.0


def test_unify_drops_parallel_osm_line_beside_dtm_but_keeps_extension():
    dtm = [_rect(0, 0, 40, 2)]
    parallel = [(2.0, 3.5), (38.0, 3.5)]  # posunutý OSM chodník 1,5 m od plochy
    away = [(40.0, 1.0), (40.0, 31.0)]  # pokračování kolmo pryč
    geom = ds.unify_sidewalks(dtm, [parallel])
    assert abs(geom.area - 80.0) < 1.0  # souběžná čára se nekreslí
    geom = ds.unify_sidewalks(dtm, [parallel, away])
    assert geom.bounds[3] > 30.0 and geom.area < 80 + 30 * 1.4 + 5


def test_clip_outside_cuts_footway_inside_dtm():
    cover = ds.dtm_union([_rect(10, -2, 20, 2)]).buffer(ds.OSM_COVER_M)
    pieces = ds.clip_outside([(0.0, 0.0), (40.0, 0.0)], cover)
    assert len(pieces) == 2
    assert max(x for x, _y in pieces[0]) < 10 and min(x for x, _y in pieces[1]) > 20


def test_osm_paved_area_overlapping_dtm_sidewalk_is_merged_not_drawn_twice(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "sidewalks.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1000, -960, -990)]]}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()

    def area(kind, x0, y0, x1, y1):
        ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
        return {
            "type": "Feature",
            "properties": {"source": "osm", "kind": kind, "oom_code": "501"},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }

    feats = [area("pedestrian_area", -980, -995, -950, -970), area("parking", -900, -900, -880, -880)]
    (work / "osm_paths" / "features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-985.0, ref_y=-999.0, grivation_deg=0.0)
    names = [p.name for p in op.build_osm_feature_parts(work, **kw)]
    assert "OSM pěší zóny (501 zpevněná)" not in names  # překrývá chodník → sloučeno s ním
    assert "OSM parkoviště (501 zpevněná)" in names  # samostatná plocha zůstává
    parts = di.build_dtm_sidewalk_parts(work, **kw)
    assert parts[0].count == 1  # jedna sloučená plocha


def test_road_parts_replace_osm_road_lines_and_keep_widths(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "roads.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1004, -940, -996)]]}),
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
        line("road_3", [-1000, -1000], [-940, -1000]),  # uvnitř DTM plochy → duplicitní
        line("road_3", [-940, -1000], [-900, -1000]),  # pokračování za plochou → doplní se
        line("bridge:road_3", [-960, -1010], [-960, -990]),  # most zůstává čárou
        line("path", [-1000, -980], [-940, -980]),
    ]
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-960.0, ref_y=-1000.0, grivation_deg=0.0)
    parts = di.build_dtm_road_parts(work, **kw)
    assert [p.name for p in parts] == ["Silnice (DTM + OSM)"] and parts[0].count == 1  # jedna sloučená plocha
    assert di.is_surface_road("road_2") and not di.is_surface_road("bridge:road_2")
    paths = op.build_osm_path_parts(work, **kw)
    assert paths and paths[0].count == 4  # most + pěšina + 2 hrany lávky 512.100; silnice nahradila plocha
    assert di.build_dtm_road_parts(work, **{**kw, "preset_id": "forest_10000"}) == []


def test_sidewalk_line_crossing_road_is_not_drawn_over_the_road(tmp_path, monkeypatch):
    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "roads.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1004, -940, -996)]]}),
        encoding="utf-8",
    )
    (work / "dtm" / "sidewalks.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1010, -990, -1005)]]}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()
    crossing = {
        "type": "Feature",
        "properties": {"highway": "sidewalk"},
        "geometry": {"type": "LineString", "coordinates": [[-960, -1030], [-960, -970]]},  # přes silnici
    }
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [crossing]}), encoding="utf-8"
    )
    seen = {}
    real = ds.unify_sidewalks

    def spy(polys, lines, **kw):
        seen["lines"] = lines
        return real(polys, lines, **kw)

    monkeypatch.setattr(ds, "unify_sidewalks", spy)
    di.build_dtm_sidewalk_parts(
        work, preset_id="sprint_4000", scale=4000, ref_x=-960.0, ref_y=-1000.0, grivation_deg=0.0
    )
    assert len(seen["lines"]) == 2  # před a za silnicí; úsek v silnici (-1004…-996) odpadl
    assert all(not (-1004 < y < -996) for ln in seen["lines"] for _x, y in ln)


def test_separate_from_roads_cuts_overlap_and_closes_narrow_gap():
    roads = ds.dtm_union([_rect(0, 0, 40, 8)])
    overlap = ds.dtm_union([_rect(0, 7, 40, 12)])  # zasahuje 1 m do silnice
    cut = ds.separate_from_roads(overlap, roads)
    assert cut.intersection(roads).area < 1e-6  # překryv odebrán
    assert abs(cut.bounds[1] - 8.0) < 1e-6  # chodník začíná přesně na hraně silnice
    gap = ds.dtm_union([_rect(0, 8.3, 40, 12)])  # škvíra 0,3 m
    closed = ds.separate_from_roads(gap, roads)
    assert abs(closed.bounds[1] - 8.0) < 0.05  # škvíra zacelená
    far = ds.dtm_union([_rect(0, 10, 40, 12)])  # škvíra 2 m (pás trávy) zůstává
    assert abs(ds.separate_from_roads(far, roads).bounds[1] - 10.0) < 1e-6


def test_merge_sidewalk_with_road_unions_near_keeps_far_and_interface():
    roads = ds.dtm_union([_rect(0, 0, 100, 8)])
    # přilehlý pás + pokračování daleko od silnice (y 20–22)
    sidewalks = ds.dtm_union([_rect(0, 8.2, 100, 11), _rect(0, 20, 40, 22)])
    merged, interface, rest = ds.merge_sidewalk_with_road(roads, sidewalks)
    assert merged is not None and interface is not None and rest is not None
    assert merged.area > roads.area  # chodník přibyl do plochy
    assert abs(rest.bounds[1] - 20.0) < 0.1  # daleký kus zůstal
    assert interface.length > 50  # společná hrana podél silnice
    # bez přilehlého chodníku se nesloučí
    far_only = ds.dtm_union([_rect(0, 20, 40, 22)])
    assert ds.merge_sidewalk_with_road(roads, far_only) == (None, None, far_only)


def test_sprint_merges_adjacent_sidewalk_into_501_1_with_interface(tmp_path):
    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "roads.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1004, -900, -996)]]}),
        encoding="utf-8",
    )
    (work / "dtm" / "sidewalks.json").write_text(
        json.dumps(
            {
                "polygons": [
                    # podél silnice (y -995…-992) + kus mimo (y -940…-938)
                    [[list(p) for p in ring] for ring in _rect(-1000, -995, -900, -992)],
                    [[list(p) for p in ring] for ring in _rect(-1000, -940, -960, -938)],
                ]
            }
        ),
        encoding="utf-8",
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-950.0, ref_y=-1000.0, grivation_deg=0.0)
    roads = di.build_dtm_road_parts(work, **kw)
    names = [p.name for p in roads]
    assert "Silnice s chodníkem (DTM + OSM, 501.1)" in names
    assert "Rozhraní chodník×silnice (501.100)" in names
    assert "Silnice (DTM + OSM)" not in names  # ne oddělená 501.20
    side = di.build_dtm_sidewalk_parts(work, **kw)
    assert side and "mimo silnici" in side[0].name
    assert side[0].count >= 1  # zůstal jen vzdálený kus


def test_dtm_parking_part_and_osm_parking_dedup(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "parking.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1000, -960, -980)]]}),
        encoding="utf-8",
    )
    (work / "dtm" / "roads.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -990, -960, -980)]]}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()

    def area(kind, x0, y0, x1, y1):
        ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
        return {
            "type": "Feature",
            "properties": {"source": "osm", "kind": kind, "oom_code": "501"},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }

    feats = [area("parking", -995, -998, -965, -982), area("parking", -900, -900, -880, -880)]
    (work / "osm_paths" / "features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-980.0, ref_y=-990.0, grivation_deg=0.0)
    parts = di.build_dtm_parking_parts(work, **kw)
    assert [p.name for p in parts] == ["DTM – parkoviště (501.0)"] and parts[0].count == 1
    geom = di.parking_geometry(work, di.road_geometry(work), None)
    assert geom.intersection(di.road_geometry(work)).area < 1e-6  # parkoviště po ořezu o silnici
    names = [p.name for p in op.build_osm_feature_parts(work, **kw)]
    assert names.count("OSM parkoviště (501 zpevněná)") == 1  # OSM parkoviště nad DTM zahozeno, vzdálené zůstává
    forest = {**kw, "preset_id": "forest_10000", "scale": 10000}
    assert [p.name for p in di.build_dtm_parking_parts(work, **forest)] == [
        "DTM – velká parkoviště",
        "DTM – velká parkoviště – obrys (501.2)",
    ]
    di.FOREST_DTM_AREAS = False
    try:
        assert di.build_dtm_parking_parts(work, **forest) == []  # les bez ploch z DTM: parkoviště jen z OSM
    finally:
        di.FOREST_DTM_AREAS = True


def test_geometry_part_without_fill_only_variant_reports_no_unmapped_code():
    """Kód bez výplňové varianty (MTBO parkoviště apod.) se nesmí hlásit jako chybějící symbol (kód '')."""
    from shapely.geometry import Point

    from app.pipeline import oom_symbol_map as sm

    sm.drain_unmapped_codes()
    kw = dict(preset_id="mtbo_10000", scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    di._geometry_part(Point(0, 0).buffer(5.0), name="Parkoviště", code="501.1", clip_bounds=None, **kw)
    assert all(code for _set, code in sm.drain_unmapped_codes())


def test_large_paved_polygon_is_split_with_fill_only_symbol_and_separate_outline():
    import re

    from shapely.geometry import Point

    big = Point(0, 0).buffer(100.0, quad_segs=600)  # ~2400 vrcholů > AREA_SPLIT_VERTICES
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    parts = di._geometry_part(big, name="Chodníky", code="501.0", clip_bounds=None, **kw)
    assert [p.name for p in parts] == ["Chodníky", "Chodníky – obrys (501.100)"]
    fill, outline = parts
    from app.pipeline.oom_symbol_map import symbol_index_for_code

    fill_idx = symbol_index_for_code("sprint_4000", 4000, "501.1")
    edge_idx = symbol_index_for_code("sprint_4000", 4000, "501.100")
    assert fill.count >= 2 and all(f'symbol="{fill_idx}"' in o for o in fill.objects_xml.split("</object>")[:-1])
    assert all(int(n) <= di.AREA_SPLIT_VERTICES + 2 for n in re.findall(r'<coords count="(\d+)">', fill.objects_xml))
    assert outline.count >= 3 and f'symbol="{edge_idx}"' in outline.objects_xml  # obrys po kusech
    assert all(int(n) <= di.OUTLINE_CHUNK_VERTICES for n in re.findall(r'<coords count="(\d+)">', outline.objects_xml))
    # malý polygon zůstává běžnou plochou s obrysem v symbolu
    small = di._geometry_part(Point(0, 0).buffer(5.0), name="Chodníky", code="501.0", clip_bounds=None, **kw)
    assert [p.name for p in small] == ["Chodníky"]
    # ořez na výřez: obrys jen po skutečné hranici, nikoli podél hranice výřezu (kratší než plný obvod)
    clipped = di._geometry_part(big, name="Chodníky", code="501.0", clip_bounds=(-50.0, -200.0, 200.0, 200.0), **kw)
    assert [p.name for p in clipped] == ["Chodníky", "Chodníky – obrys (501.100)"]
    n_full = sum(int(n) for n in re.findall(r'<coords count="(\d+)">', outline.objects_xml))
    n_clip = sum(int(n) for n in re.findall(r'<coords count="(\d+)">', clipped[1].objects_xml))
    assert n_clip < n_full  # část obvodu mimo výřez se nekreslí


def test_forest_and_mtbo_drop_sidewalks_beside_roads_but_keep_park_paths(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "osm_paths").mkdir()

    def line(hw, a, b):
        return {
            "type": "Feature",
            "properties": {"highway": hw},
            "geometry": {"type": "LineString", "coordinates": [a, b]},
        }

    feats = [
        line("road_3", [-1000, -1000], [-900, -1000]),
        line("sidewalk", [-1000, -995], [-900, -995]),  # 5 m od silnice → pryč
        line("sidewalk", [-1000, -940], [-900, -940]),  # 60 m od silnice (park) → zůstává
        line("path", [-1000, -975], [-900, -975]),  # pěšina 25 m od silnice → zůstává
        line("footway", [-1000, -1004], [-900, -1004]),  # pěšina bez tagu chodníku těsně u silnice → pryč
    ]
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    for preset in ("forest_10000", "mtbo_10000"):
        kw = dict(preset_id=preset, scale=10000, ref_x=-950.0, ref_y=-1000.0, grivation_deg=0.0)
        parts = op.build_osm_path_parts(work, **kw)
        assert parts and parts[0].count == 3  # silnice + park + pěšina; chodník a pěšina u silnice odpadly
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-950.0, ref_y=-1000.0, grivation_deg=0.0)
    assert op.build_osm_path_parts(work, **kw)[0].count == 5  # sprint chodníky ponechává
    assert op.is_sidewalk_beside_road("sidewalk", [(0, 5), (100, 5)], op.road_zone_geometry(feats[:1]) or None) is False


def test_long_merged_sidewalk_is_cut_only_where_it_runs_along_road(tmp_path):
    from shapely.geometry import LineString

    from app.pipeline import osm_paths as op

    road = {"type": "Feature", "properties": {"highway": "road_3"}, "geometry": {"type": "LineString", "coordinates": [[-1000, -1000], [-880, -1000]]}}
    zone = op.road_zone_geometry([road])
    # sloučený chodník 600 m: prvních ~130 m podél silnice (v pásu 12 m), zbytek daleko
    long_walk = [(-1000, -995), (-700, -995), (-700, -500)]
    cut = op.sidewalk_parallel_zone("sidewalk", long_walk, zone)
    assert cut is not None
    removed = LineString(long_walk).intersection(cut).length
    assert 120 < removed < 140
    # krátké křížení silnice (průchod pásem < 40 m) se nekreslí jinak než dosud – nic se neřeže
    crossing = [(-940, -1100), (-940, -900)]
    assert op.sidewalk_parallel_zone("sidewalk", crossing, zone) is None
    assert op.sidewalk_parallel_zone("track", long_walk, zone) is None  # jen chodník / pěšina

    work = tmp_path
    (work / "osm_paths").mkdir()
    feats = [road, {"type": "Feature", "properties": {"highway": "sidewalk"}, "geometry": {"type": "LineString", "coordinates": [list(p) for p in long_walk]}}]
    (work / "osm_paths" / "paths_mixed.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    kw = dict(preset_id="mtbo_10000", scale=10000, ref_x=-950.0, ref_y=-1000.0, grivation_deg=0.0)
    assert op.build_osm_path_parts(work, **kw)[0].count == 2  # silnice + zbytek chodníku (ne celý, ne nic)


def test_forest_and_mtbo_drop_sidewalks_beside_parking_too(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "osm_paths").mkdir()

    def line(hw, a, b):
        return {"type": "Feature", "properties": {"highway": hw}, "geometry": {"type": "LineString", "coordinates": [a, b]}}

    feats = [
        line("road_3", [-1000, -1000], [-900, -1000]),
        line("sidewalk", [-1000, -975], [-900, -975]),  # 25 m od silnice, ale u okraje parkoviště → pryč
        line("sidewalk", [-1000, -940], [-900, -940]),  # park daleko od všeho → zůstává
    ]
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    parking = {
        "type": "Feature",
        "properties": {"kind": "parking"},
        "geometry": {"type": "Polygon", "coordinates": [[[-990, -990], [-910, -990], [-910, -978], [-990, -978], [-990, -990]]]},
    }
    (work / "osm_paths" / "features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [parking]}), encoding="utf-8"
    )
    assert op.osm_parking_geometry(work) is not None
    for preset in ("forest_10000", "mtbo_10000"):
        kw = dict(preset_id=preset, scale=10000, ref_x=-950.0, ref_y=-1000.0, grivation_deg=0.0)
        assert op.build_osm_path_parts(work, **kw)[0].count == 2  # silnice + park; chodník u parkoviště odpadl
    (work / "osm_paths" / "features.geojson").unlink()
    kw = dict(preset_id="forest_10000", scale=10000, ref_x=-950.0, ref_y=-1000.0, grivation_deg=0.0)
    assert op.build_osm_path_parts(work, **kw)[0].count == 3  # bez parkoviště zůstává


def test_forest_drops_sidewalk_beside_pedestrian_area(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "osm_paths").mkdir()

    def line(hw, a, b):
        return {"type": "Feature", "properties": {"highway": hw}, "geometry": {"type": "LineString", "coordinates": [a, b]}}

    # bez silnice: jen pěší zóna + chodník u ní + chodník v parku
    feats = [
        line("sidewalk", [-1000, -975], [-900, -975]),
        line("sidewalk", [-1000, -940], [-900, -940]),
    ]
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    ped = {
        "type": "Feature",
        "properties": {"kind": "pedestrian_area", "oom_code": "501"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-990, -990], [-910, -990], [-910, -978], [-990, -978], [-990, -990]]],
        },
    }
    (work / "osm_paths" / "features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [ped]}), encoding="utf-8"
    )
    assert op.osm_paved_beside_geometry(work) is not None
    kw = dict(preset_id="forest_10000", scale=10000, ref_x=-950.0, ref_y=-960.0, grivation_deg=0.0)
    assert op.build_osm_path_parts(work, **kw)[0].count == 1  # jen park; chodník u pěší zóny odpadl
    playground = {
        "type": "Feature",
        "properties": {"kind": "playground", "oom_code": "501"},
        "geometry": ped["geometry"],
    }
    (work / "osm_paths" / "features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [playground]}), encoding="utf-8"
    )
    assert op.build_osm_path_parts(work, **kw)[0].count == 2  # hřiště nepotlačuje chodník


def test_forest_uses_dtm_buildings_and_fences_replace_osm(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "buildings.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1000, -990, -990)]]}),
        encoding="utf-8",
    )
    (work / "dtm" / "fences.json").write_text(
        json.dumps({"fences": [{"code": "518", "coords": [[-1000, -980], [-950, -980]]}]}), encoding="utf-8"
    )
    (work / "osm_paths").mkdir()

    def feat(kind, geom):
        return {"type": "Feature", "properties": {"source": "osm", "kind": kind, "oom_code": "521"}, "geometry": geom}

    ring = [[-999.5, -999.5], [-989.5, -999.5], [-989.5, -989.5], [-999.5, -989.5], [-999.5, -999.5]]
    feats = [
        feat("building", {"type": "Polygon", "coordinates": [ring]}),  # dvojče DTM budovy
        feat("fence", {"type": "LineString", "coordinates": [[-1000, -975], [-950, -975]]}),  # OSM plot
    ]
    (work / "osm_paths" / "features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    kw = dict(preset_id="forest_10000", scale=10000, ref_x=-975.0, ref_y=-985.0, grivation_deg=0.0)
    parts = op.build_osm_feature_parts(work, **kw)
    assert [p.name for p in parts] == ["Budovy (DTM + OSM)"] and parts[0].count == 1  # DTM budova, OSM dvojče pryč; plot kreslí DTM část
    fences = di.build_dtm_fence_parts(work, **kw)
    assert fences and fences[0].count == 2  # spojené ploty DTM (50 m) + OSM (50 m)


def test_forest_and_mtbo_fill_dtm_fence_enclosures_with_olive(tmp_path):
    work = tmp_path
    (work / "dtm").mkdir()
    ring = [[-1000, -980], [-950, -980], [-950, -940], [-1000, -940], [-1000, -980]]
    (work / "dtm" / "fences.json").write_text(
        json.dumps({"fences": [{"code": "518", "coords": ring}], "fills": [ring]}), encoding="utf-8"
    )
    kw = dict(scale=10000, ref_x=-975.0, ref_y=-960.0, grivation_deg=0.0)
    forest = di.build_dtm_fence_parts(work, preset_id="forest_10000", **kw)
    assert [p.name for p in forest] == ["DTM – oplocené plochy", "Ploty (DTM + OSM)"]
    mtbo = di.build_dtm_fence_parts(work, preset_id="mtbo_10000", **kw)
    assert [p.name for p in mtbo] == ["DTM – oplocené plochy", "Ploty (DTM + OSM)"]


def test_mtbo_draws_only_large_dtm_parking_and_osm_duplicate_is_dropped(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "parking.json").write_text(
        json.dumps(
            {
                "polygons": [
                    [[list(p) for p in ring] for ring in _rect(-1000, -1000, -970, -985)],  # 450 m² → kreslí se
                    [[list(p) for p in ring] for ring in _rect(-900, -900, -890, -890)],  # 100 m² → ne
                ]
            }
        ),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()
    ring = [[-995, -998], [-975, -998], [-975, -988], [-995, -988], [-995, -998]]
    feat = {
        "type": "Feature",
        "properties": {"source": "osm", "kind": "parking", "oom_code": "501"},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }
    (work / "osm_paths" / "features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [feat]}), encoding="utf-8"
    )
    kw = dict(preset_id="mtbo_10000", scale=10000, ref_x=-985.0, ref_y=-990.0, grivation_deg=0.0)
    parts = di.build_dtm_parking_parts(work, **kw)
    assert [p.name for p in parts] == ["DTM – velká parkoviště", "DTM – velká parkoviště – obrys (501.2)"]
    assert parts[0].count == 1
    assert op.build_osm_feature_parts(work, **kw) == []  # OSM parkoviště nad DTM parkovištěm zahozeno
    # les stejně (výplň 501.1 + okraj 501.2); sprint dál 501.0
    forest = {**kw, "preset_id": "forest_10000"}
    assert [p.count for p in di.build_dtm_parking_parts(work, **forest)] == [1, 1]
    assert op.build_osm_feature_parts(work, **forest) == []
    assert di.build_dtm_parking_parts(work, **{**kw, "preset_id": "sprint_4000"}) != []


def test_mtbo_road_corridor_merges_sidewalks_over_grass_strip_and_respects_width_and_bikes():
    # silnice 8 m široká, 100 m dlouhá; chodníky 2 m široké, 2 m (pruh zeleně) od silnice, po obou stranách
    roads = ds.dtm_union([_rect(0, 0, 100, 8)])
    sidewalks = ds.dtm_union([_rect(0, 10, 100, 12), _rect(0, -4, 100, -2)])
    corridor = ds.road_corridor(roads, sidewalks, scale=10000)  # min. šířka 0,9 mm = 9 m
    assert corridor is not None
    assert abs(corridor.bounds[1] - (-4.0)) < 0.1 and abs(corridor.bounds[3] - 12.0) < 0.1  # chodníky přes pruh trávy
    # sdílený chodník (kolo povoleno) se nesloučí
    bike = [[(0.0, 11.0), (100.0, 11.0)]]
    shared = ds.road_corridor(roads, sidewalks, bike, scale=10000)
    assert abs(shared.bounds[3] - 8.0) < 0.1 and abs(shared.bounds[1] - (-4.0)) < 0.1
    # pruh zeleně širší než 3 m se nepřeskočí
    wide_road = ds.dtm_union([_rect(0, 0, 100, 10)])  # 10 m = nad prahem i sama
    far = ds.dtm_union([_rect(0, 14, 100, 16)])  # 4 m trávy > 3 m
    assert abs(ds.road_corridor(wide_road, far, scale=10000).bounds[3] - 10.0) < 0.1
    # úzká silnice (bez chodníků pod 9 m) zůstává čárou
    narrow = ds.dtm_union([_rect(0, 0, 100, 6)])
    assert ds.road_corridor(narrow, None, scale=10000) is None
    # na 1:15000 je práh 13,5 m: silnice s chodníky 16 m ještě projde, 8 m silnice sama ne
    assert ds.road_corridor(roads, sidewalks, scale=15000) is not None
    assert ds.road_corridor(roads, None, scale=15000) is None


def test_mtbo_road_corridor_counts_parking_between_road_and_sidewalk():
    roads = ds.dtm_union([_rect(0, 0, 100, 8)])
    parking = ds.dtm_union([_rect(0, 8, 100, 13), _rect(0, 20, 100, 80)])  # pruh u silnice + vzdálené velké parkoviště
    sidewalk = ds.dtm_union([_rect(0, 14, 100, 16)])  # 1 m za parkovacím pruhem
    without = ds.road_corridor(roads, sidewalk, scale=10000)
    assert without is None or without.bounds[3] < 9  # sám chodník je 14 m od silnice → nesloučí se (a samotná silnice je užší)
    corridor = ds.road_corridor(roads, sidewalk, scale=10000, parking=parking)
    assert corridor is not None
    assert abs(corridor.bounds[1] - 0.0) < 0.1 and abs(corridor.bounds[3] - 16.0) < 0.1  # silnice + parkování + chodník
    assert corridor.intersection(ds.dtm_union([_rect(0, 20, 100, 80)])).area < 1.0  # vzdálené parkoviště zůstává mimo


def test_mtbo_corridor_needs_real_roadway_and_long_enough_piece():
    # úzká vedlejší silnice (4 m) s chodníky po obou stranách: koridor nevznikne, zůstává čára 502.x
    minor = ds.dtm_union([_rect(0, 0, 100, 4)])
    side = ds.dtm_union([_rect(0, 5, 100, 7), _rect(0, -3, 100, -1)])
    assert ds.road_corridor(minor, side, scale=10000) is None
    # široký ale krátký uzel (rozbočka, 25 × 20 m) se nekreslí koridorem
    junction = ds.dtm_union([_rect(0, 0, 25, 20)])
    assert ds.road_corridor(junction, None, scale=10000) is None
    # dlouhá silnice 10 m × 100 m ano
    assert ds.road_corridor(ds.dtm_union([_rect(0, 0, 100, 10)]), None, scale=10000) is not None


def test_wide_parts_keeps_only_wide_enough_pieces():
    geom = ds.dtm_union([_rect(0, 0, 60, 4), _rect(0, 20, 60, 21.5)])
    kept = ds.wide_parts(geom, 2.5, min_area_m2=40.0)
    assert kept is not None and kept.bounds[1] == 0 and kept.bounds[3] == 4  # úzký pruh (1,5 m) vypadl
    assert ds.wide_parts(geom, 5.0) is None


def test_mtbo_draws_wide_dtm_sidewalks_away_from_roads_and_drops_osm_line_inside(tmp_path):
    work = tmp_path
    (work / "dtm").mkdir()
    (work / "osm_paths").mkdir()
    wide = _rect(0, 0, 80, 6)  # široká cesta daleko od silnice
    narrow = _rect(0, 30, 80, 33.5)  # úzký pruh (3,5 m < 0,5 mm v 1:10000) – kreslí ho čára cesty
    near = _rect(0, 200, 80, 206)  # široký, ale u silnice
    road = _rect(0, 190, 80, 198)
    for name, polys in (("sidewalks.json", [wide, narrow, near]), ("roads.json", [road])):
        (work / "dtm" / name).write_text(
            json.dumps({"polygons": [[[list(p) for p in ring] for ring in poly] for poly in polys]}), encoding="utf-8"
        )
    geom = di.mtbo_sidewalk_geometry(work, 10000, "mixed")
    assert geom is not None
    assert geom.intersection(ds.dtm_union([wide])).area > 0.9 * 80 * 6
    assert geom.intersection(ds.dtm_union([narrow])).area < 1.0
    assert geom.intersection(ds.dtm_union([near])).area < 1.0
    parts = di.build_dtm_sidewalk_parts(
        work, preset_id="mtbo_10000", scale=10000, ref_x=40.0, ref_y=2.0, grivation_deg=0.0
    )
    assert parts and parts[0].count >= 1
    assert di.build_dtm_sidewalk_parts(
        work, preset_id="forest_10000", scale=10000, ref_x=40.0, ref_y=2.0, grivation_deg=0.0
    ) == []


def test_mtbo_corridor_bridges_narrower_stretch_between_wide_pieces():
    # silnice 10 m – úsek 8 m – 10 m: úzký úsek navazuje na široké kusy, koridor se nepřeruší
    road = ds.dtm_union([_rect(0, 0, 100, 10), _rect(100, 1, 130, 9), _rect(130, 0, 230, 10)])
    corridor = ds.road_corridor(road, None, scale=10000)
    assert corridor is not None and corridor.geom_type == "Polygon"
    assert corridor.area > 0.97 * road.area
    # úzký úsek daleko od širokého kusu (za ``CORRIDOR_BRIDGE_M``) se nepřipojí
    far = ds.dtm_union([_rect(0, 0, 100, 10), _rect(100, 1, 300, 9)])
    assert ds.road_corridor(far, None, scale=10000).bounds[2] <= 100 + ds.CORRIDOR_BRIDGE_M + 5


def test_mtbo_corridor_parts_and_road_lines_removed_inside(tmp_path, monkeypatch):
    from app.pipeline import osm_paths as op

    monkeypatch.setattr(di, "CORRIDOR_WITH_SIDEWALKS", True)  # test původního chování s chodníky v koridoru

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "roads.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1004, -900, -996)]]}),
        encoding="utf-8",
    )
    (work / "dtm" / "sidewalks.json").write_text(
        json.dumps(
            {
                "polygons": [
                    [[list(p) for p in ring] for ring in _rect(-1000, -993, -900, -991)],
                    [[list(p) for p in ring] for ring in _rect(-1000, -1009, -900, -1007)],
                ]
            }
        ),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()
    road = {
        "type": "Feature",
        "properties": {"highway": "road_3"},
        "geometry": {"type": "LineString", "coordinates": [[-1000, -1000], [-900, -1000]]},
    }
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [road]}), encoding="utf-8"
    )
    kw = dict(preset_id="mtbo_10000", scale=10000, ref_x=-950.0, ref_y=-1000.0, grivation_deg=0.0)
    parts = di.build_mtbo_corridor_parts(work, **kw)
    assert [p.name for p in parts] == ["MTBO – silnice s chodníky (koridor)", "MTBO – silnice s chodníky (koridor) – obrys (501.100)"]
    assert op.build_osm_path_parts(work, **kw) == []  # čára silnice leží uvnitř koridoru → nekreslí se
    # chodník uvnitř koridoru je jeho součástí (nekreslí se)
    sw_line = {
        "type": "Feature",
        "properties": {"highway": "sidewalk"},
        "geometry": {"type": "LineString", "coordinates": [[-1000, -992], [-900, -992]]},
    }
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [road, sw_line]}), encoding="utf-8"
    )
    assert op.build_osm_path_parts(work, **kw) == []
    (work / "osm_paths" / "bike_paths.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [{**sw_line, "properties": {"bike": True}}]}), encoding="utf-8"
    )
    # smíšená stezka, která by čárou zasahovala do kresby silnice / koridoru, se nekreslí (Petr 2026-10-25)
    assert op.build_osm_path_parts(work, **kw) == []
    forest = {**kw, "preset_id": "forest_10000"}
    assert [p.name for p in di.build_mtbo_corridor_parts(work, **forest)] == [
        "Les – silnice s chodníky z DTM (koridor)",
        "Les – silnice s chodníky z DTM (koridor) – obrys (501.2)",
    ]
    assert [p.count for p in op.build_osm_path_parts(work, **forest)] == [1]  # silnice v koridoru ne, sdílený chodník ano
    di.MTBO_ROAD_CORRIDOR = False
    try:
        assert di.build_mtbo_corridor_parts(work, **kw) == []
        assert op.build_osm_path_parts(work, **kw)[0].count == 2  # bez koridoru jako dřív: čára 502.x + sdílený chodník
    finally:
        di.MTBO_ROAD_CORRIDOR = True


def test_dtm_green_is_cut_out_of_osm_paved_areas(tmp_path):
    from shapely.geometry import shape

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "green.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-990, -990, -970, -980)]]}),
        encoding="utf-8",
    )

    def area(kind, x0, y0, x1, y1):
        ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
        return {
            "type": "Feature",
            "properties": {"source": "osm", "kind": kind},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }

    feats = [
        area("parking", -1000, -1000, -960, -970),  # přes zeleň → vystřihne se
        area("building", -1000, -1000, -960, -970),  # budova se nemění
        area("parking", -900, -900, -880, -880),  # daleko
        area("playground", -995, -995, -965, -975),  # hřiště z OSM je přesnější než zeleň z DTM → nemění se
    ]
    out = di.apply_dtm_priority_to_paved(work, feats)
    playground = [shape(f["geometry"]) for f in out if f["properties"]["kind"] == "playground"]
    assert len(playground) == 1 and abs(playground[0].area - 30 * 20) < 1e-6
    green = ds.dtm_union([_rect(-990, -990, -970, -980)])
    parking = [shape(f["geometry"]) for f in out if f["properties"]["kind"] == "parking"]
    assert len(parking) == 2
    assert all(g.intersection(green).area < 1e-6 for g in parking)
    assert abs(parking[0].area - (40 * 30 - 20 * 10)) < 1e-6  # původních 1200 m² − 200 m² zeleně
    assert [f["properties"]["kind"] for f in out].count("building") == 1


def test_reclassify_sidewalk_with_osm_road_inside_as_road():
    wide = _rect(0, 0, 60, 12)  # 12 m široká „ChodnikPlocha“ s OSM silnicí uvnitř = chyba v DTM
    crossing = _rect(100, 0, 140, 2.5)  # úzký chodník, který silnice jen kříží
    plain = _rect(200, 0, 240, 2.5)  # chodník bez silnice
    road_inside = [(0.0, 6.0), (60.0, 6.0)]  # 60 m uvnitř široké plochy
    road_cross = [(120.0, -10.0), (120.0, 10.0)]  # kříží úzký chodník (2,5 m uvnitř)
    keep, moved = ds.reclassify_sidewalks_as_roads([wide, crossing, plain], [road_inside, road_cross])
    assert moved == [wide] and keep == [crossing, plain]
    # velmi dlouhá silnice uvnitř úzké plochy (≥ 150 m) → také silnice
    long_strip = _rect(0, 0, 400, 3)
    keep, moved = ds.reclassify_sidewalks_as_roads([long_strip], [[(0.0, 1.5), (200.0, 1.5)]])
    assert moved == [long_strip]
    assert ds.reclassify_sidewalks_as_roads([wide], []) == ([wide], [])


def test_misclassified_sidewalk_is_drawn_as_road_part(tmp_path):
    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "sidewalks.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1006, -940, -994)]]}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()
    road = {
        "type": "Feature",
        "properties": {"highway": "road_3"},
        "geometry": {"type": "LineString", "coordinates": [[-1000, -1000], [-940, -1000]]},
    }
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [road]}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-970.0, ref_y=-1000.0, grivation_deg=0.0)
    assert di.build_dtm_sidewalk_parts(work, **kw) == []  # žádný chodník, plocha je silnice
    parts = di.build_dtm_road_parts(work, **kw)
    assert [p.name for p in parts] == ["Silnice (DTM + OSM)"] and parts[0].count == 1


def test_dtm_buildings_replace_osm_twins_and_keep_osm_only_buildings(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "buildings.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1000, -990, -990)]]}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()

    def bld(x0, y0, x1, y1):
        ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
        return {
            "type": "Feature",
            "properties": {"source": "osm", "kind": "building", "oom_code": "521"},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }

    feats = [bld(-999.5, -999.5, -989.5, -989.5), bld(-960, -960, -950, -950)]  # dvojče posunuté o 0,5 m + nová
    (work / "osm_paths" / "features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    merged = di.merge_dtm_buildings(work, feats)
    sources = sorted(f["properties"]["source"] for f in merged)
    assert sources == ["dtm", "osm"]  # OSM dvojče zahozeno, nová OSM budova zůstala
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-980.0, ref_y=-980.0, grivation_deg=0.0)
    parts = op.build_osm_feature_parts(work, **kw)
    assert [p.name for p in parts] == ["Budovy (DTM + OSM)"] and parts[0].count == 2


def test_touching_dtm_buildings_are_fused_into_one_object_and_shp_uses_dtm(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "buildings.json").write_text(
        json.dumps(
            {
                "polygons": [
                    [[list(p) for p in ring] for ring in _rect(-1000, -1000, -990, -990)],
                    [[list(p) for p in ring] for ring in _rect(-989.95, -1000, -980, -990)],  # škvíra 5 cm
                    [[list(p) for p in ring] for ring in _rect(-960, -960, -950, -950)],  # samostatná
                ]
            }
        ),
        encoding="utf-8",
    )
    merged = di.merge_dtm_buildings(work, [])
    assert sorted(f["properties"]["source"] for f in merged) == ["dtm", "dtm"]  # 2 objekty místo 3
    # budovy do SHP / filtru srázů: stejné skládání jako v .omap
    (work / "osm_paths").mkdir()
    (work / "osm_paths" / "buildings.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"kind": "building"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[-999.5, -999.5], [-990, -999.5], [-990, -990], [-999.5, -990], [-999.5, -999.5]]],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert len(di.dtm_mask_wkbs(work)) == 3  # maska zbytkových ploch obsahuje budovy z DTM


def test_small_paved_parts_are_separate_501_0_areas(tmp_path):
    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "small_paved.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(0, 0, 3, 2)]]}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    parts = di.build_dtm_small_paved_parts(work, **kw)
    assert [p.name for p in parts] == ["DTM – malé zpevněné stavby (501.0)"] and parts[0].count == 1
    assert di.build_dtm_small_paved_parts(work, **{**kw, "preset_id": "forest_10000"}) == []


def test_steps_keep_only_osm_steps_outside_dtm_stairs_and_sidewalks():
    stair_zone = ds.dtm_union([_rect(10, -1, 12, 1)]).buffer(ds.STAIRS_ZONE_M)
    cover = ds.dtm_union([_rect(0, -1, 40, 1)]).buffer(0.5)  # chodník z DTM
    # OSM čára schodů přes celý chodník (0–40) a dál za ním (40–60)
    pieces = ds.steps_keep([(0.0, 0.0), (60.0, 0.0)], stair_zone, cover)
    assert len(pieces) == 1
    assert min(x for x, _y in pieces[0]) > 40.0 and abs(max(x for x, _y in pieces[0]) - 60.0) < 0.1
    # bez ploch chodníků zůstane vše kromě schodiště z DTM
    pieces = ds.steps_keep([(0.0, 0.0), (60.0, 0.0)], stair_zone, None)
    assert len(pieces) == 2


def test_stairs_axis_follows_reference_path_and_centroid():
    from shapely.geometry import LineString

    stairs = _rect(10, -1, 12, 1)  # čtvercové schodiště 2 × 2 m
    path = LineString([(0.0, 0.0), (30.0, 0.0)])  # cesta ve směru x
    axis = ds.stairs_axis(stairs, [[path], []])
    (x0, y0), (x1, y1) = axis
    assert abs(y1 - y0) < 1e-6 and abs(y0) < 1e-6  # vodorovně, středem schodiště
    assert abs((x1 - x0) - (2.0 + 2 * ds.STAIRS_AXIS_EXTRA_M)) < 1e-6
    # bez cesty v okolí: po delší straně
    axis = ds.stairs_axis(_rect(0, 0, 6, 2))
    (x0, y0), (x1, y1) = axis
    assert abs(y1 - y0) < 1e-6 and abs(x1 - x0) > 6.0
    assert ds.stairs_axis(_rect(0, 0, 0.3, 0.3)) is None


def test_long_curved_stairs_get_centerline_along_the_whole_curve():
    import math

    from shapely.geometry import LineString, Polygon

    # zahnuté schodiště: oblouk 90° o poloměru 12 m (délka ≈ 18,8 m) široký 2,6 m
    arc = LineString([(12 * math.cos(a / 20 * math.pi / 2), 12 * math.sin(a / 20 * math.pi / 2)) for a in range(21)])
    poly = arc.buffer(1.3, cap_style=2)
    rings = [list(poly.exterior.coords)]
    axis = ds.stairs_axis(rings)
    assert axis is not None and len(axis) > 3  # více bodů, ne přímka o dvou
    length = LineString(axis).length
    assert 18.0 < length < 20.5  # po celém oblouku
    # konce leží poblíž konců oblouku (zahnutí se neztratilo)
    ends = sorted([axis[0], axis[-1]])
    assert min(math.dist(e, (12.0, 0.0)) for e in ends) < 1.6  # do půl šířky od konce
    assert min(math.dist(e, (0.0, 12.0)) for e in ends) < 1.6
    assert isinstance(Polygon(rings[0]).area, float)


def test_paved_cycleway_on_dtm_sidewalk_is_not_drawn_twice(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "sidewalks.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1003, -940, -997)]]}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()
    feats = [
        {
            "type": "Feature",
            "properties": {"highway": "cycleway_paved"},
            "geometry": {"type": "LineString", "coordinates": [[-1030, -1000], [-910, -1000]]},  # 60 m na chodníku
        }
    ]
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-970.0, ref_y=-1000.0, grivation_deg=0.0)
    paths = op.build_osm_path_parts(work, **kw)
    assert paths and paths[0].count == 2  # jen části před a za plochou chodníku (30 m + 30 m), ne přes ni


def test_end_at_edge_trims_inside_extends_near_and_keeps_crossing():
    area = ds.dtm_union([_rect(10, -5, 20, 5)])
    # končí uvnitř plochy → odříznuto na hraně x = 10
    (line,) = ds.end_at_edge([(0.0, 0.0), (15.0, 0.0)], area)
    assert abs(line[-1][0] - 10.0) < 1e-6 and abs(line[-1][1]) < 1e-6
    # začíná uvnitř → začíná na hraně x = 20
    (line,) = ds.end_at_edge([(15.0, 0.0), (40.0, 0.0)], area)
    assert abs(line[0][0] - 20.0) < 1e-6
    # nedosahuje k ploše o 1 m → prodlouží se na hranu
    (line,) = ds.end_at_edge([(0.0, 0.0), (9.0, 0.0)], area)
    assert abs(line[-1][0] - 10.0) < 1e-6 and len(line) == 3
    # dál než EDGE_SNAP_M → beze změny
    (line,) = ds.end_at_edge([(0.0, 0.0), (6.0, 0.0)], area)
    assert line[-1] == (6.0, 0.0) and len(line) == 2
    # prochází plochou (oba konce venku) → beze změny
    (line,) = ds.end_at_edge([(0.0, 0.0), (40.0, 0.0)], area)
    assert line[0] == (0.0, 0.0) and line[-1] == (40.0, 0.0)
    # celá uvnitř → zmizí
    assert ds.end_at_edge([(12.0, 0.0), (18.0, 0.0)], area) == []
    # bez plochy beze změny
    assert ds.end_at_edge([(0.0, 0.0), (5.0, 0.0)], None) == [[(0.0, 0.0), (5.0, 0.0)]]


def test_paths_end_exactly_on_paved_edge_in_sprint(tmp_path):
    import re

    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "sidewalks.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1002, -960, -998)]]}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()

    def path(a, b):
        return {
            "type": "Feature",
            "properties": {"highway": "path"},
            "geometry": {"type": "LineString", "coordinates": [a, b]},
        }

    feats = [
        path([-990, -1030], [-990, -1000]),  # končí uvnitř plochy (y -1002…-998)
        path([-980, -1030], [-980, -1003]),  # nedosahuje k ploše o 1 m
    ]
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-980.0, ref_y=-1002.0, grivation_deg=0.0)
    parts = op.build_osm_path_parts(work, **kw)
    assert parts and parts[0].count == 2
    # souřadnice v .omap jsou v 0,001 mm papíru; 1:4000 → 1 m terénu = 250 jednotek; map y = −(Y − ref_y) · 250
    ys = [int(y) for y in re.findall(r"-?\d+ (-?\d+)[ ;]", parts[0].objects_xml)]
    assert ys
    # obě cesty končí na hraně plochy (Y = −1002 → map y 0): žádný bod uvnitř plochy (map y < 0 = Y > −1002)
    assert min(ys) == 0 and ys.count(0) == 2


def test_complex_c_shaped_stairs_are_left_to_the_mapper_as_area(tmp_path):
    work = tmp_path
    (work / "dtm").mkdir()
    u_shape = [  # „C“: dvě ramena (4 × 1 m) spojená podestou (1 × 5 m)
        [(0, 0), (4, 0), (4, 1), (1, 1), (1, 4), (4, 4), (4, 5), (0, 5), (0, 0)]
    ]
    straight = _rect(20, 0, 30, 2)
    assert ds.is_complex_stairs(u_shape) and not ds.is_complex_stairs(straight)
    (work / "dtm" / "stairs.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in poly] for poly in (u_shape, straight)]}), encoding="utf-8"
    )
    (work / "osm_paths").mkdir()
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=10.0, ref_y=0.0, grivation_deg=0.0)
    parts = di.build_dtm_stairs_parts(work, **kw)
    names = [p.name for p in parts]
    assert names[0] == "DTM – schodiště (532.7)" and parts[0].count == 1  # jen přímé schodiště čárou
    assert any("složitá schodiště" in n for n in names)
    # les / MTBO složitá schodiště nekreslí vůbec; přímé ≥10 m zůstává schodištěm (532)
    forest = di.build_dtm_stairs_parts(work, **{**kw, "preset_id": "forest_10000", "scale": 10000})
    assert [p.name for p in forest] == ["DTM – schodiště (532)"] and forest[0].count == 1


def test_tiny_complex_stairs_are_dropped(tmp_path):
    work = tmp_path
    (work / "dtm").mkdir()
    tiny_c = [[(0, 0), (1.5, 0), (1.5, 0.4), (0.4, 0.4), (0.4, 1.1), (1.5, 1.1), (1.5, 1.5), (0, 1.5), (0, 0)]]  # „C“, < 4 m²
    (work / "dtm" / "stairs.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in tiny_c]]}), encoding="utf-8"
    )
    (work / "osm_paths").mkdir()
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    assert di.build_dtm_stairs_parts(work, **kw) == []  # střep: ani čára, ani plocha


def test_osm_paved_slivers_are_not_drawn(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "osm_paths").mkdir()

    def area(kind, ring):
        return {
            "type": "Feature",
            "properties": {"source": "osm", "kind": kind, "oom_code": "501"},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }

    sliver = [[-1000, -1000], [-973, -1007], [-1000, -1000.2], [-1000, -1000]]  # 27 m dlouhé, skoro přímka
    real = [[-960, -960], [-940, -960], [-940, -940], [-960, -940], [-960, -960]]
    (work / "osm_paths" / "features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [area("platform", sliver), area("parking", real)]}),
        encoding="utf-8",
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-980.0, ref_y=-980.0, grivation_deg=0.0)
    names = [p.name for p in op.build_osm_feature_parts(work, **kw)]
    assert names == ["OSM parkoviště (501 zpevněná)"]  # nástupiště-sliver pryč, skutečná plocha zůstává
    assert op.is_sliver_polygon([sliver]) and not op.is_sliver_polygon([real])


def test_forest_and_mtbo_use_dtm_stairs_with_own_symbol_and_min_length(tmp_path):
    from app.pipeline import osm_paths as op
    from app.pipeline.oom_symbol_map import symbol_index_for_code

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "stairs.json").write_text(
        json.dumps(
            {
                "polygons": [
                    # 14 × 3 m → osa ≥10 m → schodišťový symbol
                    [[list(p) for p in ring] for ring in _rect(-1000, -1000, -986, -997)],
                    [[list(p) for p in ring] for ring in _rect(-980, -1000, -978.5, -998.5)],  # 1,5 m → ne
                ]
            }
        ),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()
    osm_steps = {
        "type": "Feature",
        "properties": {"highway": "steps"},
        "geometry": {"type": "LineString", "coordinates": [[-1010, -998.5], [-960, -998.5]]},
    }
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [osm_steps]}), encoding="utf-8"
    )
    for preset, code in (("forest_10000", "532"), ("mtbo_10000", "843")):
        kw = dict(preset_id=preset, scale=10000, ref_x=-985.0, ref_y=-999.0, grivation_deg=0.0)
        parts = di.build_dtm_stairs_parts(work, **kw)
        assert parts and parts[0].count == 1  # jen schodiště nad 3 m (a ≥10 m → schody)
        assert f'symbol="{symbol_index_for_code(preset, 10000, code)}"' in parts[0].objects_xml
        paths = op.build_osm_path_parts(work, **kw)
        assert paths and paths[0].count == 3  # OSM schody přerušené dvěma schodišti z DTM: 3 kusy


def test_forest_mtbo_short_stairs_drawn_as_path(tmp_path):
    """Les/MTBO: schody pod ~10 m (jeden dílek) jako pěšina; sprint zůstává schodištěm."""
    from app.pipeline import osm_paths as op
    from app.pipeline.oom_symbol_map import symbol_index_for_code

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "stairs.json").write_text(
        json.dumps(
            {
                "polygons": [
                    # 7 × 3 m → osa ~7,6 m < 10 m → les/MTBO pěšina
                    [[list(p) for p in ring] for ring in _rect(-1000, -1000, -993, -997)],
                ]
            }
        ),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()
    short_steps = {
        "type": "Feature",
        "properties": {"highway": "steps"},
        "geometry": {"type": "LineString", "coordinates": [[0.0, 0.0], [8.0, 0.0]]},
    }
    long_steps = {
        "type": "Feature",
        "properties": {"highway": "steps"},
        "geometry": {"type": "LineString", "coordinates": [[0.0, 20.0], [15.0, 20.0]]},
    }
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [short_steps, long_steps]}),
        encoding="utf-8",
    )

    assert op.osm_steps_draw_code("forest_10000", 9.9) == "506"
    assert op.osm_steps_draw_code("forest_10000", 10.0) == "532"
    assert op.osm_steps_draw_code("mtbo_10000", 5.0) == "834"
    assert op.osm_steps_draw_code("mtbo_10000", 12.0) == "843"
    assert op.osm_steps_draw_code("sprint_4000", 5.0) == "532.7"

    for preset, path_code, steps_code in (
        ("forest_10000", "506", "532"),
        ("mtbo_10000", "834", "843"),
    ):
        kw = dict(preset_id=preset, scale=10000, ref_x=-996.5, ref_y=-998.5, grivation_deg=0.0)
        dtm_parts = di.build_dtm_stairs_parts(work, **kw)
        assert len(dtm_parts) == 1 and "pěšina" in dtm_parts[0].name
        assert f'symbol="{symbol_index_for_code(preset, 10000, path_code)}"' in dtm_parts[0].objects_xml

        osm_kw = dict(preset_id=preset, scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
        # Bez DTM schodišť v AOI OSM (jiný výřez souřadnic) — stairs.json je daleko, zóna nezasáhne
        paths = op.build_osm_path_parts(work, **osm_kw)
        assert paths and paths[0].count == 2
        xml = paths[0].objects_xml
        assert f'symbol="{symbol_index_for_code(preset, 10000, path_code)}"' in xml
        assert f'symbol="{symbol_index_for_code(preset, 10000, steps_code)}"' in xml

    sprint_kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-996.5, ref_y=-998.5, grivation_deg=0.0)
    sprint_dtm = di.build_dtm_stairs_parts(work, **sprint_kw)
    assert sprint_dtm and "schodiště (532.7)" in sprint_dtm[0].name
    assert f'symbol="{symbol_index_for_code("sprint_4000", 4000, "532.7")}"' in sprint_dtm[0].objects_xml


def test_dtm_stairs_parts_draw_every_stair_and_osm_steps_only_outside(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "stairs.json").write_text(
        json.dumps(
            {
                "polygons": [
                    [[list(p) for p in ring] for ring in _rect(-1000, -1000, -996, -998)],  # bez OSM čáry
                    [[list(p) for p in ring] for ring in _rect(-970, -1000, -966, -998)],  # na OSM čáře
                ]
            }
        ),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()
    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"highway": "steps"},
                "geometry": {"type": "LineString", "coordinates": [[-980, -999], [-940, -999]]},
            }
        ],
    }
    (work / "osm_paths" / "paths_mixed.geojson").write_text(json.dumps(gj), encoding="utf-8")
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-980.0, ref_y=-999.0, grivation_deg=0.0)
    parts = di.build_dtm_stairs_parts(work, **kw)
    assert [p.name for p in parts] == ["DTM – schodiště (532.7)"] and parts[0].count == 2
    paths = op.build_osm_path_parts(work, **kw)
    assert paths and paths[0].count == 2  # OSM čára přerušená schodištěm z DTM: před ním a za ním


def test_dtm_cover_geometry_clips_zabaged_lines(tmp_path):
    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "roads.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(0, -4, 40, 4)]]}), encoding="utf-8"
    )
    cover = di.dtm_cover_geometry(work)
    assert cover is not None and cover.contains(__import__("shapely.geometry").geometry.Point(20, 0))
    pieces = ds.clip_outside([(-30.0, 0.0), (80.0, 0.0)], cover)
    assert len(pieces) == 2  # část uvnitř plochy se nekreslí


def test_is_surface_sidewalk_excludes_bridges_and_steps():
    assert di.is_surface_sidewalk("sidewalk")
    assert not di.is_surface_sidewalk("steps")
    assert not di.is_surface_sidewalk("path")


def test_sidewalks_parts_and_osm_paths_skip_sidewalks(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "sidewalks.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1000, -970, -998)]]}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()
    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"highway": "sidewalk"},
                "geometry": {"type": "LineString", "coordinates": [[-1000, -999], [-970, -999]]},
            },
            {
                "type": "Feature",
                "properties": {"highway": "path"},
                "geometry": {"type": "LineString", "coordinates": [[-1000, -980], [-970, -980]]},
            },
        ],
    }
    (work / "osm_paths" / "paths_mixed.geojson").write_text(json.dumps(gj), encoding="utf-8")
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-985.0, ref_y=-999.0, grivation_deg=0.0)
    parts = di.build_dtm_sidewalk_parts(work, **kw)
    assert [p.name for p in parts] == ["Chodníky (DTM + OSM)"] and parts[0].count == 1
    paths = op.build_osm_path_parts(work, **kw)
    assert paths and paths[0].count == 1  # jen pěšina, chodník nahradila plocha
    assert di.build_dtm_sidewalk_parts(work, **{**kw, "preset_id": "forest_10000"}) == []


def _write_polys(work, name, polys):
    (work / "dtm").mkdir(exist_ok=True)
    (work / "dtm" / name).write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in poly] for poly in polys]}), encoding="utf-8"
    )


def _write_paths(work, feats):
    (work / "osm_paths").mkdir(exist_ok=True)
    for name in ("paths_mixed.geojson", "paths_osm.geojson"):
        (work / "osm_paths" / name).write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")


def _feat(coords, hw):
    return {"type": "Feature", "properties": {"highway": hw}, "geometry": {"type": "LineString", "coordinates": coords}}


def test_mtbo_corridor_only_along_drawn_roads(tmp_path):
    # Manipulační plocha / areál 200 × 150 m z DTM (roads.json) bez silnice se koridorem nezalije (Praha 5: 12 ha).
    work = tmp_path
    _write_polys(work, "roads.json", [_rect(0, 0, 200, 150)])
    _write_paths(work, [_feat([(20, 20), (180, 130)], "track")])
    assert di.mtbo_corridor_geometry(work, 10000) is None
    # Silnice areálem: koridor jen v pásu kolem její osy, ne celý areál.
    work2 = tmp_path / "b"
    work2.mkdir()
    _write_polys(work2, "roads.json", [_rect(0, 0, 200, 150)])
    _write_paths(work2, [_feat([(0, 75), (200, 75)], "road_3")])
    corridor = di.mtbo_corridor_geometry(work2, 10000)
    assert corridor is not None
    assert corridor.bounds[1] >= 75 - di.CORRIDOR_ROAD_AXIS_M - 0.1
    assert corridor.bounds[3] <= 75 + di.CORRIDOR_ROAD_AXIS_M + 0.1
    assert corridor.is_valid


def test_mtbo_footway_along_corridor_edge_is_not_drawn(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    _write_polys(work, "roads.json", [_rect(0, -10, 200, 10)])  # široká ulice (20 m) z DTM
    road = _feat([(0, 0), (200, 0)], "road_3")
    edge_path = _feat([(0, 14), (200, 14)], "footway")  # 4 m od okraje koridoru – v 1:10000 splyne s obrubníkem
    far_path = _feat([(0, 60), (200, 60)], "footway")
    green_path = _feat([(0, 16), (200, 16)], "footway")  # 6 m od okraje (za pásem zeleně) – čitelně oddělená
    _write_paths(work, [road, edge_path, far_path, green_path])
    op._PAVED_NETWORK_CACHE.clear()
    parts = op.build_osm_path_parts(work, preset_id="mtbo_10000", scale=10000, ref_x=100.0, ref_y=0.0, grivation_deg=0.0)
    assert sum(p.count for p in parts) == 2  # zůstane vzdálená pěšina a chodník za pásem zeleně


def test_forest_footway_near_corridor_edge_is_dropped_with_line_gap(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    _write_polys(work, "roads.json", [_rect(0, -10, 200, 10)])  # široká ulice (20 m) z DTM
    near_path = _feat([(0, 15), (200, 15)], "footway")  # 5 m od okraje koridoru – v lese (mezera 0,35 mm) splyne s okrajem 501.2
    far_path = _feat([(0, 18), (200, 18)], "footway")  # 8 m od okraje – čitelně oddělená
    _write_paths(work, [_feat([(0, 0), (200, 0)], "road_3"), near_path, far_path])
    op._PAVED_NETWORK_CACHE.clear()
    parts = op.build_osm_path_parts(work, preset_id="forest_10000", scale=10000, ref_x=100.0, ref_y=0.0, grivation_deg=0.0)
    assert sum(p.count for p in parts) == 1  # zůstane jen vzdálená pěšina
    assert op.FOREST_DTM_EDGE_GAP_MM == op.LINE_MIN_GAP_MM > op.MTBO_DTM_EDGE_GAP_MM


def test_mtbo_dtm_geometry_is_cached_until_inputs_change(tmp_path, monkeypatch):
    work = tmp_path
    _write_polys(work, "roads.json", [_rect(0, -6, 200, 6)])
    _write_paths(work, [_feat([(0, 0), (200, 0)], "road_3")])
    calls = []
    real = ds.road_corridor
    monkeypatch.setattr(ds, "road_corridor", lambda *a, **k: calls.append(1) or real(*a, **k))
    di.mtbo_corridor_geometry(work, 10000)
    di.mtbo_dtm_area_geometry(work, 10000)
    di.mtbo_parking_geometry(work, 10000)
    assert len(calls) == 1
    import os
    import time

    p = work / "dtm" / "roads.json"
    t = time.time() + 5
    os.utime(p, (t, t))
    di.mtbo_corridor_geometry(work, 10000)
    assert len(calls) == 2


def test_generalize_area_fills_bays_drops_spurs_and_small_holes():
    from shapely.geometry import Polygon, box

    # pás 100 × 20 m se zálivem 2 m, výběžkem 2 m a ostrůvkem 4 × 4 m (1:10000: zaliv < 4 m, výběžek < 3 m, díra < 100 m²)
    base = box(0, 0, 100, 20).difference(box(40, 15, 42, 20)).difference(box(70, 8, 74, 12))
    spur = box(20, 20, 22, 30)
    g = ds.generalize_area(base.union(spur), 10000)
    assert g.is_valid and len(g.interiors if g.geom_type == "Polygon" else []) == 0  # ostrůvek zacelen
    assert g.contains(Polygon([(40.5, 15.5), (41.5, 15.5), (41.5, 19.5), (40.5, 19.5)]))  # záliv zacelen
    assert g.bounds[3] < 21  # výběžek odříznut
    assert abs(g.area - 2000) < 30
    # budova v zálivu: zacelení do ní nezasáhne
    blocked = ds.generalize_area(base, 10000, blockers=box(40, 15, 42, 20))
    assert blocked.intersection(box(40, 15, 42, 20)).area < 0.5
    # malý samostatný kus (< 0,5 mm² = 50 m²) se zahodí
    assert ds.generalize_area(box(0, 0, 6, 6), 10000) is None


def test_end_at_edge_accepts_geometry_collection_with_lines():
    from shapely.geometry import GeometryCollection, LineString, box

    area = GeometryCollection([box(0, -5, 10, 5), LineString([(20, 0), (30, 0)])])  # boundary je None
    out = ds.end_at_edge([(-20.0, 0.0), (5.0, 0.0)], area)
    assert len(out) == 1 and abs(out[0][-1][0]) < 1e-6  # končí na hraně plochy


def _drawn_length_in_rect(parts, rect, ref, scale):
    """Délka kreslených čar (m) uvnitř obdélníku ve světových souřadnicích (mapa v 0,001 mm, osa y obrácená)."""
    import re

    from shapely.geometry import LineString, box

    zone = box(*rect)
    total = 0.0
    for part in parts:
        for body in re.findall(r"<coords[^>]*>([^<]*)</coords>", part.objects_xml):
            pts = [
                (ref[0] + int(mx) * scale / 1e6, ref[1] - int(my) * scale / 1e6)
                for mx, my in re.findall(r"(-?\d+) (-?\d+)(?: \d+)?;", body)
            ]
            if len(pts) >= 2:
                total += LineString(pts).intersection(zone).length
    return total


def test_forest_and_mtbo_roads_and_paths_are_cut_by_dtm_parking(tmp_path):
    from app.pipeline import osm_paths as op

    def make(work, with_parking):
        (work / "dtm").mkdir()
        if with_parking:
            (work / "dtm" / "parking.json").write_text(
                json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1000, -970, -985)]]}),
                encoding="utf-8",
            )
        (work / "osm_paths").mkdir()
        ring = [[-1100, -1100], [-1090, -1100], [-1090, -1090], [-1100, -1090], [-1100, -1100]]
        zone = {  # vzdálená OSM plocha: síť zpevněných ploch tak není prázdná
            "type": "Feature",
            "properties": {"source": "osm", "kind": "parking", "oom_code": "501"},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }
        (work / "osm_paths" / "features.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": [zone]}), encoding="utf-8"
        )

        def line(hw, a, b):
            return {
                "type": "Feature",
                "properties": {"highway": hw},
                "geometry": {"type": "LineString", "coordinates": [a, b]},
            }

        feats = [
            line("road_3", [-1020, -992], [-950, -992]),
            line("track", [-1020, -999], [-950, -999]),
        ]
        (work / "osm_paths" / "paths_mixed.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
        )

    inside = (-1000, -1000, -970, -985)
    ref = (-985.0, -990.0)
    for preset in ("forest_10000", "mtbo_10000"):
        kw = dict(preset_id=preset, scale=10000, ref_x=ref[0], ref_y=ref[1], grivation_deg=0.0)
        free = tmp_path / preset / "free"
        free.mkdir(parents=True)
        make(free, False)
        base = op.build_osm_path_parts(free, **kw)
        assert _drawn_length_in_rect(base, inside, ref, 10000) > 50  # bez parkoviště se kreslí celé
        op._PAVED_NETWORK_CACHE.clear()
        cut = tmp_path / preset / "cut"
        cut.mkdir(parents=True)
        make(cut, True)
        parts = op.build_osm_path_parts(cut, **kw)
        assert _drawn_length_in_rect(parts, inside, ref, 10000) < 1.0
        assert sum(p.count for p in parts) >= 2  # napojení na hranu parkoviště zůstává
        op._PAVED_NETWORK_CACHE.clear()


def test_road_corridor_min_drawn_drops_narrower_roads_and_keeps_wider():
    from shapely.geometry import box
    from shapely.ops import unary_union

    wide, narrow = box(0, 40, 100, 52), box(100, 43, 130, 50)  # 12 m široká silnice + navazující úsek 7 m
    roads = unary_union([wide, narrow])
    kw = dict(scale=10000)
    base = ds.road_corridor(roads, None, **kw)
    assert base is not None and base.intersection(narrow).area > 0.9 * narrow.area  # navazující úsek >= 5 m zůstane
    cor = ds.road_corridor(roads, None, min_drawn_m=9.9, **kw)
    assert cor is not None
    assert cor.intersection(narrow).area < 0.05 * narrow.area  # užší než kresba silnice 502 zmizí
    assert cor.intersection(wide).area > 0.95 * wide.area


def test_road_corridor_min_drawn_fills_short_neck_along_axis():
    from shapely.geometry import LineString, box
    from shapely.ops import unary_union

    # 12 m široká silnice se zúžením na 7 m v délce 10 m: bez osy se rozpadne na dva kusy, s osou zůstane celá
    roads = unary_union([box(0, 0, 95, 12), box(95, 2.5, 105, 9.5), box(105, 0, 200, 12)])
    kw = dict(scale=10000, min_drawn_m=9.9)
    split = ds.road_corridor(roads, None, **kw)
    assert split is not None and split.geom_type == "MultiPolygon"
    axis = LineString([(-20, 6), (220, 6)])
    cor = ds.road_corridor(roads, None, axes=axis, **kw)
    assert cor is not None and cor.geom_type == "Polygon"
    neck = cor.intersection(box(97, -5, 103, 20))
    assert neck.bounds[3] - neck.bounds[1] >= 9.8  # zúžení má šířku kresby silnice
    assert cor.bounds[0] >= -0.1 and cor.bounds[2] <= 200.1  # osa koridor neprodlouží


def test_road_corridor_min_drawn_drops_roundabout_ring_but_keeps_wide_straight_road():
    from shapely.geometry import Point, box

    kw = dict(scale=10000, min_drawn_m=9.9)
    for inner in (12, 10.5):  # jízdní pás 8 m a 9,5 m < 9,9 m: prstenec se nesmí rozpadnout na tříšť
        ring = Point(0, 0).buffer(20).difference(Point(0, 0).buffer(inner))
        assert ds.road_corridor(ring, None, **kw) is None
    road = box(0, 0, 200, 12)
    cor = ds.road_corridor(road, None, **kw)
    assert cor is not None and cor.area > 0.95 * road.area


def test_corridor_without_sidewalks_does_not_widen(tmp_path, monkeypatch):
    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "roads.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1006, -900, -994)]]}), encoding="utf-8"
    )
    (work / "dtm" / "sidewalks.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -993, -900, -990)]]}), encoding="utf-8"
    )
    (work / "osm_paths").mkdir()
    road = {"type": "Feature", "properties": {"highway": "road_3"}, "geometry": {"type": "LineString", "coordinates": [[-1000, -1000], [-900, -1000]]}}
    (work / "osm_paths" / "paths_mixed.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [road]}), encoding="utf-8")
    monkeypatch.setattr(di, "CORRIDOR_WITH_SIDEWALKS", False)
    plain = di.mtbo_corridor_geometry(work, 10000)
    assert plain is not None and plain.bounds[3] < -993.9  # chodník (od -993) není součástí
    monkeypatch.setattr(di, "CORRIDOR_WITH_SIDEWALKS", True)
    with_sw = di.mtbo_corridor_geometry(work, 10000)
    assert with_sw is not None and with_sw.bounds[3] > -991.5 and with_sw.area > plain.area


def test_corridor_edge_omitted_where_drawn_road_meets_corridor(tmp_path):
    from shapely.geometry import Point

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "roads.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-950, -1006, -850, -994)]]}), encoding="utf-8"
    )
    (work / "osm_paths").mkdir()
    road = {"type": "Feature", "properties": {"highway": "road_3"}, "geometry": {"type": "LineString", "coordinates": [[-1000, -1000], [-800, -1000]]}}
    (work / "osm_paths" / "paths_mixed.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [road]}), encoding="utf-8")
    kw = dict(preset_id="mtbo_10000", scale=10000, ref_x=-900.0, ref_y=-1000.0, grivation_deg=0.0)
    corridor = di.mtbo_corridor_geometry(work, 10000)
    assert corridor is not None
    omit = di.corridor_junction_omit(work, corridor, "mtbo_10000", 10000)
    assert omit is not None
    r = 9.9 / 2 + di.JUNCTION_EXTRA_M
    for x in (-950, -850):
        assert omit.contains(Point(x, -1000)) and omit.contains(Point(x, -1000 + r - 0.1))
    parts = di.build_mtbo_corridor_parts(work, **kw)
    assert [p.count for p in parts] == [1, parts[1].count]  # výplň celá, obrys zvlášť
    full = di._geometry_part(corridor, name="x", code="501.0", outline_separate=True, clip_bounds=None, **kw)
    assert parts[1].count >= 1 and full[1].count >= 1
    assert parts[1].objects_xml != full[1].objects_xml  # obrys s mezerou na napojení se liší od celého


def test_corridor_junction_omit_from_drawn_road_ends():
    from shapely.geometry import Point, box

    corridor = box(0, 0, 100, 12)
    ends = [(100.3, 6.0, 4.0), (50.0, 40.0, 4.0), (30.0, 12.2, 2.0)]  # u hrany, daleko, u hrany (užší čára)
    omit = di.corridor_junction_omit(None, corridor, "mtbo_10000", 10000, road_ends=ends)
    assert omit is not None
    assert omit.contains(Point(100, 6 + 4.0)) and not omit.contains(Point(50, 35))
    assert omit.contains(Point(30, 12 + 2.0)) and not omit.contains(Point(30, 12 - 2.5))
    assert di.corridor_junction_omit(None, corridor, "mtbo_10000", 10000, road_ends=[(50, 40, 4.0)]) is None


def test_drawn_path_ends_roundtrip():
    from app.pipeline.oom_coords import projected_to_map_coord
    from app.pipeline.oom_import import OomObjectPart
    from app.pipeline.oom_symbols import symbol_set_path
    from app.pipeline.path_clearance import _symbol_info, drawn_path_ends

    kw = dict(preset_id="forest_10000", scale=10000, ref_x=-900.0, ref_y=-1000.0, grivation_deg=3.0)
    info = _symbol_info(symbol_set_path("forest_10000", 10000))
    sym = next(i for i, (major, kind) in info.items() if major == 502 and kind == "line")
    a = projected_to_map_coord(-950.0, -1000.0, ref_x=-900.0, ref_y=-1000.0, scale=10000, grivation_deg=3.0)
    b = projected_to_map_coord(-850.0, -990.0, ref_x=-900.0, ref_y=-1000.0, scale=10000, grivation_deg=3.0)
    xml = f'<object type="1" symbol="{sym}"><coords count="2">{a[0]} {a[1]};{b[0]} {b[1]};</coords></object>'
    ends = drawn_path_ends([OomObjectPart("x", xml, 1)], majors=frozenset({502}), **kw)
    assert len(ends) == 2
    assert abs(ends[0][0] + 950) < 0.05 and abs(ends[0][1] + 1000) < 0.05
    assert abs(ends[1][0] + 850) < 0.05 and abs(ends[1][1] + 990) < 0.05
    assert ends[0][2] > 1.0  # půlka šířky 502 v metrech
    assert drawn_path_ends([OomObjectPart("x", xml, 1)], majors=frozenset({815}), **kw) == []


def test_corridor_adjacent_paved_ignores_slivers(tmp_path, monkeypatch):
    from shapely.geometry import Point, box
    from shapely.ops import unary_union

    from app.pipeline import osm_paths as op

    corridor = box(0, 0, 100, 10)
    parking = box(20, 10, 50, 40)  # přiléhá k hraně koridoru
    sliver = box(60, 0, 90, 10.4)  # plocha přes silnici: po ořezu koridorem jen pruh 0,4 m
    monkeypatch.setattr(di, "dtm_parking_geometry", lambda *a, **k: unary_union([parking, sliver]))
    monkeypatch.setattr(op, "paved_network_geometry", lambda *a, **k: box(0, -20, 100, -10.2))  # OSM síť se nepočítá
    adj = di._corridor_adjacent_paved(tmp_path, corridor, "mtbo_10000", 10000, "mixed")
    assert adj is not None
    assert adj.buffer(0.1).contains(Point(35, 10)) and adj.distance(Point(75, 10)) > 1.0
    assert adj.distance(Point(50, -10)) > 1.0


def test_edge_omit_drops_short_stub_between_two_omits():
    from shapely.geometry import Point, box

    kw = dict(preset_id="mtbo_10000", scale=10000, ref_x=-900.0, ref_y=-1000.0, grivation_deg=0.0)
    rect = box(-950, -1006, -850, -994)

    def edges(omit):
        parts = di._geometry_part(rect, name="x", code="501.0", outline_separate=True, clip_bounds=None, edge_omit=omit, **kw)
        return parts[1].count

    a, b, far = Point(-920, -1006).buffer(2.0), Point(-913, -1006).buffer(2.0), Point(-870, -1006).buffer(2.0)  # mezi a, b 3 m (0,3 mm)
    base = edges(a)  # obrys uzavřeného obdélníku přerušený jedním kruhem
    assert edges(a.union(b)) == base  # pahýl 5 m mezi kruhy se zahodí, zbytek obrysu zůstane
    assert edges(a.union(far)) == base + 1  # daleké přerušení: oba kusy zůstanou
    assert edges(a.union(b).union(far)) == base + 1
    # pahýl s jedním koncem u přerušení (kruh blízko rohu) se také zahodí
    corner = Point(-950, -1006).buffer(2.0)
    assert edges(corner.union(Point(-945, -1006).buffer(0.6))) <= edges(corner)


def test_trim_along_corridor():
    from shapely.geometry import box

    from app.pipeline.dtm_sidewalks import trim_along_corridor

    corridor = box(0, 0, 100, 10)
    # souběžně 2 m od hrany, 100 m: zmizí
    assert trim_along_corridor([(0, 12), (100, 12)], corridor, 4.0) == []
    # kolmé napojení: beze změny
    perp = [(50, 10), (50, 40)]
    assert trim_along_corridor(perp, corridor, 4.0) == [perp]
    # 40 m podél hrany a odbočka pryč: zbude odbočka napojená na hranu
    line = [(0, 10), (0, 12), (40, 12), (60, 40)]
    out = trim_along_corridor(line, corridor, 4.0)
    assert len(out) == 1
    piece = out[0]
    assert abs(piece[0][1] - 10.0) < 1e-6  # začátek leží na hraně koridoru
    assert piece[-1] == (60.0, 40.0)


def test_trim_along_corridor_precomputed_band_and_boundary_give_same_result():
    from shapely.geometry import box

    corridor = box(0, 0, 100, 10)
    band = corridor.buffer(2.0)
    for line in (
        [(0, 12), (100, 12)],
        [(50, 10), (50, 40)],
        [(0, 10), (0, 12), (40, 12), (60, 40)],
        [(0.0, 100.0), (50.0, 100.0)],
    ):
        assert ds.trim_along_corridor(line, corridor, 4.0, band=band, boundary=corridor.boundary) == ds.trim_along_corridor(
            line, corridor, 4.0
        )


def test_trim_along_corridor_shallow_end_is_cut_and_snapped_to_edge():
    import math

    from shapely.geometry import LineString, box

    corridor = box(0, 0, 100, 10)
    w = 4.0
    # konec dobíhá k hraně pod 15° (poslední úsek v pásu je dlouhý, konec musí skončit na hraně)
    t = math.tan(math.radians(15))
    line = [(10.0, 40.0), (10.0, 12.0), (10.0 + 2.0 / t, 12.0), (10.0 + 2.0 / t + 2.0 / t, 10.0)]
    out = ds.trim_along_corridor(line, corridor, w)
    assert len(out) == 1
    assert abs(corridor.boundary.distance(__import__("shapely.geometry").geometry.Point(out[0][-1]))) < 1e-6
    in_band = LineString(out[0]).intersection(corridor.buffer(w / 2)).length
    old_in_band = LineString(line).intersection(corridor.buffer(w / 2)).length
    assert in_band < old_in_band


def test_trim_along_corridor_perpendicular_end_stays_on_edge():
    from shapely.geometry import LineString, Point, box

    corridor = box(0, 0, 100, 10)
    perp = [(50.0, 10.0), (50.0, 40.0)]
    out = ds.trim_along_corridor(perp, corridor, 4.0)
    assert len(out) == 1
    assert corridor.boundary.distance(Point(out[0][0])) < 1e-6
    assert abs(LineString(out[0]).length - 30.0) < 0.5


def test_trim_along_corridor_drops_stub_near_edge():
    from shapely.geometry import box

    corridor = box(0, 0, 100, 10)
    assert ds.trim_along_corridor([(20.0, 11.0), (26.0, 11.0)], corridor, 4.0) == []


def test_trim_along_corridor_line_far_from_corridor_is_unchanged():
    from shapely.geometry import box

    coords = [(0.0, 100.0), (50.0, 100.0)]
    assert ds.trim_along_corridor(coords, box(0, 0, 50, 10), 8.7) == [coords]


def test_corridor_edge_merged_with_adjacent_parking_reaches_corner(tmp_path, monkeypatch):
    from shapely.geometry import Point, box

    corridor, parking = box(0, 0, 100, 10), box(20, 10, 50, 40)
    seen = {}
    monkeypatch.setattr(di, "dtm_area_preset", lambda p: True)
    monkeypatch.setattr(di, "corridor_geometry_for", lambda *a, **k: corridor)
    monkeypatch.setattr(di, "_corridor_adjacent_paved", lambda *a, **k: parking)
    monkeypatch.setattr(di, "corridor_junction_omit", lambda *a, **k: None)
    monkeypatch.setattr(di, "_geometry_part", lambda geom, **k: seen.update(k) or [])
    di.build_mtbo_corridor_parts(tmp_path, preset_id="mtbo_10000", scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    lines = seen["edge_lines"]
    assert lines.distance(Point(35, 10)) > 1.0  # společná hranice bez okraje
    assert lines.distance(Point(10, 10)) < 0.01 and lines.distance(Point(60, 10)) < 0.01  # zbytek hrany koridoru
    # u rohu obrys koridoru pokračuje po hraně parkoviště dál, než parkoviště svůj okraj vynechá (0,5 m)
    assert lines.distance(Point(20, 10 + di.CORRIDOR_SHARED_EDGE_M)) < 0.01
    assert lines.distance(Point(50, 10 + di.CORRIDOR_SHARED_EDGE_M)) < 0.01


def test_classified_polygons_mixed_uses_osm_roads_dropped_for_zabaged(tmp_path):
    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "sidewalks.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1007, -800, -993)]]}), encoding="utf-8"
    )
    (work / "dtm" / "roads.json").write_text(json.dumps({"polygons": []}), encoding="utf-8")
    (work / "osm_paths").mkdir()
    road = {"type": "Feature", "properties": {"highway": "road_3"}, "geometry": {"type": "LineString", "coordinates": [[-990, -1000], [-810, -1000]]}}
    empty = {"type": "FeatureCollection", "features": []}
    (work / "osm_paths" / "paths_mixed.geojson").write_text(json.dumps(empty), encoding="utf-8")  # ulici kreslí ZABAGED
    (work / "osm_paths" / "paths_osm.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [road]}), encoding="utf-8")
    sidewalks, roads = di.classified_polygons(work, "mixed")
    assert sidewalks == [] and len(roads) == 1  # chodník z DTM s OSM ulicí uvnitř je silnice i v mixed


def test_dtm_disabled_hides_job_dtm_data(tmp_path):
    (tmp_path / "dtm").mkdir()
    (tmp_path / "dtm" / "roads.json").write_text(json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(0, 0, 100, 10)]]}), encoding="utf-8")
    assert di.dtm_owns_roads(tmp_path)
    with di.dtm_disabled():
        assert di.dtm_is_disabled() and not di.dtm_owns_roads(tmp_path)
        assert di.road_geometry(tmp_path) is None
    assert di.dtm_owns_roads(tmp_path) and not di.dtm_is_disabled()
    with di.dtm_disabled(False):
        assert di.dtm_owns_roads(tmp_path)


def test_dropped_sidewalk_shared_with_bikes_only_when_overlapping_road():
    from app.pipeline import osm_paths as op

    from shapely.geometry import LineString, box

    road_zone, bike_road_zone = box(0, -8, 200, 8), box(0, -5, 200, 5)  # pás s mezerou / jen zásah do kresby
    near = {"type": "Feature", "properties": {"highway": "footway"}, "geometry": {"type": "LineString", "coordinates": [[0, 4], [200, 4]]}}
    gap = {"type": "Feature", "properties": {"highway": "footway"}, "geometry": {"type": "LineString", "coordinates": [[0, -7], [200, -7]]}}
    bike_zone = LineString([(0, 4), (200, 4)]).union(LineString([(0, -7), (200, -7)])).buffer(1.0)
    _geom, owners = op.dropped_sidewalk_geometry([near, gap], road_zone, bike_zone, op.SIDEWALK_DEDUP_BASES, bike_road_zone)
    assert owners == {0}  # smíšená stezka v mezeře zůstává, zasahující do kresby silnice pryč
    _geom, owners = op.dropped_sidewalk_geometry([near, gap], road_zone, bike_zone, op.SIDEWALK_DEDUP_BASES)
    assert owners == set()  # bez užšího pásu se smíšená stezka nezahazuje
    _geom, owners = op.dropped_sidewalk_geometry([near, gap], road_zone, None, op.SIDEWALK_DEDUP_BASES)
    assert owners == {0, 1}


def test_corridor_variant_e_keeps_only_continuously_wide_road_or_parking(monkeypatch):
    from shapely.geometry import box
    from shapely.ops import unary_union

    monkeypatch.setattr(ds, "CORRIDOR_SEEDS_E", True)
    kw = dict(scale=10000, min_drawn_m=9.9)
    short_wide = unary_union([box(0, 0, 60, 12), box(60, 2, 300, 10)])  # 12 m jen na 60 m, jinak 8 m
    assert ds.road_corridor(short_wide, None, **kw) is None  # kreslí se čarou
    long_wide = unary_union([box(0, 0, 150, 13), box(150, 2, 300, 10)])
    cor = ds.road_corridor(long_wide, None, **kw)
    assert cor is not None and cor.area > 150 * 12
    parking = box(10, 12, 50, 30)  # parkoviště u krátkého širokého kusu
    assert ds.road_corridor(short_wide, None, parking=parking, **kw) is not None


def test_dtm_road_area_with_only_unpaved_track_is_not_road(tmp_path):
    """way/256370567: štěrková cesta (track) v DTM ploše komunikace → plochu nekreslit, kreslí čára OSM."""
    work = tmp_path
    (work / "dtm").mkdir()
    gravel, street = _rect(0, -4, 300, 4), _rect(0, 96, 300, 104)
    (work / "dtm" / "roads.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in poly] for poly in (gravel, street)]}), encoding="utf-8"
    )
    (work / "osm_paths").mkdir()

    def line(hw, y):
        return {"type": "Feature", "properties": {"highway": hw}, "geometry": {"type": "LineString", "coordinates": [[-5, y], [305, y]]}}

    paths = {"type": "FeatureCollection", "features": [line("track", 0), line("road_3", 100), line("track", 101)]}
    (work / "osm_paths" / "paths_osm.geojson").write_text(json.dumps(paths), encoding="utf-8")
    _sidewalks, roads = di.classified_polygons(work, "mixed")
    assert len(roads) == 1 and roads[0][0][0][1] == 96  # zůstane jen ulice (silnice v ní má přednost)
    cover = di.dtm_cover_geometry(work, 0.0)
    assert cover is not None and cover.bounds[1] >= 96
