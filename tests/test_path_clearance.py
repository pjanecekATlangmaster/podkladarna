import json

import pytest

from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import OomObjectPart, _area_object_with_holes, _point_object
from app.pipeline.oom_symbol_map import symbol_index_for_code, symbol_width_mm
from app.pipeline.path_clearance import _parse_coords, _COORDS_RE, _OBJECT_RE, displace_objects_from_paths

PRESET, SCALE = "forest_10000", 10000
KW = dict(preset_id=PRESET, scale=SCALE, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)


def _to_map(pts):
    return [projected_to_map_coord(x, y, ref_x=0.0, ref_y=0.0, scale=SCALE, grivation_deg=0.0) for x, y in pts]


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def _min_dist_m(xml):
    """Nejmenší vzdálenost bodů objektu od osy silnice y = 0 (m)."""
    pts = _parse_coords(_COORDS_RE.search(xml).group(1))
    return min(abs(y) for _x, y, _f in pts) / 100.0  # 1:10 000 → 100 µm na metr


def _work(tmp_path):
    (tmp_path / "osm_paths").mkdir()
    road = {"type": "Feature", "properties": {"highway": "road_3"}, "geometry": {"type": "LineString", "coordinates": [[-100, 0], [100, 0]]}}
    (tmp_path / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [road]}), encoding="utf-8"
    )
    return tmp_path


def _objects(parts):
    return [m.group(0) for p in parts for m in _OBJECT_RE.finditer(p.objects_xml)]


def test_building_olive_and_point_move_away_from_road_forest_stays(tmp_path):
    work = _work(tmp_path)
    road_half = symbol_width_mm(PRESET, SCALE, "502") / 2 * 10  # m
    building = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "521"), [_to_map(_rect(-30, 2, -10, 20))])
    tiny = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "521"), [_to_map(_rect(10, 2, 14, 5))])
    olive = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "520"), [_to_map(_rect(-60, -40, -35, -1))])
    forest = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "406"), [_to_map(_rect(30, 1, 60, 30))])
    px, py = _to_map([(0, 2)])[0]
    point = _point_object(symbol_index_for_code(PRESET, SCALE, "531"), px, py)
    parts = [OomObjectPart(name="x", objects_xml="\n".join([building, tiny, olive, forest, point]), count=5)]
    out = _objects(displace_objects_from_paths(parts, work, **KW))
    b, t, o, f, p = out
    assert _min_dist_m(b) > road_half + 1.9  # budova: černá kresba → mezera 0,2 mm (2 m)
    assert road_half + 0.4 < _min_dist_m(o) < road_half + 1.5  # oliva: jen minimální odstup
    assert _min_dist_m(p) > road_half + 4.0  # bod: poloměr 0,3 mm + mezera
    assert f == forest.strip()  # les se nemění
    assert _min_dist_m(t) > road_half + 1.9 and _area(t) == _area(tiny)  # budova se posune celá, tvar zůstane
    assert _area(b) == _area(building)
    assert _polygon_of(o).is_valid and _area(o) < _area(olive)  # oliva se ořízne o pás cesty


def _polygon_of(xml):
    from shapely.geometry import Polygon

    pts = _parse_coords(_COORDS_RE.search(xml).group(1))
    return Polygon([(x, y) for x, y, _f in pts])


def _area(xml):
    return round(_polygon_of(xml).area)


def test_building_crossed_by_path_and_wall_pushed_onto_other_path_stay(tmp_path):
    work = _work(tmp_path)
    # druhá silnice rovnoběžně 9 m severně: zeď mezi nimi by se odsunula k ní
    other = {"type": "Feature", "properties": {"highway": "road_3"}, "geometry": {"type": "LineString", "coordinates": [[-100, 9], [100, 9]]}}
    road = json.loads((work / "osm_paths" / "paths_mixed.geojson").read_text(encoding="utf-8"))
    road["features"].append(other)
    (work / "osm_paths" / "paths_mixed.geojson").write_text(json.dumps(road), encoding="utf-8")
    from app.pipeline.oom_import import _path_object

    wall = _path_object(symbol_index_for_code(PRESET, SCALE, "513"), _to_map([(-40, 3), (40, 3)]))
    crossed = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "521"), [_to_map(_rect(50, -5, 60, 20))])
    parts = [OomObjectPart(name="x", objects_xml="\n".join([wall, crossed]), count=2)]
    w, c = _objects(displace_objects_from_paths(parts, work, **KW))
    assert w == wall.strip() and c == crossed.strip()


def test_sprint_and_map_without_paths_are_unchanged(tmp_path):
    building = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "521"), [_to_map(_rect(-30, 2, -10, 20))])
    parts = [OomObjectPart(name="x", objects_xml=building, count=1)]
    assert displace_objects_from_paths(parts, tmp_path, **KW) == parts  # bez cest
    work = _work(tmp_path)
    assert displace_objects_from_paths(parts, work, **{**KW, "preset_id": "sprint_4000", "scale": 4000}) == parts


def test_paths_are_taken_from_drawn_map_objects_without_geojson(tmp_path):
    from app.pipeline.oom_import import _path_object

    # cesta jen jako objekt mapy (např. ZABAGED), žádný geojson s cestami
    road = _path_object(symbol_index_for_code(PRESET, SCALE, "502"), _to_map([(-100, 0), (100, 0)]))
    building = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "521"), [_to_map(_rect(-30, 2, -10, 20))])
    parts = [OomObjectPart(name="x", objects_xml="\n".join([road, building]), count=2)]
    r, b = _objects(displace_objects_from_paths(parts, tmp_path, **KW))
    assert r == road.strip()
    road_half = symbol_width_mm(PRESET, SCALE, "502") / 2 * 10
    assert _min_dist_m(b) > road_half + 1.9


def test_closed_osm_fence_is_filled_with_olive_in_mtbo(tmp_path):
    from app.pipeline import dtm_import as di

    ring = [[0, 0], [30, 0], [30, 25], [0, 25], [0, 0]]
    fence = {"type": "Feature", "properties": {"kind": "fence"}, "geometry": {"type": "LineString", "coordinates": ring}}
    out = di.join_and_orient_osm_fences(tmp_path, [fence], preset_id="mtbo_10000", scale=10000)
    kinds = sorted(f["properties"]["kind"] for f in out)
    assert kinds == ["fence", "garden"]


def test_self_intersecting_olive_is_repaired_even_far_from_paths(tmp_path):
    from shapely.geometry import Polygon

    work = _work(tmp_path)
    bowtie = [(0, 200), (20, 220), (20, 200), (0, 220), (0, 200)]  # daleko od silnice
    olive = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "520"), [_to_map(bowtie)])
    parts = [OomObjectPart(name="x", objects_xml=olive, count=1)]
    out = _objects(displace_objects_from_paths(parts, work, **KW))
    assert out and all(_polygon_of(o).is_valid for o in out)
    assert not Polygon([(x, y) for x, y, _f in _parse_coords(_COORDS_RE.search(olive).group(1))]).is_valid


def test_tiny_self_intersecting_olive_is_repaired_not_kept(tmp_path):
    work = _work(tmp_path)
    bowtie = [(0, 200), (3, 203), (3, 200), (0, 203), (0, 200)]  # užší než minimum (4 m v 1:10 000)
    olive = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "520"), [_to_map(bowtie)])
    out = _objects(displace_objects_from_paths([OomObjectPart(name="x", objects_xml=olive, count=1)], work, **KW))
    assert out and all(_polygon_of(o).is_valid for o in out)


def test_olive_lying_under_road_is_dropped(tmp_path):
    work = _work(tmp_path)
    strip = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "520"), [_to_map(_rect(-20, -1, 20, 1.5))])
    parts = [OomObjectPart(name="x", objects_xml=strip, count=1)]
    out = displace_objects_from_paths(parts, work, **KW)
    assert out == []


def test_osm_fence_gap_is_connected_before_olive_fill_in_forest(tmp_path):
    from app.pipeline import dtm_import as di

    # plot kolem zahrady s mezerou 4 m (konce na sebe míří) – uzavře ho až spojka, pak se vyplní olivou
    gap = [[10, 0], [30, 0], [30, 25], [0, 25], [0, 0], [6, 0]]
    fence = {"type": "Feature", "properties": {"kind": "fence"}, "geometry": {"type": "LineString", "coordinates": gap}}
    garden = {
        "type": "Feature",
        "properties": {"kind": "garden"},
        "geometry": {"type": "Polygon", "coordinates": [[[1, 1], [29, 1], [29, 24], [1, 24], [1, 1]]]},
    }
    out = di.join_and_orient_osm_fences(tmp_path, [fence, garden], preset_id="forest_10000", scale=10000)
    fills = [f for f in out if (f["properties"] or {}).get("fence_fill")]
    fences = [f for f in out if f["properties"]["kind"] == "fence"]
    assert len(fills) == 1 and len(fences) == 1
    assert len(fences[0]["geometry"]["coordinates"]) >= 6  # spojka je součástí čáry plotu


def test_merge_fill_rings_adds_only_new_enclosures():
    from app.pipeline import dtm_import as di

    a = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    same = [(0, 0), (10, 0), (10, 9), (0, 9), (0, 0)]
    other = [(20, 0), (30, 0), (30, 10), (20, 10), (20, 0)]
    assert di.merge_fill_rings([a], [same, other]) == [a, other]
    assert di.merge_fill_rings([], [other]) == [other]


def test_paved_fill_inside_olive_is_not_drawn_olive_stays_whole():
    from app.pipeline.path_clearance import paved_fill_outside_olive

    olive = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "520"), [_to_map(_rect(0, 0, 60, 60))])
    inside = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "501.1"), [_to_map(_rect(20, 20, 40, 40))])
    across = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "501.1"), [_to_map(_rect(45, 30, 75, 40))])
    forest = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "406"), [_to_map(_rect(20, 20, 40, 40))])
    parts = [OomObjectPart(name="x", objects_xml="\n".join([olive, inside, across, forest]), count=4)]
    out = _objects(paved_fill_outside_olive(parts, preset_id=PRESET, scale=SCALE))
    assert len(out) == 3 and out[0] == olive.strip() and out[2] == forest.strip()  # oliva i jiné plochy beze změny
    assert abs(_area(out[1]) - _area(across) / 2) < _area(across) * 0.01  # výplň jen mimo olivu
    sprint = paved_fill_outside_olive(parts, preset_id="sprint_4000", scale=4000)
    assert sprint == parts


def test_paved_fill_stays_visible_in_large_olive_area():
    from app.pipeline.path_clearance import paved_fill_outside_olive

    big = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "520"), [_to_map(_rect(0, 0, 50, 100))])
    big2 = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "520"), [_to_map(_rect(50, 0, 100, 100))])
    inside = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "501.1"), [_to_map(_rect(20, 20, 40, 40))])
    parts = [OomObjectPart(name="x", objects_xml="\n".join([big, big2, inside]), count=3)]
    # dva sousední kusy olivy = jeden areál 1 ha → výplň zůstane
    assert paved_fill_outside_olive(parts, preset_id=PRESET, scale=SCALE) == parts


SPRINT_KW = dict(preset_id="sprint_2m", scale=4000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)


def _sprint_map(tmp_path, covered):
    """Budova 20 × 20 m (0–20), dvě cesty 506 napříč ní (y = 5 jen krytá, y = 15 obyčejná) a jedna podél obrysu."""
    from app.pipeline.oom_import import _path_object

    def to_map(pts):
        return [projected_to_map_coord(x, y, ref_x=0.0, ref_y=0.0, scale=4000, grivation_deg=0.0) for x, y in pts]

    (tmp_path / "osm_paths").mkdir()
    feats = [{"type": "Feature", "properties": {"covered": True}, "geometry": {"type": "LineString", "coordinates": [[-10, 5], [30, 5]]}}] if covered else []
    (tmp_path / "osm_paths" / "covered_passages.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    sym = symbol_index_for_code("sprint_2m", 4000, "506")
    building = _area_object_with_holes(symbol_index_for_code("sprint_2m", 4000, "521"), [to_map(_rect(0, 0, 20, 20))])
    lines = [_path_object(sym, to_map([(-10, y), (30, y)])) for y in (5, 15)] + [_path_object(sym, to_map([(-10, 0.4), (30, 0.4)]))]
    return [OomObjectPart(name="x", objects_xml="\n".join([building, *lines]), count=4)]


def _line_lengths_m(parts, sym_code):
    from shapely.geometry import LineString

    sym = symbol_index_for_code("sprint_2m", 4000, sym_code)
    out = []
    for p in parts:
        for m in _OBJECT_RE.finditer(p.objects_xml):
            if int(m.group(3)) == sym and m.group(2) == "1":
                pts = _parse_coords(_COORDS_RE.search(m.group(4)).group(1))
                out.append(LineString([(x, y) for x, y, _f in pts]).length / 250.0)  # 1:4000 → 250 µm na metr
    return sorted(out)


def test_sprint_covered_passage_becomes_roof_and_cuts_building(tmp_path):
    from app.pipeline.path_clearance import sprint_building_passages

    parts = _sprint_map(tmp_path, covered=True)
    out = sprint_building_passages(parts, tmp_path, **SPRINT_KW)
    # Obyčejná cesta y = 15 uvnitř budovy zmizí (zbydou dvě poloviny venku), krytá y = 5 také; podél obrysu (0,4 m) zůstane celá.
    assert _line_lengths_m(out, "506") == [10.0, 10.0, 11.0, 11.0, 40.0]  # krytá: celý průchod; obyčejná: 1 m od obrysu zůstává
    roof_sym = symbol_index_for_code("sprint_2m", 4000, "522")
    roofs = [m for p in out for m in _OBJECT_RE.finditer(p.objects_xml) if int(m.group(3)) == roof_sym]
    assert len(roofs) == 1
    roof = _polygon_of(roofs[0].group(0))
    assert 20 <= roof.area / 250.0**2 <= 60  # 20 m dlouhý pás široký aspoň 2 m
    b = [m.group(0) for p in out for m in _OBJECT_RE.finditer(p.objects_xml) if int(m.group(3)) == symbol_index_for_code("sprint_2m", 4000, "521")]
    assert len(b) == 2  # budova je průchodem rozříznutá na dva kusy
    assert sum(_area(x) for x in b) + roof.area == pytest.approx(400 * 250.0**2, rel=0.01)


def test_sprint_without_covered_only_cuts_lines_and_non_sprint_unchanged(tmp_path):
    from app.pipeline.path_clearance import sprint_building_passages

    parts = _sprint_map(tmp_path, covered=False)
    out = sprint_building_passages(parts, tmp_path, **SPRINT_KW)
    assert len(_line_lengths_m(out, "506")) == 5  # obě cesty rozpůlené + obrysová
    assert not any(int(m.group(3)) == symbol_index_for_code("sprint_2m", 4000, "522") for p in out for m in _OBJECT_RE.finditer(p.objects_xml))
    assert sprint_building_passages(parts, tmp_path, **dict(SPRINT_KW, preset_id="forest_10000", scale=10000)) is parts


# Srázy (104 / 201 / 202) u cest v lese a MTBO (Petr 2026-10-26, Barrandov, parkoviště u tramvaje):
# podél cesty se odsunou, přes cestu / po ploše 501 se vynechají, drobky < 0,5 mm pryč.
@pytest.mark.parametrize(
    "code, coords, expect",
    [
        ("201", [(-40, 5), (40, 5)], "moved"),  # podél silnice 5 m od osy, čárky k silnici: odsun za pás silnice + čárky
        ("201", [(40, 5), (-40, 5)], "moved_line"),  # čárky od silnice: stačí odstup čáry + mezera
        ("201", [(0, -30), (0, 30)], "cut"),  # kolmo přes silnici: uprostřed se vyřízne, zůstanou dva kusy
        ("104", [(-40, 5), (40, 5)], "moved"),
        ("201", [(-40, 1), (40, 1)], "moved"),  # těsně u osy: sráz se radši posune (až 1,5 mm), než zahodí
        ("201", [(-2, 1), (2, 1)], "dropped"),  # drobek pod 0,5 mm v pásu silnice: pryč
        ("201", [(-40, 60), (40, 60)], "same"),  # daleko: beze změny
    ],
)
def test_cliff_moves_off_path_or_is_cut(tmp_path, code, coords, expect):
    from app.pipeline.oom_import import _path_object
    from app.pipeline.path_clearance import CLIFF_TICK_MM, BLACK_GAP_MM

    work = _work(tmp_path)
    cliff = _path_object(symbol_index_for_code(PRESET, SCALE, code), _to_map(coords))
    parts = [OomObjectPart(name="x", objects_xml=cliff, count=1)]
    out = _objects(displace_objects_from_paths(parts, work, **KW))
    road_half = symbol_width_mm(PRESET, SCALE, "502") / 2 * 10
    line_clear = road_half + symbol_width_mm(PRESET, SCALE, code) / 2 * 10 + BLACK_GAP_MM * 10
    clearance = line_clear + CLIFF_TICK_MM * 10
    if expect == "same":
        assert out == [cliff.strip()]
    elif expect == "dropped":
        assert out == []
    elif expect == "moved":
        assert len(out) == 1 and _min_dist_m(out[0]) >= clearance - 0.3
    elif expect == "moved_line":
        assert len(out) == 1 and line_clear - 0.3 <= _min_dist_m(out[0]) < clearance - 0.3
    else:
        assert len(out) == 2
        for o in out:
            assert _min_dist_m(o) >= line_clear - 0.3


def test_cliff_on_paved_area_is_cut(tmp_path):
    # Sráz vedoucí po zpevněné ploše 501.1 (parkoviště) se na ploše vynechá, mimo ni zůstane.
    from app.pipeline.oom_import import _path_object

    work = _work(tmp_path)
    paved = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "501.1"), [_to_map(_rect(-20, 30, 20, 60))])
    cliff = _path_object(symbol_index_for_code(PRESET, SCALE, "201"), _to_map([(-60, 45), (60, 45)]))
    parts = [OomObjectPart(name="x", objects_xml="\n".join([paved, cliff]), count=2)]
    out = _objects(displace_objects_from_paths(parts, work, **KW))
    assert out[0] == paved.strip()
    pieces = out[1:]
    assert len(pieces) == 2
    for o in pieces:
        xs = [x / 100.0 for x, _y, _f in _parse_coords(_COORDS_RE.search(o).group(1))]
        assert all(abs(x) >= 19.9 for x in xs)

