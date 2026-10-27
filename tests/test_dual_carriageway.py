"""Les a MTBO: rozdělená vozovka (dva souběžné jednosměrné pásy) jedním symbolem 502.0 (``app.pipeline.dual_carriageway``)."""

import json

import pytest
from shapely.geometry import LineString, Point

from app.pipeline import dual_carriageway as dc
from app.pipeline import osm_paths as op


def _feat(coords, name="K Barrandovu", hw="road_4", roundabout=False):
    return {
        "type": "Feature",
        "properties": {"source": "osm", "highway": hw, "name": name, "roundabout": roundabout, "lanes": "2"},
        "geometry": {"type": "LineString", "coordinates": [[float(x), float(y)] for x, y in coords]},
    }


def test_threshold_from_map_mm():
    assert dc.max_axis_m("forest_10000", 10000) == pytest.approx(13.1)
    assert dc.max_axis_m("forest_10000", 15000) == pytest.approx(19.65)


def test_two_opposite_oneway_lanes_become_one_axis():
    # Dva protisměrné pásy 11,5 m od sebe, 300 m: jedna osa uprostřed; pás původních čar je v zóně.
    a = _feat([(0, 0), (150, 0), (300, 0)])
    b = _feat([(300, 11.5), (150, 11.5), (0, 11.5)])
    axes, zone = dc._compute([a, b], dc.max_axis_m("forest_10000", 10000))
    assert len(axes) == 1
    ln = LineString(axes[0])
    assert ln.length == pytest.approx(300, abs=6)
    assert abs(Point(150, 5.75).distance(ln)) < 0.3
    assert zone.contains(Point(150, 0)) and zone.contains(Point(150, 11.5)) and not zone.contains(Point(150, 30))


def test_short_split_and_far_lanes_are_not_dual():
    # Rozplet 60 m u kruháče a pásy 20 m od sebe se nekreslí 502.0; stejný směr (ne protisměr) také ne.
    short = [_feat([(0, 0), (60, 0)]), _feat([(60, 8), (0, 8)])]
    assert dc._compute(short, 13.1) == ([], None)
    far = [_feat([(0, 0), (300, 0)]), _feat([(300, 20), (0, 20)])]
    assert dc._compute(far, 13.1) == ([], None)
    same_dir = [_feat([(0, 0), (300, 0)]), _feat([(0, 10), (300, 10)])]
    assert dc._compute(same_dir, 13.1) == ([], None)
    # odbočka šikmo od pásu (50°) není protisměrný pás, i když vede do 13 m: směry musí být proti sobě
    turn = [_feat([(0, 0), (300, 0)]), _feat([(300, 0), (200, 10), (0, 10)])]
    assert dc._runs(LineString([(0, 0), (300, 0)]), LineString([(300, 0), (240, 50)]), 13.1) == []
    assert dc._compute(turn, 13.1)[0]  # pás, který se po krátké odbočce vrací souběžně, osu dá


def test_axes_of_split_ways_join_across_gap():
    # Pás A je v OSM rozdělený na dva ways s mezerou 10 m (křižovatka): osa je jedna, souvislá.
    a1 = _feat([(0, 0), (140, 0)])
    a2 = _feat([(150, 0), (300, 0)])
    b = _feat([(300, 11), (0, 11)])
    axes, _zone = dc._compute([a1, a2, b], 13.1)
    assert len(axes) == 1 and LineString(axes[0]).length > 280


def test_dual_axis_drawn_as_502_0_and_lanes_dropped(tmp_path):
    (tmp_path / "osm_paths").mkdir()
    a = [(0.0, 0.0), (150.0, 0.0), (300.0, 0.0)]
    b = [(300.0, 11.5), (150.0, 11.5), (0.0, 11.5)]
    paths = {"type": "FeatureCollection", "features": [_feat(a), _feat(b)]}
    (tmp_path / "osm_paths" / "paths_mixed.geojson").write_text(json.dumps(paths), encoding="utf-8")
    (tmp_path / "osm_paths" / "paths_osm.geojson").write_text(json.dumps(paths), encoding="utf-8")
    (tmp_path / "osm_paths" / op.ONEWAY_ROADS_NAME).write_text(json.dumps(paths), encoding="utf-8")
    (tmp_path / "osm_paths" / "features.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
    op._PAVED_NETWORK_CACHE.clear()
    import re

    import app.pipeline.oom_symbol_map as sm

    for preset in ("forest_10000", "mtbo_10000"):
        parts = op.build_osm_path_parts(tmp_path, preset_id=preset, scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
        id2code = {v: k for k, v in sm._code_to_index(sm.symbol_set_path(preset, 10000)).items()}
        codes = [id2code[int(m.group(1))] for p in parts for m in re.finditer(r'<object\b[^>]*\bsymbol="(\d+)"', p.objects_xml)]
        assert codes == ["502.0"], (preset, codes)  # jen osa 502.0, původní pásy 502.1 ne
    assert op.osm_oom_code("road_dual", "sprint_4000") != "502.0"


def test_side_road_reaches_edge_of_dual_symbol(tmp_path):
    # Boční ulice končící na jižním pásu (y = 0) dojde až k okraji kresby 502.0 (osa y = 5,75, půlka šířky 9,6 m),
    # ne jen k pásu nekresleného jízdního pruhu (Petr 2026-10-26, D23).
    import re

    (tmp_path / "osm_paths").mkdir()
    a = [(0.0, 0.0), (150.0, 0.0), (300.0, 0.0)]
    b = [(300.0, 11.5), (150.0, 11.5), (0.0, 11.5)]
    side = {"type": "Feature", "properties": {"source": "osm", "highway": "road_3"}, "geometry": {"type": "LineString", "coordinates": [[150.0, -60.0], [150.0, 0.0]]}}
    paths = {"type": "FeatureCollection", "features": [_feat(a), _feat(b), side]}
    for name in ("paths_mixed.geojson", "paths_osm.geojson"):
        (tmp_path / "osm_paths" / name).write_text(json.dumps(paths), encoding="utf-8")
    (tmp_path / "osm_paths" / op.ONEWAY_ROADS_NAME).write_text(json.dumps({"type": "FeatureCollection", "features": [_feat(a), _feat(b)]}), encoding="utf-8")
    (tmp_path / "osm_paths" / "features.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
    op._PAVED_NETWORK_CACHE.clear()
    parts = op.build_osm_path_parts(tmp_path, preset_id="forest_10000", scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    half = dc.zone_half_m("forest_10000", 10000)  # půlka výplně 502.0 (8,55 m): konec na okrajové lince
    tops = []
    for p in parts:
        for m in re.finditer(r"<coords[^>]*>(.*?)</coords>", p.objects_xml, re.DOTALL):
            pts = [(int(c.split()[0]) * 0.01, -int(c.split()[1]) * 0.01) for c in m.group(1).split(";") if c.strip()]
            if len(pts) >= 2 and all(abs(x - 150.0) < 0.5 for x, _y in pts):
                tops.append(max(y for _x, y in pts))
    assert tops, "boční ulice se nekreslí"
    assert max(tops) == pytest.approx(5.75 - half, abs=0.6)


def test_axis_not_bridged_through_roundabout():
    # Pásy končí po obou stranách kruhového objezdu (r = 11 m): osa se přes ostrůvek nespojí, končí na prstenci
    # a prstenec zóna neořízne (Lamačova, Petr 2026-10-27).
    import math

    ring = [(11 * math.cos(math.radians(a)), 11 * math.sin(math.radians(a))) for a in range(0, 360, 10)]
    ring.append(ring[0])
    west_a, west_b = _feat([(-311, -5), (-11, -5)]), _feat([(-11, 5), (-311, 5)])
    east_a, east_b = _feat([(11, -5), (311, -5)]), _feat([(311, 5), (11, 5)])
    feats = [west_a, west_b, east_a, east_b, _feat(ring, roundabout=True)]
    islands = dc.roundabout_islands(feats)
    axes, zone = dc._compute(feats[:4], 13.1, 8.4, islands)
    assert len(axes) == 2
    assert all(LineString(a).distance(Point(0, 0)) > 9.0 for a in axes)
    assert not zone.intersects(LineString(ring))
    # bez ostrůvku se osa přes objezd spojí (původní chování)
    assert len(dc._compute(feats[:4], 13.1, 8.4)[0]) == 1
