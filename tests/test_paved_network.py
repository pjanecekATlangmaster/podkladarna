"""Zpevněné plochy v lese a MTBO: šířky kresby ze sady, slučování ploch, silnice a cesty na ploše."""

from __future__ import annotations

import pytest

from app.pipeline import osm_paths as op
from app.pipeline.oom_symbol_map import symbol_line_width_mm


def test_symbol_line_width_from_set():
    # ISOM 1:15000: okraj 501.2 0,14 mm, silnice 503 0,35 mm, pěšina 506 0,18 mm.
    assert symbol_line_width_mm("les", 15000, "501.2") == pytest.approx(0.14)
    assert symbol_line_width_mm("les", 15000, "503") == pytest.approx(0.35)
    assert symbol_line_width_mm("les", 15000, "506") == pytest.approx(0.18)
    # 502 = výplň 0,3 mm + černé okraje → celkem 0,58 mm.
    assert symbol_line_width_mm("les", 15000, "502") == pytest.approx(0.58)
    # ISOM 1:10000 má symboly 1,5× větší.
    assert symbol_line_width_mm("les", 10000, "503") == pytest.approx(0.525, abs=0.01)
    # Plocha není čára.
    assert symbol_line_width_mm("les", 15000, "501.1") is None


def test_drawn_symbol_width_in_ground_metres():
    # 503 (0,35 mm) v 1:15000 = 5,25 m; v 1:10000 (sada 1,5×) totéž.
    assert op.drawn_symbol_width_m("road_2", "les", 15000) == pytest.approx(5.25)
    assert op.drawn_symbol_width_m("road_2", "les", 10000) == pytest.approx(5.25, abs=0.1)
    # MTBO pěšina 834 (0,4 mm) v 1:15000 = 6 m.
    assert op.drawn_symbol_width_m("footway", "mtbo", 15000) == pytest.approx(6.0)
    assert op.paved_edge_width_m("les", 15000) == pytest.approx(2.1)
    # MTBO okraj 501.100 (ISMTBOM „schod nebo okraj zpevněné plochy“) v 1:15000.
    from app.pipeline.oom_symbol_map import symbol_line_width_mm

    mm = symbol_line_width_mm("mtbo", 15000, "501.100")
    assert mm and op.paved_edge_width_m("mtbo", 15000) == pytest.approx(mm * 15)


def _road(coords, hw="road_2"):
    return {"type": "Feature", "properties": {"highway": hw}, "geometry": {"type": "LineString", "coordinates": coords}}


def test_sidewalk_zone_follows_symbol_widths():
    feats = [_road([(0, 0), (200, 0)])]
    zone15 = op.sidewalk_road_zone_geometry(feats, None, preset_id="les", scale=15000)
    zone75 = op.sidewalk_road_zone_geometry(feats, None, preset_id="les", scale=7500)
    near = [(0, 6), (200, 6)]  # chodník 6 m od osy silnice
    far = [(0, 11), (200, 11)]  # 11 m: dřív (pevných 12 m) se zahodil, teď je v 1:15000 čitelný
    assert op.is_sidewalk_beside_road("sidewalk", near, zone15)
    assert not op.is_sidewalk_beside_road("sidewalk", far, zone15)
    # 1:7500 (sada 1:10000 zmenšená): čáry jsou v terénu užší, 6 m od osy už je chodník čitelný.
    assert not op.is_sidewalk_beside_road("sidewalk", near, zone75)
    # Široká silnice 502.1 má širší pás.
    wide = op.sidewalk_road_zone_geometry([_road([(0, 0), (200, 0)], "road_4")], None, preset_id="les", scale=15000)
    assert wide.area > zone15.area


def test_sidewalk_zone_includes_paved_edge():
    from shapely.geometry import box

    zone = op.sidewalk_road_zone_geometry([], box(0, 0, 50, 50), preset_id="les", scale=15000)
    assert op.is_sidewalk_beside_road("sidewalk", [(0, 54), (50, 54)], zone)
    assert not op.is_sidewalk_beside_road("sidewalk", [(0, 60), (50, 60)], zone)


def _area(x0, y0, x1, y1, kind="pedestrian_area", **props):
    ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
    return {
        "type": "Feature",
        "properties": {"source": "osm", "kind": kind, "oom_code": "501", **props},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def _paved(feats):
    return [f for f in feats if f["properties"]["kind"] in op._PAVED_AREA_KINDS]


def test_network_merges_close_areas_across_kinds():
    feats = [_area(0, 0, 20, 20, "parking"), _area(23, 0, 60, 20), {"type": "Feature", "properties": {"kind": "bench"}, "geometry": {"type": "Point", "coordinates": [5, 5]}}]
    out, net = op.build_paved_network(feats, [])
    paved = _paved(out)
    assert len(paved) == 1
    assert paved[0]["properties"]["kind"] == "pedestrian_area"  # větší z obou
    assert paved[0]["properties"]["merged"] is True
    assert any(f["properties"]["kind"] == "bench" for f in out)
    assert net.area == pytest.approx(60 * 20, rel=0.01)


def test_network_does_not_merge_across_street():
    # Dvě plochy po stranách ulice (mezera 3 m, osa silnice uprostřed mezery).
    feats = [_area(0, 0, 50, 20), _area(0, 23, 50, 40)]
    road = _road([(-10, 21.5), (60, 21.5)])
    out, _net = op.build_paved_network(feats, [road])
    assert len(_paved(out)) == 2
    out, _net = op.build_paved_network(feats, [])
    assert len(_paved(out)) == 1


def test_network_keeps_bridge_areas_apart():
    feats = [_area(0, 0, 20, 20), _area(22, 0, 40, 20, structure="bridge"), _area(42, 0, 60, 20)]
    out, net = op.build_paved_network(feats, [])
    paved = _paved(out)
    assert len(paved) == 3
    assert sum(1 for f in paved if f["properties"].get("structure") == "bridge") == 1
    # most do sítě nepatří
    assert net.area == pytest.approx(20 * 20 + 18 * 20, rel=0.01)


def test_paved_area_structure_tags():
    assert op.paved_area_structure({"bridge": "yes"}) == "bridge"
    assert op.paved_area_structure({"layer": "1"}) == "bridge"
    assert op.paved_area_structure({"tunnel": "yes"}) == "tunnel"
    assert op.paved_area_structure({"layer": "-1"}) == "tunnel"
    assert op.paved_area_structure({"amenity": "parking"}) is None


def _write_job(tmp_path, features, paths=()):
    import json

    out = tmp_path / "osm_paths"
    out.mkdir(exist_ok=True)
    (out / "features.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    (out / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": list(paths)}), encoding="utf-8"
    )


def _codes(parts, preset, scale):
    import re
    from collections import Counter

    import app.pipeline.oom_symbol_map as sm

    id2code = {v: k for k, v in sm._code_to_index(sm.symbol_set_path(preset, scale)).items()}
    cnt: Counter[str] = Counter()
    for p in parts:
        for m in re.finditer(r'<object\b[^>]*\bsymbol="(\d+)"', p.objects_xml):
            cnt[id2code[int(m.group(1))]] += 1
    return cnt


@pytest.mark.parametrize("preset", ["forest_10000", "mtbo_10000"])
def test_feature_parts_draw_merged_network(tmp_path, preset):
    _write_job(tmp_path, [_area(0, 0, 20, 20, "parking"), _area(22, 0, 60, 20)])
    parts = _parts_with_edge(tmp_path, preset)  # výplň ze sítě + okraj z paved_surface
    cnt = _codes(parts, preset, 10000)
    assert cnt["501.1"] == 1 and cnt[op.paved_edge_code(preset)] == 1  # jedna plocha, jeden okraj (les 501.2, MTBO 501.100)


@pytest.mark.parametrize("preset", ["forest_10000", "mtbo_10000"])
def test_street_side_parking_merged_with_road(tmp_path, preset):
    # Hilmarova (way/1452628243): podélné stání 2–4,5 m od osy silnice – s ulicí jedna zpevněná plocha v šířce kresby.
    from shapely.geometry import shape

    # Lohniského: kolmé stání 6 m (širší než pruh), ale celé pod kresbou silnice – také s ulicí (2026-10-27).
    feats = [_area(0, 2, 40, 4.5, "parking"), _area(0, 15, 40, 40, "parking"), _area(70, -3, 110, 3, "parking")]
    _write_job(tmp_path, feats, [_road([(-20, 0), (130, 0)], "road_3")])
    out = op.merge_parking_into_road(tmp_path, feats, op._paths_features_for_network(tmp_path), preset, 10000)
    assert out[1] == feats[1]
    assert shape(out[2]["geometry"]).area > shape(feats[2]["geometry"]).area
    merged = shape(out[0]["geometry"])
    half = op.drawn_symbol_width_m("road_3", preset, 10000) / 2.0
    assert merged.bounds[1] == pytest.approx(-half, abs=0.01)  # silnice v šířce své kresby
    assert merged.bounds[0] < 0 and merged.bounds[2] > 40
    assert op.merge_parking_into_road(tmp_path, feats, [], preset, 10000) == feats
    assert op.merge_parking_into_road(tmp_path, feats, op._paths_features_for_network(tmp_path), "sprint_4000", 4000) == feats


def test_street_side_parking_road_not_drawn_over(tmp_path):
    # Silnice v místě stání se nekreslí (je plochou 501.1 s okrajem 501.2), mimo něj ano.
    _write_job(tmp_path, [_area(0, 2, 40, 4.5, "parking")], [_road([(-50, 0), (90, 0)], "road_3")])
    lengths = _path_lengths(tmp_path)
    assert lengths["502.2"] < 140 - 40


def _line(coords, hw):
    return {"type": "Feature", "properties": {"highway": hw}, "geometry": {"type": "LineString", "coordinates": coords}}


def _path_lengths(tmp_path, preset="forest_10000", scale=10000):
    """Délky nakreslených čar (m) podle kódu symbolu – z map souřadnic zpět (1 mm mapy = scale/1000 m)."""
    import re

    import app.pipeline.oom_symbol_map as sm

    op._PAVED_NETWORK_CACHE.clear()
    parts = op.build_osm_path_parts(tmp_path, preset_id=preset, scale=scale, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    id2code = {v: k for k, v in sm._code_to_index(sm.symbol_set_path(preset, scale)).items()}
    out: dict[str, float] = {}
    for p in parts:
        for m in re.finditer(r'<object\b[^>]*\bsymbol="(\d+)".*?<coords[^>]*>(.*?)</coords>', p.objects_xml, re.DOTALL):
            pts = [tuple(int(v) for v in c.split()[:2]) for c in m.group(2).split(";") if c.strip()]
            length = sum(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in zip(pts, pts[1:]))
            code = id2code[int(m.group(1))]
            out[code] = out.get(code, 0.0) + length / 1000.0 * scale / 1000.0
    return out


def test_road_over_parking_kept_only_as_connection(tmp_path):
    # Parkoviště 40 × 40 m, silnice vede z ulice (x < 0) přes celé parkoviště a dál.
    road = _line([(-50, 20), (90, 20)], "road_2")
    _write_job(tmp_path, [_area(0, 0, 40, 40, "parking")], [road])
    lengths = _path_lengths(tmp_path)
    assert lengths["503.0"] == pytest.approx(100, abs=1.0)  # 140 m − 40 m uvnitř parkoviště


def test_road_grazing_corner_is_not_cut(tmp_path):
    road = _line([(-50, 42), (90, 38)], "road_2")  # škrtne parkoviště jen na pár metrech
    _write_job(tmp_path, [_area(0, 0, 40, 40, "parking")], [road])
    assert _path_lengths(tmp_path)["503.0"] == pytest.approx(140, abs=1.0)


def test_track_and_path_interrupted_on_paved_area(tmp_path):
    track = _line([(-30, 10), (70, 10)], "track")
    path = _line([(-30, 30), (70, 30)], "footway")
    _write_job(tmp_path, [_area(0, 0, 40, 40)], [track, path])
    lengths = _path_lengths(tmp_path)
    assert lengths["504.0"] == pytest.approx(60, abs=1.0)  # vozová cesta
    assert lengths["506.0"] == pytest.approx(60, abs=1.0)  # pěšina


def test_bridge_over_paved_area_is_kept(tmp_path):
    bridge = _line([(-30, 20), (70, 20)], op.bridge_highway("road_2"))
    _write_job(tmp_path, [_area(0, 0, 40, 40)], [bridge])
    lengths = _path_lengths(tmp_path)
    assert sum(lengths.values()) == pytest.approx(100, abs=1.0)


def _parts_with_edge(tmp_path, preset="forest_10000", scale=10000):
    """Výplň sítě (``build_osm_feature_parts``) + okraj z hotových čar cest (les a MTBO: ``paved_surface``)."""
    from app.pipeline import dtm_import as di
    from app.pipeline import paved_surface
    from app.pipeline.path_clearance import drawn_path_bands

    kw = dict(preset_id=preset, scale=scale, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    op._PAVED_NETWORK_CACHE.clear()
    feature_parts = op.build_osm_feature_parts(tmp_path, **kw)
    path_parts = op.build_osm_path_parts(tmp_path, **kw)
    bands = drawn_path_bands(path_parts, majors=di.JUNCTION_MAJORS, **kw)
    return feature_parts + paved_surface.build_paved_edge_parts(tmp_path, road_bands=bands, **kw)

def _edge_length(parts, preset="forest_10000", scale=10000):
    import re

    import app.pipeline.oom_symbol_map as sm

    edge_id = sm._code_to_index(sm.symbol_set_path(preset, scale))[op.paved_edge_code(preset)]
    total = 0.0
    for p in parts:
        for m in re.finditer(rf'<object\b[^>]*\bsymbol="{edge_id}".*?<coords[^>]*>(.*?)</coords>', p.objects_xml, re.DOTALL):
            pts = [tuple(int(v) for v in c.split()[:2]) for c in m.group(1).split(";") if c.strip()]
            total += sum(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in zip(pts, pts[1:]))
    return total / 1000.0 * scale / 1000.0


def test_edge_omitted_along_road(tmp_path):
    # Parkoviště 40 × 40 m, silnice podél spodní hrany 3 m od ní: tam okraj nekreslit.
    road = _line([(-50, -3), (90, -3)], "road_2")
    _write_job(tmp_path, [_area(0, 0, 40, 40, "parking")], [road])
    parts = _parts_with_edge(tmp_path)
    length = _edge_length(parts)
    assert length < 160 - 30  # spodní hrana (40 m) a kousky boků chybí
    assert length > 100  # horní hrana a většina boků zůstává
    # Bez silnice celý obvod.
    _write_job(tmp_path, [_area(0, 0, 40, 40, "parking")], [])
    parts = _parts_with_edge(tmp_path)
    assert _edge_length(parts) == pytest.approx(160, abs=1.0)


@pytest.mark.parametrize("preset", ["forest_10000", "mtbo_10000"])
def test_edge_kept_along_road_inside_parking(tmp_path, preset):
    # Jízdní pruh uvnitř parkoviště (nekreslí se) okraj nemaže; vjezd ano jen kolem napojení (MTBO jako les).
    road = _line([(-50, 20), (90, 20)], "road_2")
    _write_job(tmp_path, [_area(0, 0, 40, 40, "parking")], [road])
    parts = _parts_with_edge(tmp_path, preset)
    length = _edge_length(parts, preset)
    assert 160 - 2 * 25 < length < 160  # chybí jen okolí dvou vjezdů


def test_road_over_pitch_is_kept(tmp_path):
    # Silnice přes hřiště / sportoviště se neřeže (spíš nepřesnost OSM), jen přes parkoviště a pěší zónu.
    road = _line([(-50, 20), (90, 20)], "road_2")
    _write_job(tmp_path, [_area(0, 0, 40, 40, "pitch")], [road])
    assert _path_lengths(tmp_path)["503.0"] == pytest.approx(140, abs=1.0)


def test_network_blocks_merge_with_osm_roads(tmp_path):
    # Silnice pro blokaci spojení přes ulici se berou z paths_osm (mixed je po dedupu se ZABAGED neúplný).
    import json

    _write_job(tmp_path, [_area(0, 0, 50, 20), _area(0, 23, 50, 40)], [])
    (tmp_path / "osm_paths" / "paths_osm.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [_road([(-10, 21.5), (60, 21.5)])]}), encoding="utf-8"
    )
    op._PAVED_NETWORK_CACHE.clear()
    net = op.paved_network_geometry(tmp_path)
    assert net.geom_type == "MultiPolygon"


def test_footway_beside_road_dropped_like_sidewalk():
    # Brichtova (Praha 5): highway=footway bez footway=sidewalk 5–9 m od osy residential (502) – v lese 1:10000 nečitelné.
    zone = op.sidewalk_road_zone_geometry([_road([(0, 0), (100, 0)], "road_3")], None, preset_id="forest_10000", scale=10000)
    walk = [(0, 4.8), (40, 7.3), (100, 8.8)]
    assert op.is_sidewalk_beside_road("footway", walk, zone)
    assert not op.is_sidewalk_beside_road("track", walk, zone)


def test_cycleway_beside_road_dropped_in_forest_only():
    # Ke Smíchovu (way/69720778): cyklostezka 5–7 m od osy silnice – v lese se slije, MTBO ji pro jezdce nechá.
    zone = op.sidewalk_road_zone_geometry([_road([(0, 0), (100, 0)], "road_3")], None, preset_id="forest_10000", scale=10000)
    bike = [(0, 5.0), (100, 6.5)]
    assert op.is_sidewalk_beside_road("cycleway", bike, zone, op.sidewalk_dedup_bases("forest_10000"))
    assert not op.is_sidewalk_beside_road("cycleway", bike, zone, op.sidewalk_dedup_bases("mtbo_10000"))


def test_sidewalk_zone_uses_dtm_road_symbol():
    from shapely.geometry import box

    from app.pipeline.dtm_widths import WidthIndex

    roads = [_road([(0, 0), (100, 0)], "road_2")]
    plain = op.sidewalk_road_zone_geometry(roads, None, preset_id="forest_10000", scale=10000)
    wide = op.sidewalk_road_zone_geometry(
        roads, None, preset_id="forest_10000", scale=10000, road_widths=WidthIndex([box(0, -5, 100, 5)])
    )
    # Silnice široká 10 m v DTM se kreslí 502.1 → pás je širší než podle tagu.
    assert wide.area > plain.area


def test_path_crossing_parking_not_cut_before_edge():
    from shapely.geometry import box

    zone = op.sidewalk_road_zone_geometry([], box(0, 0, 40, 40), preset_id="forest_10000", scale=10000)
    crossing = [(20, -30), (20, 70)]
    assert op.sidewalk_parallel_zone("footway", crossing, zone) is None


def _walk(coords, hw="sidewalk"):
    return ([tuple(c) for c in coords], hw)


def test_corridor_not_from_bridge():
    items = [_walk([(0, 0), (60, 0)]), _walk([(0, 8), (60, 8)], op.bridge_highway("sidewalk"))]
    kept, areas = op.merge_parallel_pedestrian_to_areas(items)
    assert areas == [] and len(kept) == 2


def test_corridor_only_from_paved_lines():
    items = [_walk([(0, 0), (60, 0)], "pedestrian"), _walk([(0, 8), (60, 8)], "pedestrian")]
    _kept, areas = op.merge_parallel_pedestrian_to_areas(items)
    assert len(areas) == 1  # bez informace o povrchu jako dřív
    _kept, areas = op.merge_parallel_pedestrian_to_areas(items, paved_lines=[items[0][0]])
    assert areas == []  # jen jedna z čar má zpevněný povrch
    _kept, areas = op.merge_parallel_pedestrian_to_areas(items, paved_lines=[items[0][0], items[1][0]])
    assert len(areas) == 1


def test_corridor_not_over_building():
    from shapely.geometry import box

    items = [_walk([(0, 0), (60, 0)]), _walk([(0, 10), (60, 10)])]
    _kept, areas = op.merge_parallel_pedestrian_to_areas(items, buildings=[box(10, 2, 40, 9)])
    assert areas == []


def test_corridor_not_from_diverging_lines():
    # Čáry se rozbíhají (min. rozestup 2 m, na konci 30 m): průměrná šířka nad 12 m → žádný koridor.
    items = [_walk([(0, 0), (60, 0)]), _walk([(0, 2), (60, 30)])]
    _kept, areas = op.merge_parallel_pedestrian_to_areas(items)
    assert areas == []


def _write_dtm(tmp_path, name, rects):
    import json

    d = tmp_path / "dtm"
    d.mkdir(exist_ok=True)
    polys = [[[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]] for x0, y0, x1, y1 in rects]
    (d / name).write_text(json.dumps({"polygons": polys}), encoding="utf-8")


def test_dtm_width_index_measures_strip():
    from shapely.geometry import box

    from app.pipeline.dtm_widths import WidthIndex

    idx = WidthIndex([box(0, -3, 100, 3)])
    assert idx.line_width([(0, 0), (100, 0)]) == pytest.approx(6.0)
    assert idx.line_width([(0, 20), (100, 20)]) is None  # čára mimo plochu


def test_width_line_code_rules():
    from app.pipeline.dtm_widths import width_line_code

    assert width_line_code("road_2", 10.0, "503") == "502.1"
    assert width_line_code("road_2", 8.0, "503") == "502"
    assert width_line_code("road_2", 6.0, "503") == "502"
    assert width_line_code("road_2", 4.0, "503") == "503"
    assert width_line_code("road_2", None, "503") == "503"
    assert width_line_code("footway", 3.0, "506") == "505"
    assert width_line_code("footway", 1.5, "506") == "506"
    assert width_line_code("track", 3.0, "504") == "504"


def test_forest_symbols_from_dtm_width(tmp_path, monkeypatch):
    # Silnice v OSM jako road_2 (503), v DTM 6 m široká → 502; pěšina 3 m → 505.
    road = _line([(0, 0), (200, 0)], "road_2")
    walk = _line([(0, 50), (200, 50)], "footway")
    _write_job(tmp_path, [], [road, walk])
    _write_dtm(tmp_path, "roads.json", [(-5, -3, 205, 3)])
    _write_dtm(tmp_path, "sidewalks.json", [(-5, 48.5, 205, 51.5)])
    lengths = _path_lengths(tmp_path)
    assert lengths["502.2"] == pytest.approx(200, abs=1.0)  # generátor 502 = v sadě 502.2 (5–9 m)
    assert lengths["505.0"] == pytest.approx(200, abs=1.0)


def test_forest_wide_dtm_walk_becomes_paved_area(tmp_path):
    # Pěší plocha z DTM 20 m široká → zpevněná plocha 501.1 + okraj, pěšina uvnitř se nekreslí.
    walk = _line([(0, 50), (200, 50)], "footway")
    _write_job(tmp_path, [], [walk])
    _write_dtm(tmp_path, "sidewalks.json", [(0, 40, 200, 60)])
    op._PAVED_NETWORK_CACHE.clear()
    parts = op.build_osm_feature_parts(tmp_path, preset_id="forest_10000", scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    cnt = _codes(parts, "forest_10000", 10000)
    assert cnt["501.1"] == 1
    assert sum(_path_lengths(tmp_path).values()) == pytest.approx(0, abs=1.0)


def test_mtbo_ignores_dtm_widths_when_off(tmp_path, monkeypatch):
    from app.pipeline import dtm_import as di

    monkeypatch.setattr(di, "MTBO_USE_DTM", False)
    road = _line([(0, 0), (200, 0)], "road_2")
    _write_job(tmp_path, [], [road])
    _write_dtm(tmp_path, "roads.json", [(-5, -5, 205, 5)])
    lengths = _path_lengths(tmp_path, preset="mtbo_10000")
    assert len(lengths) == 1  # jeden symbol podle tagů, DTM je v MTBO vypnuté


@pytest.mark.parametrize("road_areas, expected", [(True, {}), (False, {"502.1": 200.0})])
def test_mtbo_wide_dtm_road_drawn_by_corridor(tmp_path, monkeypatch, road_areas, expected):
    # MTBO s DTM: silnice široká 10 m (1 mm v 1:10000) kreslí koridor plochou, čára uvnitř se nekreslí
    # (záložní režim koridoru podle šířky; s JUNCTION_AREAS je plocha jen u složitých křižovatek).
    # ``ROAD_AREAS = False`` (2.45.0): les a MTBO kreslí silnici jen čarou.
    from app.pipeline import junction_areas

    monkeypatch.setattr(junction_areas, "ROAD_AREAS", road_areas)
    monkeypatch.setattr(junction_areas, "JUNCTION_AREAS", False)
    road = _line([(0, 0), (200, 0)], "road_2")
    _write_job(tmp_path, [], [road])
    _write_dtm(tmp_path, "roads.json", [(-5, -5, 205, 5)])
    assert _path_lengths(tmp_path, preset="mtbo_10000") == expected


def test_prepare_stores_rail_tunnels_only(tmp_path, monkeypatch):
    import json

    def way(i, tags, lon0):
        return {
            "type": "way",
            "id": i,
            "tags": tags,
            "geometry": [{"lat": 50.0345, "lon": lon0}, {"lat": 50.0345, "lon": lon0 + 0.003}],
        }

    tram_tunnel = way(1177591713, {"railway": "tram", "layer": "-1", "tunnel": "yes"}, 14.372)
    tram_street = way(2, {"railway": "tram", "highway": "residential", "surface": "asphalt"}, 14.380)
    monkeypatch.setattr(op, "fetch_osm_path_elements", lambda *a, **k: [tram_tunnel, tram_street])
    op.prepare_osm_paths(tmp_path, (14.37, 50.03, 14.39, 50.04), None, preset_id="forest_10000", log=lambda m: None)
    rails = json.loads((tmp_path / "osm_paths" / op.RAIL_TUNNELS_NAME).read_text(encoding="utf-8"))["features"]
    assert len(rails) == 1
    zone = op.rail_tunnel_geometry(tmp_path)
    assert zone is not None and zone.area > 0
    # Tramvaj v ose silnice (jedna way s highway) zůstává silnicí.
    paths = json.loads((tmp_path / "osm_paths" / "paths_osm.geojson").read_text(encoding="utf-8"))["features"]
    assert any(str(f["properties"]["highway"]).startswith("road_") for f in paths)


def test_long_tunnel_not_drawn_in_forest(tmp_path):
    long_tunnel = _line([(0, 0), (200, 0)], op.tunnel_highway("road_3"))
    underpass = _line([(0, 50), (20, 50)], op.tunnel_highway("footway"))
    _write_job(tmp_path, [], [long_tunnel, underpass])
    lengths = _path_lengths(tmp_path)
    assert sum(lengths.values()) == pytest.approx(20, abs=1.0)  # jen krátký podchod
    lengths = _path_lengths(tmp_path, preset="mtbo_10000")
    assert sum(lengths.values()) == pytest.approx(20, abs=1.0)


def test_small_complex_steps_not_drawn():
    feats = [
        _line([(0, 0), (5, 0)], "steps"),  # „L“ ze dvou ramen 5 + 5 m
        _line([(5, 0), (5, 5)], "steps"),
        _line([(50, 0), (50, 3), (54, 3), (54, 0)], "steps"),  # „C“ v jedné čáře
        _line([(100, 0), (108, 0.3)], "steps"),  # rovné krátké schody zůstávají
        _line([(200, 0), (220, 0), (220, 10)], "steps"),  # velké zalomené schodiště zůstává
        _line([(0, 10), (5, 10), (5, 15)], "footway"),
    ]
    assert op.small_complex_steps(feats) == {0, 1, 2}


def test_small_complex_steps_skipped_outside_sprint(tmp_path):
    _write_job(tmp_path, [], [_line([(0, 0), (5, 0)], "steps"), _line([(5, 0), (5, 5)], "steps")])
    assert _path_lengths(tmp_path) == {}
    assert sum(_path_lengths(tmp_path, "sprint_4000", 4000).values()) > 0


def test_wide_parking_with_inner_road_kept(tmp_path):
    # Lidl (way/128405208): parkoviště s obslužnou komunikací uvnitř leží celé v pásu silnice, ale je široké.
    feats = [_area(0, -10, 40, 10, "parking")]
    _write_job(tmp_path, feats, [_road([(-20, 0), (60, 0)], "road_4")])
    assert op.merge_parking_into_road(tmp_path, feats, op._paths_features_for_network(tmp_path), "forest_10000", 10000) == feats


@pytest.mark.parametrize("preset", ["forest_10000", "mtbo_10000"])
def test_narrow_walk_lobe_removed(tmp_path, preset):
    from shapely.geometry import box, shape

    # Náměstí 40 × 40 m s výběžkem širokým 4 m – výběžek kreslí čára cesty, náměstí zůstává plochou.
    from shapely.geometry import mapping

    square = box(0, 0, 40, 40).union(box(40, 18, 80, 22))
    feat = {"type": "Feature", "properties": {"kind": "pedestrian_area"}, "geometry": mapping(square)}
    out = op.open_narrow_walk_areas([feat], preset, 10000)
    assert len(out) == 1
    assert shape(out[0]["geometry"]).bounds[2] == pytest.approx(40, abs=0.5)
    narrow = {"type": "Feature", "properties": {"kind": "pedestrian_area"}, "geometry": mapping(box(0, 0, 60, 5))}
    assert op.open_narrow_walk_areas([narrow], preset, 10000) == []
    assert op.open_narrow_walk_areas([narrow], "sprint_4000", 4000) == [narrow]
    parking = {"type": "Feature", "properties": {"kind": "parking"}, "geometry": mapping(box(0, 0, 60, 5))}
    assert op.open_narrow_walk_areas([parking], preset, 10000) == [parking]


def test_corridor_axis_between_lines():
    axes: list = []
    items = [_walk([(0, 0), (60, 0)], "pedestrian"), _walk([(0, 6), (60, 6)], "pedestrian")]
    _kept, areas = op.merge_parallel_pedestrian_to_areas(items, axes_out=axes)
    assert len(areas) == 1 and len(axes) == 1
    pts, hw = axes[0]
    assert hw == "pedestrian"
    assert all(abs(y - 3.0) < 0.01 for _x, y in pts)


@pytest.mark.parametrize("width, drawn", [(5, True), (20, False)])
def test_corridor_axis_drawn_only_where_area_dropped(tmp_path, width, drawn):
    # Trnkovo nám. (way/1033591397 + /96): úzký koridor odpadne → kreslí se osa; široký zůstane plochou.
    import json

    feats = [_area(0, 0, 60, width)]
    _write_job(tmp_path, feats, [])
    axis = _line([(0, width / 2), (60, width / 2)], "pedestrian")
    (tmp_path / "osm_paths" / op.CORRIDOR_AXES_NAME).write_text(
        json.dumps({"type": "FeatureCollection", "features": [axis]}), encoding="utf-8"
    )
    total = sum(_path_lengths(tmp_path).values())
    assert (total > 50) == drawn
    assert sum(_path_lengths(tmp_path, "sprint_4000", 4000).values()) == 0


def test_corridor_axis_covers_longer_line():
    # Delší čára (80 m) s kratší (30 m): osa vede po celé délce delší, ne jen v překryvu.
    from shapely.geometry import LineString

    axis = op._corridor_axis([(0, 0), (80, 0)], [(20, 4), (50, 4)])
    line = LineString(axis)
    assert line.length == pytest.approx(80, abs=1.0)
    assert all(abs(y - 2.0) < 0.01 for _x, y in axis)


def test_dtm_walk_area_not_opened():
    from shapely.geometry import box, mapping

    dtm = {"type": "Feature", "properties": {"kind": "pedestrian_area", "source": "dtm"}, "geometry": mapping(box(0, 0, 60, 6))}
    assert op.open_narrow_walk_areas([dtm], "forest_10000", 10000) == [dtm]


def test_short_pieces_joined_into_one_line(tmp_path):
    # Pěšina ze tří navazujících úseků (prostřední krátké schody → pěšina) je v lese jedna čára, ve sprintu tři.
    paths = [_line([(0, 0), (30, 0)], "footway"), _line([(30, 0), (35, 0)], "steps"), _line([(35, 0), (70, 0)], "footway")]
    _write_job(tmp_path, [], paths)

    def count(preset, scale):
        op._PAVED_NETWORK_CACHE.clear()
        parts = op.build_osm_path_parts(tmp_path, preset_id=preset, scale=scale, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
        return sum(p.count for p in parts)

    assert count("forest_10000", 10000) == 1
    assert count("sprint_4000", 4000) == 3
    assert op.join_line_pieces([[(0, 0), (1, 0)], [(1, 0), (2, 0)], [(1, 0), (1, 1)]]) != [[(0, 0), (2, 0)]]
    # Konce do JOIN_SNAP_M (0,2 m) od sebe se spojí (po ořezech končí kusy v různých bodech), dál ne.
    assert op.join_line_pieces([[(0, 0), (10, 0)], [(10.15, 0.1), (20, 0)]]) == [[(0, 0), (10, 0), (20, 0)]]
    assert len(op.join_line_pieces([[(0, 0), (10, 0)], [(10.5, 0), (20, 0)]])) == 2


def test_join_through_junction_straightest_continuation():
    # T křižovatka: průběžná ulice se spojí přes uzel, rameno zůstane zvlášť; rameno pod 45° se nespojí s ničím.
    out = op.join_line_pieces([[(0, 0), (10, 0)], [(10, 0), (20, 1)], [(10, 0), (10, 10)]])
    assert sorted(len(p) for p in out) == [2, 3]
    assert [p for p in out if len(p) == 3][0] == [(0, 0), (10, 0), (20, 1)]
    # Kruhový objezd ze čtyř oblouků s radiálními rameny v uzlech → jeden uzavřený prstenec + čtyři ramena.
    import math

    arc = lambda a0, a1: [(round(10 * math.cos(math.radians(a)), 3), round(10 * math.sin(math.radians(a)), 3)) for a in range(a0, a1 + 1, 15)]
    ring = [arc(0, 90), arc(90, 180), arc(180, 270), arc(270, 360)]
    arms = [[(10, 0), (30, 0)], [(0, 10), (0, 30)], [(-10, 0), (-30, 0)], [(0, -10), (0, -30)]]
    out = op.join_line_pieces(ring + arms)
    closed = [p for p in out if p[0] == p[-1]]
    assert len(closed) == 1 and len(closed[0]) == 25 and len(out) == 5


def test_street_side_parking_edge_continues_to_road(tmp_path):
    # Ulice slitá se stáním: okraj 501.2 vede po obou dlouhých stranách až k čáře silnice, přes ústí ne.
    _write_job(tmp_path, [_area(0, 2, 40, 4.5, "parking")], [_road([(-50, 0), (90, 0)], "road_3")])
    parts = _parts_with_edge(tmp_path)
    # Ústí do silnice: okraj končí na okrajové lince silnice (půlka výplně 502, dřív pás kresby + 0,35 mm = 3,5 m),
    # proto dosáhne o pár metrů blíž k silnici než dřívější horní mez 105 m (dlouhé strany 2 × 51 m + zbytky u rohů).
    assert 100 < _edge_length(parts) < 114


@pytest.mark.parametrize("squeezed", [False, True])
def test_service_link_drawn_as_road(tmp_path, squeezed):
    # Účelová komunikace ze silnice na parkoviště (way/1032671443): kreslí se 502, pokud se kresba vejde mezi budovy.
    feats = [_area(-20, 30, 20, 60, "parking")]
    if squeezed:
        feats += [_area(-12, 8, -2, 25, "building"), _area(2, 8, 12, 25, "building")]
    _write_job(tmp_path, feats, [_road([(-60, 0), (60, 0)], "road_3"), _line([(0, 0), (0, 30)], "road_1")])
    lengths = _path_lengths(tmp_path)
    if squeezed:
        assert lengths.get("504.0", 0) > 20
    else:
        # Vjezd na parkoviště kreslí pruh zpevněné plochy (``extend_parking_by_links``), čára ne.
        assert lengths.get("504.0", 0) == 0
        assert lengths["502.2"] == pytest.approx(120, abs=1.0)
        half = op.drawn_symbol_width_m("road_3", "forest_10000", 10000) / 2.0
        net = op.paved_network_geometry(tmp_path, "forest_10000", 10000)
        assert net.bounds[1] == pytest.approx(half, abs=0.05)  # plocha od okraje kresby silnice
        assert net.contains(op_point(0, half + 10))


def op_point(x, y):
    from shapely.geometry import Point

    return Point(x, y)


def test_service_link_to_parking_edge_open_at_road(tmp_path):
    # Okraj 501.2 pruhu vjezdu vede po obou stranách až ke kresbě silnice, přes ústí do silnice ne.
    _write_job(
        tmp_path,
        [_area(-20, 30, 20, 60, "parking")],
        [_road([(-60, 0), (60, 0)], "road_3"), _line([(0, 0), (0, 30)], "road_1")],
    )
    parts = _parts_with_edge(tmp_path)
    half = op.drawn_symbol_width_m("road_3", "forest_10000", 10000) / 2.0
    # parkoviště 40 + 2·30 − šířka ústí do pruhu + 2 · délka pruhu (30 − half), bez ústí do silnice
    expected = 40 + 2 * 30 + (40 - 2 * half) + 2 * (30 - half)
    # Ústí do silnice je teď jen pás kresby čáry + 0,3 m (dřív + půlka okraje + mezera čitelnosti 0,35 mm),
    # okraj pruhu proto sahá na obou stranách o tolik blíž k čáře silnice.
    from app.pipeline.dtm_import import JUNCTION_EXTRA_M

    closer = op.paved_edge_width_m("forest_10000", 10000) / 2.0 + op.mm_to_ground_m(op.LINE_MIN_GAP_MM, 10000) - JUNCTION_EXTRA_M
    assert expected - 2 * half - 2.0 < _edge_length(parts) < expected + 2 * closer + 1.0


def test_sidewalk_beside_road_keeps_long_remainder_away_from_road(tmp_path):
    # Chodník 150 m podél silnice (zahodí se) pokračuje 56 m pryč od ní kolem hřiště (way/1451823252): zbytek zůstává.
    _write_job(tmp_path, [], [_road([(0, 0), (200, 0)], "road_3"), _line([(0, 4), (150, 4), (150, 60)], "sidewalk")])
    lengths = _path_lengths(tmp_path)
    drawn = sum(v for c, v in lengths.items() if not c.startswith("502"))
    assert 40 < drawn < 58
    # Bez zbytku (celý podél silnice) se chodník zahodí celý.
    _write_job(tmp_path, [], [_road([(0, 0), (200, 0)], "road_3"), _line([(0, 4), (150, 4), (150, 20)], "sidewalk")])
    lengths = _path_lengths(tmp_path)
    assert sum(v for c, v in lengths.items() if not c.startswith("502")) == 0


def test_service_link_to_garage_stays_line(tmp_path):
    _write_job(
        tmp_path,
        [_area(-10, 30, 10, 45, "building")],
        [_road([(-60, 0), (60, 0)], "road_3"), _line([(0, 0), (0, 30)], "road_1")],
    )
    lengths = _path_lengths(tmp_path)
    assert lengths["502.2"] > 120 + 20


@pytest.mark.parametrize("hw", ["footway", "track", "road_1"])
def test_black_path_ends_at_road_drawing(tmp_path, hw):
    # Černá cesta přes silnici 502 se kreslí jen k okraji kresby silnice (z obou stran), ne přes ni.
    _write_job(tmp_path, [], [_road([(-60, 0), (60, 0)], "road_3"), _line([(0, -40), (0, 40)], hw)])
    lengths = _path_lengths(tmp_path)
    half = op.drawn_symbol_width_m("road_3", "forest_10000", 10000) / 2.0
    black = sum(v for k, v in lengths.items() if not k.startswith("502"))
    assert black == pytest.approx(80 - 2 * half, abs=0.5)


def test_path_ending_at_dropped_sidewalk_extends_to_road_drawing(tmp_path):
    # Chodník souběžný se silnicí se zahodí; pěšina, která na něm končila, se prodlouží k okraji kresby silnice.
    _write_job(
        tmp_path,
        [],
        [
            _road([(-60, 0), (60, 0)], "road_3"),
            _line([(-60, 7), (60, 7)], "sidewalk"),
            _line([(0, 7), (0, 40)], "footway"),
        ],
    )
    lengths = _path_lengths(tmp_path)
    half = op.drawn_symbol_width_m("road_3", "forest_10000", 10000) / 2.0
    black = sum(v for k, v in lengths.items() if not k.startswith("502"))
    assert black == pytest.approx(40 - half, abs=0.5)


def test_path_not_reaching_dropped_sidewalk_not_extended(tmp_path):
    # Pěšina končí 5 m od zahozeného chodníku: neprodlouží se.
    _write_job(
        tmp_path,
        [],
        [
            _road([(-60, 0), (60, 0)], "road_3"),
            _line([(-60, 7), (60, 7)], "sidewalk"),
            _line([(0, 12), (0, 40)], "footway"),
        ],
    )
    lengths = _path_lengths(tmp_path)
    black = sum(v for k, v in lengths.items() if not k.startswith("502"))
    assert black == pytest.approx(28, abs=0.5)


def test_black_path_crosses_narrow_road(tmp_path):
    # Silnice 503 (černá čára) cestu nepřerušuje.
    _write_job(tmp_path, [], [_road([(-60, 0), (60, 0)], "road_2"), _line([(0, -40), (0, 40)], "track")])
    assert _path_lengths(tmp_path)["504.0"] == pytest.approx(80, abs=0.5)


def test_street_side_parking_beside_road_drawing_merged(tmp_path):
    # way/1452628250: stání 4,1–8,2 m od osy leží hlavně vedle kresby silnice, přiléhá k ní dlouhou stranou → slije se.
    from shapely.geometry import shape

    feats = [_area(0, 4.1, 30, 8.2, "parking")]
    _write_job(tmp_path, feats, [_road([(-20, 0), (50, 0)], "road_3")])
    out = op.merge_parking_into_road(tmp_path, feats, op._paths_features_for_network(tmp_path), "forest_10000", 10000)
    assert shape(out[0]["geometry"]).bounds[1] < 0


def test_short_service_ignores_dtm_width(tmp_path):
    # way/1032671443: řez DTM zachytí těleso sousední ulice (≥ 9 m) – krátká service nedostane 502.1.
    _write_job(tmp_path, [], [_line([(0, 0), (25, 0)], "road_1")])
    _write_dtm(tmp_path, "roads.json", [(-5, -6, 30, 6)])
    _write_dtm(tmp_path, "sidewalks.json", [(500, 500, 510, 510)])
    lengths = _path_lengths(tmp_path)
    assert "502.1" not in lengths


def test_network_polygons_valid_after_map_rounding():
    # Praha 5: mitre buffery a ořez blokery dávaly sebeprotínající 501.1 (po zaokrouhlení na souřadnice mapy)
    # i výjimku GEOS. Náhodné natočené plochy + silnice → všechny výstupy validní i po zaokrouhlení na 1 cm.
    import random

    from shapely import affinity
    from shapely.geometry import Polygon, box, mapping

    rnd = random.Random(1)
    for _ in range(120):
        feats = []
        for _k in range(rnd.randint(3, 8)):
            x, y = rnd.uniform(0, 80), rnd.uniform(0, 80)
            g = affinity.rotate(box(x, y, x + rnd.uniform(2, 25), y + rnd.uniform(2, 25)), rnd.uniform(0, 90))
            feats.append({"type": "Feature", "properties": {"kind": "parking"}, "geometry": mapping(g)})
        road = _road([(rnd.uniform(0, 100), 0), (rnd.uniform(0, 100), 100)], "road_3")
        out, _net = op.build_paved_network(feats, [road])
        ang = rnd.uniform(0, 360)
        for f in out:
            g = affinity.rotate(Polygon(f["geometry"]["coordinates"][0], f["geometry"]["coordinates"][1:]), ang, origin=(0, 0))
            ext = [(round(x * 100), round(y * 100)) for x, y in g.exterior.coords]
            holes = [[(round(x * 100), round(y * 100)) for x, y in h.coords] for h in g.interiors]
            rounded = Polygon(ext, holes)
            assert rounded.is_valid and rounded.exterior.is_simple


def test_mtbo_paved_network_contains_dtm_areas(tmp_path, monkeypatch):
    from app.pipeline import junction_areas

    monkeypatch.setattr(junction_areas, "ROAD_AREAS", True)
    monkeypatch.setattr(junction_areas, "JUNCTION_AREAS", False)  # koridor podle šířky (záložní režim)
    # MTBO s DTM: koridor silnice (silnice 12 m z DTM) a široký chodník z DTM už nejsou samostatné části, ale prvky
    # (kind "paved_dtm") sítě zpevněných ploch – síť je MÁ pokrývat (jedna plocha, jeden okraj z paved_surface);
    # dřív (UNIFIED_PAVED = False) se z nich síť ořezávala, aby se tatáž plocha nekreslila dvakrát.
    road = _road([(0, 0), (200, 0)], "road_3")
    _write_job(tmp_path, [_area(20, -10, 80, 40), _area(120, 60, 180, 90)], [road])
    _write_dtm(tmp_path, "roads.json", [(-5, -6, 205, 6)])
    _write_dtm(tmp_path, "sidewalks.json", [(0, 30, 200, 36)])  # 6 m široká pěší cesta 30 m od silnice
    from app.pipeline import dtm_import as di

    op._PAVED_NETWORK_CACHE.clear()
    zone = di.mtbo_dtm_area_geometry(tmp_path, 10000)
    assert zone is not None and zone.area > 200 * 12
    assert op.with_dtm_walk_areas(tmp_path, [], "mtbo_10000", 10000) == []  # široké chodníky nejsou v MTBO z OSM walk
    net = op.paved_network_geometry(tmp_path, "mtbo_10000", 10000)
    assert net is not None
    # nový režim: plochy z DTM jsou součástí sítě (dřív: průnik < 1 m²)
    assert net.intersection(zone).area > 0.95 * zone.area
    assert net.area > zone.area + 0.9 * 60 * 30  # samostatná OSM plocha mimo DTM zůstává
    # a kreslí se jako výplň 501.1 sítě, ne jako samostatná část koridoru / chodníků
    from app.pipeline import dtm_import as di2

    kw = dict(preset_id="mtbo_10000", scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    assert di2.build_mtbo_corridor_parts(tmp_path, **kw) == []
    assert di2.build_dtm_sidewalk_parts(tmp_path, **kw) == []
    op._PAVED_NETWORK_CACHE.clear()
    assert _codes(op.build_osm_feature_parts(tmp_path, **kw), "mtbo_10000", 10000)["501.1"] >= 1


def test_mixed_uses_osm_roads_for_dedup_and_road_band(tmp_path):
    # mixed: OSM silnice zahozená v paths_mixed proti ZABAGED se vrátí z paths_osm – dedup chodníků a konec cest u 502.
    import json

    road = _road([(-60, 0), (60, 0)], "road_3")
    crossing = _line([(0, -40), (0, 40)], "footway")
    sidewalk = _line([(-50, 6), (50, 6)], "sidewalk")
    _write_job(tmp_path, [], [crossing, sidewalk])
    (tmp_path / "osm_paths" / "paths_osm.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [road, crossing, sidewalk]}), encoding="utf-8"
    )
    lengths = _path_lengths(tmp_path)
    half = op.drawn_symbol_width_m("road_3", "forest_10000", 10000) / 2.0
    assert lengths.pop("502.2") == pytest.approx(120, abs=0.5)  # OSM silnice vrácena
    assert sum(lengths.values()) == pytest.approx(80 - 2 * half, abs=0.5)  # chodník zahozen, pěšina končí u 502


@pytest.mark.parametrize("gap, two", [(7.2, True), (4.0, False)])
def test_narrow_corridor_draws_original_lines_when_legible(tmp_path, gap, two):
    # Trnkovo nám. (way/1033591396 × way/1033591397, 7,2 m od sebe): úzký koridor odpadne → v lese 1:10000
    # obě čáry (čitelné), těsnější dvojice jen osa. Chodník na okraji odpadlé plochy se nezahodí jako „podél plochy“.
    import json

    a = [(0, 0), (60, 0)]
    b = [(0, gap), (60, gap)]
    _write_job(tmp_path, [_area(0, 0, 60, gap)], [])
    axis = {
        "type": "Feature",
        "properties": {
            "source": "osm",
            "highway": "sidewalk",
            "lines": [{"highway": "sidewalk", "coordinates": a}, {"highway": "sidewalk", "coordinates": b}],
        },
        "geometry": {"type": "LineString", "coordinates": [(0, gap / 2), (60, gap / 2)]},
    }
    (tmp_path / "osm_paths" / op.CORRIDOR_AXES_NAME).write_text(
        json.dumps({"type": "FeatureCollection", "features": [axis]}), encoding="utf-8"
    )
    total = sum(_path_lengths(tmp_path).values())
    assert total == pytest.approx(120 if two else 60, abs=1.0)


def test_corridor_pairs_out_matches_axes():
    axes: list = []
    pairs: list = []
    items = [_walk([(0, 0), (60, 0)], "pedestrian"), _walk([(0, 6), (60, 6)], "pedestrian")]
    op.merge_parallel_pedestrian_to_areas(items, axes_out=axes, pairs_out=pairs)
    assert len(pairs) == len(axes) == 1
    assert {tuple(map(tuple, pts))[0] for pts, _hw in pairs[0]} == {(0, 0), (0, 6)}


def test_mixed_osm_road_band(tmp_path):
    # Strakonická: ZABAGED Ulice na ose OSM 502.1 (mixed) se ořízne o kresbu OSM silnice; sprint beze změny.
    from shapely.geometry import LineString

    _write_job(
        tmp_path,
        [],
        [_road([(0, 0), (100, 0)], "road_4"), _line([(0, 50), (100, 50)], op.bridge_highway("road_3"))],
    )
    band = op.mixed_osm_road_band(tmp_path, "forest_10000", 10000)
    half = op.drawn_symbol_width_m("road_4", "forest_10000", 10000) / 2.0
    assert band.bounds[1] == pytest.approx(-half, abs=0.01)
    assert band.bounds[3] < 50  # most se nepočítá
    assert LineString([(10, 1.9), (90, 1.9)]).difference(band).length == pytest.approx(0, abs=0.01)
    assert op.mixed_osm_road_band(tmp_path, "sprint_4000", 4000) is None


def test_mixed_restores_osm_roads_dropped_against_zabaged(tmp_path):
    # mixed = OSM + detaily ze ZABAGED: OSM ulice zahozená v přípravě proti ZABAGED Ulice (Voskovcova way/25464790)
    # se v lese kreslí z OSM; souběžná užší OSM silnice (OSM×OSM dedup) se nevrací. Sprint beze změny.
    import json

    street = _road([(0, 0), (200, 0)], "road_3")
    service = _road([(0, 0.5), (200, 0.5)], "road_1")  # souběžná užší – v mixed zahozená prefer-wider
    path = _line([(0, 50), (100, 50)], "path")
    _write_job(tmp_path, [], [path, _road([(0, 1), (200, 1)], "road_4")])
    (tmp_path / "osm_paths" / "paths_osm.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [street, service, path]}), encoding="utf-8"
    )
    missing = op.mixed_osm_roads_missing(tmp_path)
    assert missing == []  # obě silnice leží na road_4 z mixed
    _write_job(tmp_path, [], [path])
    (tmp_path / "osm_paths" / "paths_osm.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [street, path]}), encoding="utf-8"
    )
    assert [f["properties"]["highway"] for f in op.mixed_osm_roads_missing(tmp_path)] == ["road_3"]
    assert _path_lengths(tmp_path)["502.2"] == pytest.approx(200, abs=1.0)
    assert "501.30" not in _path_lengths(tmp_path, "sprint_4000", 4000)
    import shutil

    shutil.copy(tmp_path / "osm_paths" / "paths_mixed.geojson", tmp_path / "osm_paths" / "paths.geojson")
    assert len(op.load_osm_path_lines(tmp_path)) == 1
    assert len(op.load_osm_path_lines(tmp_path, corridor_lines=True)) == 2  # + OSM ulice: ZABAGED Ulice pod ní ustoupí


def test_mixed_restored_roads_dedup_parallel(tmp_path):
    # Vrácené OSM silnice mezi sebou: souběžná service 3 m vedle primary (50.03286, 14.38235) se nevrací.
    import json

    primary = _road([(0, 0), (200, 0)], "road_4")
    service = _road([(0, 3), (200, 3)], "road_1")
    _write_job(tmp_path, [], [])
    (tmp_path / "osm_paths" / "paths_osm.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [primary, service]}), encoding="utf-8"
    )
    assert [f["properties"]["highway"] for f in op.mixed_osm_roads_missing(tmp_path)] == ["road_4"]


def test_mixed_restored_wider_road_supersedes_mixed_service(tmp_path):
    # Vrácená širší OSM silnice nahradí souběžnou užší silnici z paths_mixed (kreslí se jen jednou).
    import json

    primary = _road([(0, 0), (200, 0)], "road_4")
    service = _road([(0, 3), (200, 3)], "road_1")
    _write_job(tmp_path, [], [service])
    (tmp_path / "osm_paths" / "paths_osm.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [primary, service]}), encoding="utf-8"
    )
    added, superseded = op.mixed_osm_road_fix(tmp_path)
    assert [f["properties"]["highway"] for f in added] == ["road_4"]
    assert superseded == {op._road_feature_key(service)}
    added[0]["properties"]["highway"] = "x"  # výsledek je v paměti jobu: volající ho nesmí změnit
    superseded.clear()
    added2, superseded2 = op.mixed_osm_road_fix(tmp_path)
    assert [f["properties"]["highway"] for f in added2] == ["road_4"] and superseded2 == {op._road_feature_key(service)}
    lengths = _path_lengths(tmp_path)
    assert sum(lengths.values()) == pytest.approx(200, abs=0.5)


def test_mixed_restored_road_clips_partial_parallel(tmp_path):
    # Souběh jen na části (50.03286): užší service se ořízne o vrácenou primary po úsecích, zbytek zůstane.
    import json

    primary = _road([(0, 0), (300, 0)], "road_4")
    service = _road([(100, 3), (160, 3), (160, 80)], "road_2")
    _write_job(tmp_path, [], [service])
    (tmp_path / "osm_paths" / "paths_osm.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [primary, service]}), encoding="utf-8"
    )
    added, superseded = op.mixed_osm_road_fix(tmp_path)
    assert superseded == {op._road_feature_key(service)}
    hws = sorted(f["properties"]["highway"] for f in added)
    assert hws == ["road_2", "road_4"]
    rest = next(f for f in added if f["properties"]["highway"] == "road_2")
    assert op.polyline_length(rest["geometry"]["coordinates"]) == pytest.approx(80, abs=8)


@pytest.mark.parametrize(
    "narrow, wider",
    [
        # Wassermannova × K Barrandovu: užší silnice vede uzlem, odkud vychází širší nájezd (rozvětvení), 2026-10-27
        ([(0, -100), (0, 100)], [[(0, 0), (30, -40), (80, -60)], [(0, -20), (-40, -50), (-90, -60)]]),
        # souběžná obslužná komunikace napojená šikmo koncem na širší silnici
        ([(50, 0), (60, 8), (200, 8)], [[(-100, 0), (300, 0)]]),
        # křížení v rohu: rameno užší leží v prodloužení ramene širší (Do Klukovic × Tréglova × Werichova)
        ([(0, 100), (0, 0), (-100, -10)], [[(100, 0), (0, 0), (10, -100)]]),
    ],
)
def test_mixed_restored_narrow_road_kept_at_fork(tmp_path, narrow, wider):
    # Rozbíhání u uzlu není souběh: užší silnice se neořízne a navazuje na širší.
    import json

    feats = [_road(narrow, "road_2")] + [_road(w, "road_4") for w in wider]
    _write_job(tmp_path, [], [])
    (tmp_path / "osm_paths" / "paths_osm.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    added, _superseded = op.mixed_osm_road_fix(tmp_path)
    rest = [f for f in added if f["properties"]["highway"] == "road_2"]
    assert sum(op.polyline_length(f["geometry"]["coordinates"]) for f in rest) == pytest.approx(op.polyline_length(narrow), abs=0.5)


def test_path_extension_not_across_other_line(tmp_path):
    # Prodloužení k silnici, které by křížilo jinou kreslenou cestu (ponechaný chodník, schody), se nedělá.
    road = _road([(-100, 0), (100, 0)], "road_3")
    sidewalk = _line([(-100, 7), (100, 7)], "sidewalk")
    path = _line([(0, 7), (0, 40)], "footway")
    cross = _line([(-20, 5.5), (20, 5.5)], "steps")
    _write_job(tmp_path, [], [road, sidewalk, path, cross])
    assert _path_lengths(tmp_path).get("506.0", 0) == pytest.approx(33, abs=0.5)


def test_path_extension_not_across_kept_part_of_dropped_sidewalk(tmp_path):
    # Chodník zahozený jen zčásti: jeho ponechaný krátký úsek u silnice je pro prodloužení cesty překážkou.
    road = _road([(-100, 0), (100, 0)], "road_3")
    dropped = _line([(-100, 7), (100, 7)], "sidewalk")
    partly = _line([(-100, 7.2), (-30, 7.2), (-30, 30), (20, 30), (20, 5), (-10, 5), (-10, 30)], "sidewalk")
    path = _line([(0, 7.5), (0, 40)], "footway")
    _write_job(tmp_path, [], [road, dropped, partly])
    base = _path_lengths(tmp_path).get("506.0", 0)
    _write_job(tmp_path, [], [road, dropped, partly, path])
    assert _path_lengths(tmp_path).get("506.0", 0) - base == pytest.approx(32.5, abs=0.3)


def test_prepare_stores_walk_tunnels_and_service_area(tmp_path, monkeypatch):
    import json

    def way(i, tags, lon0, closed=False):
        g = [{"lat": 50.0345, "lon": lon0}, {"lat": 50.0345, "lon": lon0 + 0.0006}]
        if closed:
            g = g + [{"lat": 50.0349, "lon": lon0 + 0.0006}, {"lat": 50.0349, "lon": lon0}, g[0]]
        return {"type": "way", "id": i, "tags": tags, "geometry": g}

    ski = way(160570380, {"highway": "track", "tracktype": "grade1", "surface": "asphalt", "tunnel": "yes", "layer": "-1"}, 14.372)
    car = way(3, {"highway": "tertiary", "tunnel": "yes", "layer": "-1"}, 14.376)
    yard = way(532655852, {"highway": "service", "area": "yes"}, 14.380, closed=True)
    cutting = way(530615139, {"highway": "track", "tracktype": "grade1", "surface": "asphalt", "layer": "-1"}, 14.384)
    monkeypatch.setattr(op, "fetch_osm_path_elements", lambda *a, **k: [ski, car, yard, cutting])
    op.prepare_osm_paths(tmp_path, (14.37, 50.03, 14.39, 50.04), None, preset_id="sprint_4000", log=lambda m: None)
    walk = json.loads((tmp_path / "osm_paths" / op.WALK_TUNNELS_NAME).read_text(encoding="utf-8"))["features"]
    assert len(walk) == 1  # jen lyžařský podchod, silniční tunel ne
    feats = json.loads((tmp_path / "osm_paths" / "features.geojson").read_text(encoding="utf-8"))["features"]
    assert any(f["properties"]["kind"] == "pedestrian_area" for f in feats)  # dvůr service + area=yes
    paths = json.loads((tmp_path / "osm_paths" / "paths_osm.geojson").read_text(encoding="utf-8"))["features"]
    hws = [f["properties"]["highway"] for f in paths]
    assert "road_2" in hws  # asfaltový track jen s layer=-1 je zářez, ne tunel


@pytest.mark.parametrize(
    "code, preset, major",
    [
        ("502.1", "forest_10000", True),
        ("503", "mtbo_10000", True),  # MTBO kreslí asfaltovou účelovou 502.2: navazuje do výplně, nekončí na okraji
        ("503", "forest_10000", False),  # les 503 je černá čára: končí na okraji kresby silnice
        ("505", "mtbo_10000", False),
    ],
)
def test_major_road_code_by_drawn_symbol(code, preset, major):
    assert op.is_major_road_code(code, preset, 10000) is major
