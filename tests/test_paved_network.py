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
