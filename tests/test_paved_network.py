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
    # MTBO sada 501.2 v katalogu nemá → ISOM 0,14 mm.
    assert op.paved_edge_width_m("mtbo", 15000) == pytest.approx(2.1)


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
    parts = op.build_osm_feature_parts(tmp_path, preset_id=preset, scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    cnt = _codes(parts, preset, 10000)
    if preset.startswith("forest"):
        assert cnt["501.1"] == 1 and cnt["501.2"] == 1  # jedna plocha, jeden okraj
    else:
        assert sum(cnt.values()) == 1


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


def test_sprint_road_over_parking_unchanged(tmp_path):
    road = _line([(-50, 20), (90, 20)], "road_2")
    _write_job(tmp_path, [_area(0, 0, 40, 40, "parking")], [road])
    lengths = _path_lengths(tmp_path, preset="sprint_2m", scale=4000)
    assert sum(lengths.values()) == pytest.approx(140, abs=1.0)


def _edge_length(parts, preset="forest_10000", scale=10000):
    import re

    import app.pipeline.oom_symbol_map as sm

    edge_id = sm._code_to_index(sm.symbol_set_path(preset, scale))["501.2"]
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
    op._PAVED_NETWORK_CACHE.clear()
    parts = op.build_osm_feature_parts(tmp_path, preset_id="forest_10000", scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    length = _edge_length(parts)
    assert length < 160 - 30  # spodní hrana (40 m) a kousky boků chybí
    assert length > 100  # horní hrana a většina boků zůstává
    # Bez silnice celý obvod.
    _write_job(tmp_path, [_area(0, 0, 40, 40, "parking")], [])
    parts = op.build_osm_feature_parts(tmp_path, preset_id="forest_10000", scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    assert _edge_length(parts) == pytest.approx(160, abs=1.0)


def test_edge_kept_along_road_inside_parking(tmp_path):
    # Jízdní pruh uvnitř parkoviště (nekreslí se) okraj nemaže; vjezd ano jen kolem napojení.
    road = _line([(-50, 20), (90, 20)], "road_2")
    _write_job(tmp_path, [_area(0, 0, 40, 40, "parking")], [road])
    parts = op.build_osm_feature_parts(tmp_path, preset_id="forest_10000", scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    length = _edge_length(parts)
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

    assert width_line_code("road_2", 8.0, "503") == "502.1"
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
    assert lengths["502.2"] == pytest.approx(200, abs=1.0)  # generátor 502 = v sadě 502.2 (5–7 m)
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


def test_mtbo_ignores_dtm_widths(tmp_path):
    road = _line([(0, 0), (200, 0)], "road_2")
    _write_job(tmp_path, [], [road])
    _write_dtm(tmp_path, "roads.json", [(-5, -5, 205, 5)])
    lengths = _path_lengths(tmp_path, preset="mtbo_10000")
    assert len(lengths) == 1  # jeden symbol podle tagů, DTM se v MTBO nepoužívá
