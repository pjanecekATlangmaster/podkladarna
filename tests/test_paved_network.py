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
