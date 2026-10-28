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


@pytest.mark.parametrize("side_hw, into_fill", [("road_3", True), ("road_1", False)])
def test_side_road_reaches_fill_or_edge_of_dual_symbol(tmp_path, side_hw, into_fill):
    # Boční silnice s výplní (road_3 → 502) končící na jižním pásu (y = 0) dojde až k němu, tj. do výplně 502.0 (osa
    # y = 5,75): výplň je nad okrajovými linkami, ústí je čisté (Petr 2026-10-28). Černá cesta (road_1 → 504) končí
    # na okrajové lince 502.0 (půlka výplně od osy; Petr 2026-10-26, D23).
    import re

    (tmp_path / "osm_paths").mkdir()
    a = [(0.0, 0.0), (150.0, 0.0), (300.0, 0.0)]
    b = [(300.0, 11.5), (150.0, 11.5), (0.0, 11.5)]
    side = {"type": "Feature", "properties": {"source": "osm", "highway": side_hw}, "geometry": {"type": "LineString", "coordinates": [[150.0, -60.0], [150.0, 0.0]]}}
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
    if into_fill:
        assert max(tops) == pytest.approx(0.0, abs=0.6)  # až k pásu (0,3 m), hluboko ve výplni 502.0
    else:
        assert max(tops) <= 5.75 - half + 0.6  # nejdál na okrajové lince 502.0 (černá cesta končí na okraji kresby)


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


@pytest.mark.parametrize(
    "cont, other, extended",
    [
        # pásy se slijí v (300, 5.5), obousměrná silnice 36 m do křižovatky (Lamačova × K Barrandovu): osa až do uzlu
        ([[(300, 5.5), (318, 5.5)], [(318, 5.5), (336, 5.5)]], [[(336, -50), (336, 50)]], True),
        # pokračování nekončí na křižovatce (slepý konec) – osa zůstává
        ([[(300, 5.5), (336, 5.5)]], [], False),
        # odbočka do strany (60°) není pokračování
        ([[(300, 5.5), (318, 36.5)]], [[(318, 36.5), (318, 80)], [(318, 36.5), (360, 36.5)]], False),
    ],
)
def test_axis_extended_along_continuation_to_junction(cont, other, extended):
    lanes = [_feat([(0, 0), (290, 0), (300, 5.5)]), _feat([(300, 5.5), (290, 11), (0, 11)])]
    roads = [[(float(x), float(y)) for x, y in c] for c in cont + other]
    axes, zone = dc._compute(lanes, 13.1, None, None, roads)
    assert len(axes) == 1
    far = max(axes[0], key=lambda p: p[0])
    assert (far[0] > 330) is extended
    assert zone.contains(Point(320, 5.5)) is extended


@pytest.mark.parametrize("angle_deg", [90, 60, 45])
def test_continuation_zone_keeps_crossing_road(angle_deg):
    # Osa prodloužená do T-křižovatky: příčná silnice v uzlu (336, 5.5) zůstane celá, i šikmá.
    import math

    lanes = [_feat([(0, 0), (290, 0), (300, 5.5)]), _feat([(300, 5.5), (290, 11), (0, 11)])]
    a = math.radians(angle_deg)
    dx, dy = 60 * math.cos(a), 60 * math.sin(a)
    node = (336.0, 5.5)
    crossing = [(node[0] - dx, node[1] - dy), node, (node[0] + dx, node[1] + dy)]
    roads = [[(300.0, 5.5), node], crossing[:2], crossing[1:]]
    axes, zone = dc._compute(lanes, 13.1, 8.4, None, roads)
    assert max(p[0] for p in axes[0]) > 330
    assert zone.intersection(LineString(crossing)).length < 0.5


def test_axis_not_extended_along_diverging_lane():
    # Rozplet: za koncem osy se pásy rozbíhají (9°) – osa po jednom z nich nepokračuje, i když vede ke křižovatce.
    import math

    t = math.tan(math.radians(9))
    lane_a = _feat([(0, 0), (250, 0), (300, -50 * t)])
    lane_b = _feat([(300, 11 + 50 * t), (250, 11), (0, 11)])
    roads = [
        [(250.0, 0.0), (300.0, -50 * t)],
        [(300.0, 11 + 50 * t), (250.0, 11.0)],
        [(300.0, -60.0), (300.0, 60.0)],
    ]
    axes, _zone = dc._compute([lane_a, lane_b], 13.1, None, None, roads)
    assert len(axes) == 1 and max(p[0] for p in axes[0]) < 270


@pytest.mark.parametrize(
    "arm_in, arm_out, linked, split",
    [
        # Lipského u zastávky: dvě ramena 25 m z vrcholu (0, 25) k hlavní silnici y = 0, konce 8 m od sebe → jedna čára
        ([(-4, 0), (0, 25)], [(0, 25), (4, 0)], True, False),
        # Pražského u objezdu: každé rameno jsou v OSM dva úseky – spojí se, u silnice nezůstane pahýl
        ([(-4, 0), (0, 25)], [(0, 25), (4, 0)], True, True),
        # konce ramen 30 m od sebe (dvě samostatné ulice) – ne
        ([(-15, 0), (0, 25)], [(0, 25), (15, 0)], False, False),
        # ramena ve vrcholu nenavazují protisměrně (obě do vrcholu) – ne
        ([(-4, 0), (0, 25)], [(4, 0), (0, 25)], False, False),
    ],
)
def test_short_fork_drawn_as_one_line(arm_in, arm_out, linked, split):
    def _ways(arm):
        if not split:
            return [arm]
        (x0, y0), (x1, y1) = arm
        m = (x0 + 0.3 * (x1 - x0), y0 + 0.3 * (y1 - y0))
        return [[arm[0], m], [m, arm[1]]]

    feats = [_feat(w, name="Lipského", hw="road_3") for arm in (arm_in, arm_out) for w in _ways(arm)]
    links, zone = dc._forks(feats, 13.1)
    assert bool(links) is linked
    # stejné rozvětvení jiné ulice o 1 km dál se najde také (ramena se párují v rámci ulice)
    far = [[(x + 1000, y) for x, y in arm] for arm in (arm_in, arm_out)]
    both, _z = dc._forks(feats + [_feat(far[0], name="Jiná", hw="road_3"), _feat(far[1], name="Jiná", hw="road_3")], 13.1)
    assert len(both) == (2 if linked else 0)
    if linked:
        (mid, hw), = links
        assert hw == "road_3" and mid[0] == pytest.approx((0, 25)) and abs(mid[-1][0]) < 0.5 and mid[-1][1] < 0.5
        main = LineString([(-50, 0), (50, 0)])
        assert zone.intersection(main).length < 0.5  # hlavní silnice u konců ramen zůstane
        assert zone.intersection(LineString(arm_in)).length > 15  # rameno se nekreslí


@pytest.mark.parametrize(
    "ramp, name, dropped",
    [
        ([(0, 20), (12, 12), (20, 0)], "", True),  # rampa vedle křížení (0, 0) – trojúhelník
        ([(0, 20), (12, 12), (20, 0)], "Jiná", False),  # pojmenovaná jednosměrka je ulice, ne rampa
        ([(0, 20), (2, 2), (20, 0)], "", False),  # vede přes křižovatku (míjí ji o < 5 m)
        ([(0, 70), (60, 60), (70, 0)], "", False),  # konce daleko od křížení
    ],
)
def test_slip_lane_beside_junction_not_drawn(ramp, name, dropped):
    # U Náhonu × K Barrandovu: odbočovací rampa mezi dvěma kříženci se v lese a MTBO nekreslí (Petr 2026-10-28)
    roads = [[(0, -100), (0, 100)], [(-100, 0), (100, 0)]]
    zone = dc._slip_zone([_feat(ramp, name=name)], roads)
    assert (zone is not None and zone.contains(LineString(ramp).interpolate(0.5, normalized=True))) == dropped
    # hlavní silnice se u konců rampy nepřeruší a zbytek rampy u ní je kratší než nejkratší kreslený kus
    assert zone is None or all(zone.intersection(LineString(r)).length < 0.01 for r in roads)
    from app.pipeline.dtm_sidewalks import clip_outside

    assert not dropped or not clip_outside(ramp, zone)


def test_axis_smoothed_at_bay():
    # Záliv zastávky: jeden pás uhne o 2 m na 20 m – osa se nevlní (střed by uhnul o 1 m), konce zůstanou.
    lane_a = _feat([(0, 0), (300, 0)])
    lane_b = _feat([(300, 10), (160, 10), (155, 12), (145, 12), (140, 10), (0, 10)])
    axes, _zone = dc._compute([lane_a, lane_b], 13.1)
    (axis,) = axes
    assert max(abs(y - 5.0) for _x, y in axis) < 0.5
    assert axis[0][0] < 3 and axis[-1][0] > 297  # konce osy zůstanou
