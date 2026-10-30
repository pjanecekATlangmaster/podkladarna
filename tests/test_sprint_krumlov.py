"""Sprint podle srovnání s ruční mapou (Český Krumlov): slučování budov, bus nádraží, uličky na parkovišti, kupky mimo zástavbu."""

import json

import pytest

from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import OomObjectPart, _area_object_with_holes, _path_object, _point_object
from app.pipeline.oom_symbol_map import symbol_index_for_code
from app.pipeline.path_clearance import (
    _COORDS_RE,
    _OBJECT_RE,
    _parse_coords,
    _rings,
    sprint_knolls_outside_town,
    sprint_merge_buildings,
)

PRESET, SCALE = "sprint_2m", 4000
KW = dict(preset_id=PRESET, scale=SCALE, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
UM_PER_M = 1e6 / SCALE


def _to_map(pts):
    return [projected_to_map_coord(x, y, ref_x=0.0, ref_y=0.0, scale=SCALE, grivation_deg=0.0) for x, y in pts]


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def _objects_of(parts, code, preset=PRESET, scale=SCALE):
    sym = symbol_index_for_code(preset, scale, code)
    return [m for p in parts for m in _OBJECT_RE.finditer(p.objects_xml) if int(m.group(3)) == sym]


def _part(*objs):
    return [OomObjectPart(name="x", objects_xml="\n".join(objs), count=len(objs))]


@pytest.mark.parametrize(
    "rects, path, holes, expected_objects, expected_rings, olive",
    [
        ([_rect(0, 0, 10, 10), _rect(10, 0, 20, 10)], None, [], 1, 1, None),  # společná stěna
        ([_rect(0, 0, 10, 10), _rect(11, 0, 21, 10)], None, [], 1, 1, None),  # mezera 1 m bez cesty
        ([_rect(0, 0, 10, 10), _rect(11, 0, 21, 10)], [(10.5, -5), (10.5, 15)], [], 2, 1, None),  # v mezeře vede cesta
        ([_rect(0, 0, 30, 30)], None, [_rect(10, 10, 16, 16)], 1, 1, None),  # dvůr bez cesty se zaplní
        ([_rect(0, 0, 30, 30)], [(8, 13), (18, 13)], [_rect(10, 10, 16, 16)], 1, 2, None),  # dvůr s cestou zůstane
        ([_rect(0, 0, 30, 30)], [(8, 13), (18, 13)], [_rect(10, 10, 12, 12)], 1, 1, None),  # díra pod 1 mm² zmizí i s cestou
        ([_rect(0, 0, 30, 30)], None, [_rect(10, 10, 16, 16)], 1, 1, _rect(10, 10, 16, 16)),  # zahrada (oliva) v uzavřeném bloku = budova (ISSprOM 521)
    ],
)
def test_sprint_merge_buildings(tmp_path, rects, path, holes, expected_objects, expected_rings, olive):
    sym = symbol_index_for_code(PRESET, SCALE, "521")
    objs = []
    for i, r in enumerate(rects):
        rings = [_to_map(r)] + ([_to_map(h) for h in holes] if i == 0 else [])
        objs.append(_area_object_with_holes(sym, rings))
    if path:
        objs.append(_path_object(symbol_index_for_code(PRESET, SCALE, "506"), _to_map(path)))
    if olive:
        objs.append(_area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "520"), [_to_map(olive)]))
    out = sprint_merge_buildings(_part(*objs), tmp_path, **KW)
    buildings = _objects_of(out, "521")
    assert len(buildings) == expected_objects
    assert max(len(_rings(_parse_coords(_COORDS_RE.search(m.group(4)).group(1)))) for m in buildings) == expected_rings
    if olive:
        assert not _objects_of(out, "520")  # oliva v zalitém dvoře se ořízne pryč


def test_sprint_merge_buildings_forest_unchanged(tmp_path):
    parts = _part(_area_object_with_holes(symbol_index_for_code("forest_10000", 10000, "521"), [_to_map(_rect(0, 0, 10, 10))]))
    assert sprint_merge_buildings(parts, tmp_path, **dict(KW, preset_id="forest_10000", scale=10000)) is parts


@pytest.mark.parametrize(
    "tags, geom, expected",
    [
        ({"amenity": "bus_station"}, "way", ("parking", "501")),
        ({"amenity": "bus_station"}, "node", None),
        ({"amenity": "bus_station", "building": "yes"}, "way", ("building", "521")),
    ],
)
def test_bus_station_area_is_paved(tags, geom, expected):
    from app.pipeline.osm_paths import classify_osm_feature

    assert classify_osm_feature(tags, geom=geom) == expected


@pytest.mark.parametrize("road_y, expected_len_m", [(15, 100.0), (-20, 140.0), (1, 140.0)])
def test_sprint_road_not_drawn_inside_parking(tmp_path, road_y, expected_len_m):
    from shapely.geometry import LineString

    from app.pipeline import osm_paths as op

    (tmp_path / "osm_paths").mkdir()
    road = {"type": "Feature", "properties": {"highway": "road_1"}, "geometry": {"type": "LineString", "coordinates": [[-50, road_y], [90, road_y]]}}
    parking = {"type": "Feature", "properties": {"kind": "parking"}, "geometry": {"type": "Polygon", "coordinates": [_rect(0, 0, 40, 30)]}}
    (tmp_path / "osm_paths" / "paths_mixed.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [road]}), encoding="utf-8")
    (tmp_path / "osm_paths" / "features.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [parking]}), encoding="utf-8")
    parts = op.build_osm_path_parts(tmp_path, **KW)
    length = 0.0
    for p in parts:
        for m in _OBJECT_RE.finditer(p.objects_xml):
            pts = _parse_coords(_COORDS_RE.search(m.group(4)).group(1))
            length += LineString([(x, y) for x, y, _f in pts]).length / UM_PER_M
    assert length == pytest.approx(expected_len_m, abs=3.0)


@pytest.mark.parametrize(
    "preset, scale, point, removed",
    [
        (PRESET, SCALE, (-3, 5), True),  # ulice u domu
        (PRESET, SCALE, (30, 5), False),  # louka 20 m od domu
        (PRESET, SCALE, (5, 5), True),  # uvnitř budovy
        ("sprint_2m", SCALE, (105, 5), True),  # uvnitř olivy (viz plocha níže)
        ("forest_10000", 10000, (-3, 5), False),  # les beze změny
    ],
)
def test_sprint_knolls_only_outside_town(preset, scale, point, removed):
    def sym(code):
        return symbol_index_for_code(preset, scale, code)

    to_map = lambda pts: [projected_to_map_coord(x, y, ref_x=0.0, ref_y=0.0, scale=scale, grivation_deg=0.0) for x, y in pts]  # noqa: E731
    px, py = to_map([point])[0]
    parts = _part(
        _area_object_with_holes(sym("521"), [to_map(_rect(0, 0, 10, 10))]),
        _area_object_with_holes(sym("520"), [to_map(_rect(100, 0, 110, 10))]),
        _point_object(sym("109"), px, py),
    )
    out = sprint_knolls_outside_town(parts, preset_id=preset, scale=scale)
    assert (len([m for p in out for m in _OBJECT_RE.finditer(p.objects_xml) if int(m.group(3)) == sym("109")]) == 0) is removed


def _street_dir(tmp_path, parcel):
    d = tmp_path / "ruian_parcels"
    d.mkdir()
    (d / "komunikace.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [parcel]}}]}),
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.parametrize(
    "parcel, highway, green, expected_area",
    [
        (_rect(0, 0, 40, 10), "residential", None, 320.0),  # ulice mezi budovou a hranicí, OSM residential
        (_rect(0, 0, 40, 10), "road_3", None, 320.0),  # syntetické highway z přípravy OSM
        (_rect(0, 0, 40, 10), "residential", _rect(0, 0, 40, 3), 200.0),  # zelený pás se vyřízne
        (_rect(0, 0, 40, 10), "residential", _rect(0, 0, 40, 1), 320.0),  # úzký pás (koruny nad okrajem) ne
        (_rect(0, 0, 40, 10), "path", None, 0.0),  # jen pěšina = nezpevněná
        (_rect(0, 0, 2.5, 2.5), "residential", None, 0.0),  # kus pod 0,5 mm² mapy
    ],
)
def test_street_paved_geometry(tmp_path, parcel, highway, green, expected_area):
    from shapely.geometry import Polygon

    from app.pipeline.street_parcels import street_paved_geometry

    work = _street_dir(tmp_path, parcel)
    building = Polygon(_rect(0, 8, 40, 10))
    out = street_paved_geometry(work, building, [([(-5.0, 4.0), (45.0, 4.0)], highway)], scale=SCALE, green=Polygon(green) if green else None)
    if expected_area == 0.0:
        assert out is None
        return
    assert out.area == pytest.approx(expected_area, rel=0.05)
    assert out.intersection(building).area < 1.0
    if green and expected_area < 320.0:
        assert out.intersection(Polygon(green)).area < 1.0


def test_fetch_street_parcels_cache_and_error(tmp_path, monkeypatch):
    from app.pipeline import street_parcels
    from app.pipeline.fetch_openzu import FetchError

    bbox = (14.31, 48.81, 14.32, 48.82)
    calls = []

    def boom(url, timeout):
        calls.append(url)
        raise FetchError("síť")

    monkeypatch.setattr(street_parcels, "_http_json", boom)
    logs = []
    assert street_parcels.fetch_street_parcels(tmp_path, bbox, log=logs.append) is None  # chyba sítě
    assert calls and logs and "⚠" in logs[0]
    calls.clear()
    cached = _street_dir(tmp_path, _rect(0, 0, 1, 1)) / "ruian_parcels" / "komunikace.geojson"
    assert street_parcels.fetch_street_parcels(tmp_path, bbox) == cached  # cache = bez HTTP
    assert not calls


def test_sprint_street_parcel_part_under_olive_and_mask(tmp_path, monkeypatch):
    import numpy as np

    from app.pipeline import street_parcels

    work = _street_dir(tmp_path, _rect(0, 0, 40, 10))
    (tmp_path / "osm_paths").mkdir()
    (tmp_path / "osm_paths" / "paths.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"highway": "residential"}, "geometry": {"type": "LineString", "coordinates": [[-5, 5], [45, 5]]}}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(street_parcels, "fetch_street_parcels", lambda *a, **k: work / "ruian_parcels" / "komunikace.geojson")
    monkeypatch.setattr(street_parcels, "_prepare_green_mask", lambda *a, **k: None)
    olive = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "520"), [_to_map(_rect(0, 0, 40, 4))])
    out = street_parcels.sprint_street_parcel_part(_part(olive), work, (14.31, 48.81, 14.32, 48.82), **KW)
    assert out[0].name.startswith("Sprint – ulice z katastru") and len(out) == 2  # pod ostatními
    street = _objects_of(out[:1], "501.1")  # výplň bez obrysu
    assert street and not _objects_of(out[:1], "501")
    # oliva se nepřekrývá: zbývá ~ 40 × 6 m (v µm mapy)
    area = sum(_polygon_um2(m) for m in street) / UM_PER_M**2
    assert area == pytest.approx(240.0, rel=0.1)
    # maska zeleně: obdélník 10×5 px po 0,5 m
    mask = np.zeros((20, 20), dtype=bool)
    mask[5:15, 4:10] = True
    g = street_parcels._mask_polygons(mask, 100.0, 200.0, 0.5)
    assert g.area == pytest.approx(10 * 6 * 0.25) and g.bounds == (102.0, 192.5, 105.0, 197.5)
    # jen pixely u parcel ulic (do 1 px): ulice x 100–102,5 → zeleň x 102–103 (2 px sloupce)
    from shapely.geometry import box

    inside = street_parcels._rasterize_area(box(100.0, 190.0, 102.5, 200.0).buffer(0.5), mask.shape, 100.0, 200.0, 0.5, 0.5)
    g2 = street_parcels._mask_polygons(mask & inside, 100.0, 200.0, 0.5)
    assert g2.bounds[0] == 102.0 and g2.bounds[2] <= 103.5


@pytest.mark.parametrize(
    "part_name, expected_m2",
    [
        ("Silnice (DTM + OSM)", 22 * 10),  # zbývá x 18–40
        ("Mosty (DTM) úr. +1 – lávka", 40 * 10),  # mostovka parcelu pod mostem neořezává
    ],
)
def test_sprint_street_parcel_not_under_dtm_area(tmp_path, monkeypatch, part_name, expected_m2):
    """Plocha z DTM má přednost i s okrajem: parcela se pod ní ani do 3 m kolem ní nekreslí (Wassermannova)."""
    from app.pipeline import street_parcels

    work = _street_dir(tmp_path, _rect(0, 0, 40, 10))
    (tmp_path / "osm_paths").mkdir()
    (tmp_path / "osm_paths" / "paths.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"highway": "residential"}, "geometry": {"type": "LineString", "coordinates": [[-5, 5], [45, 5]]}}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(street_parcels, "fetch_street_parcels", lambda *a, **k: work / "ruian_parcels" / "komunikace.geojson")
    monkeypatch.setattr(street_parcels, "_prepare_green_mask", lambda *a, **k: None)
    street_parcels._GEOM_CACHE = None
    dtm = _area_object_with_holes(symbol_index_for_code(PRESET, SCALE, "501.1"), [_to_map(_rect(0, -2, 15, 12))])
    parts = [OomObjectPart(name=part_name, objects_xml=dtm, count=1)]
    out = street_parcels.sprint_street_parcel_part(parts, work, (14.31, 48.81, 14.32, 48.82), **KW)
    area = sum(_polygon_um2(m) for m in _objects_of(out[:1], "501.1")) / UM_PER_M**2
    assert area == pytest.approx(expected_m2, rel=0.05)


def _polygon_um2(m):
    from app.pipeline.path_clearance import _polygon

    return _polygon(_rings(_parse_coords(_COORDS_RE.search(m.group(4)).group(1)))).area


def test_street_geometry_cache_key_follows_inputs(tmp_path):
    """Plocha ulic se pro čistou mapu převezme jen při stejných vstupech (mtime parcel / masky / OSM)."""
    import os

    from app.pipeline import street_parcels

    d = tmp_path / street_parcels.CACHE_SUBDIR
    d.mkdir()
    parcels = d / street_parcels.PARCELS_NAME
    parcels.write_text("{}", encoding="utf-8")
    before = street_parcels._street_inputs_stamp(tmp_path)
    assert street_parcels._street_inputs_stamp(tmp_path) == before
    os.utime(parcels, ns=(1, 1))
    assert street_parcels._street_inputs_stamp(tmp_path) != before
