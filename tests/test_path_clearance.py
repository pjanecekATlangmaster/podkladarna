import json

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


def test_building_olive_and_point_move_away_from_road_but_forest_and_tiny_building_stay(tmp_path):
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
    assert t == tiny.strip() and f == forest.strip()  # malá budova by se zmenšila pod minimum; les se nemění


def test_sprint_and_map_without_paths_are_unchanged(tmp_path):
    building = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "521"), [_to_map(_rect(-30, 2, -10, 20))])
    parts = [OomObjectPart(name="x", objects_xml=building, count=1)]
    assert displace_objects_from_paths(parts, tmp_path, **KW) == parts  # bez cest
    work = _work(tmp_path)
    assert displace_objects_from_paths(parts, work, **{**KW, "preset_id": "sprint_4000", "scale": 4000}) == parts
