"""Graf OSM silnic a měření napojení v kresbě (``road_graph``)."""

import pytest

pytest.importorskip("shapely")

from app.pipeline.road_graph import build_road_graph, connection_report

MAIN = [(0.0, 0.0), (100.0, 0.0), (200.0, 0.0)]  # průběžná čára, boční ulice v jejím vnitřním bodě
SIDE = [(100.0, 0.0), (100.0, 80.0)]
BRIDGE = [(50.0, -40.0), (50.0, 40.0)]  # kříží bez sdíleného bodu (most) – uzel není


def test_t_junction_on_through_line_is_node():
    g = build_road_graph([(MAIN, "road_2"), (SIDE, "road_3"), (BRIDGE, "road_3")])
    inc = g.incidence()
    degrees = sorted(len(v) for v in inc.values())
    assert degrees.count(3) == 1  # T-křižovatka uprostřed průběžné čáry
    assert not any(n == (50.0, 0.0) for n in g.nodes)  # most bez sdíleného bodu
    assert len(g.edges) == 4  # MAIN rozdělená v (100, 0) na dvě hrany + SIDE + BRIDGE


# (případ, kreslené čáry [(souřadnice, půlka šířky)], díry, napojená)
@pytest.mark.parametrize(
    "case, drawn, holes, connected",
    [
        ("vše napojené", [(MAIN, 4.35), (SIDE, 4.35)], 0, 3),
        ("boční končí 15 m před silnicí", [(MAIN, 4.35), ([(100.0, 15.0), (100.0, 80.0)], 4.35)], 1, 2),
        ("boční končí na okraji kresby", [(MAIN, 4.35), ([(100.0, 4.5), (100.0, 80.0)], 4.35)], 0, 3),
        ("boční se nekreslí", [(MAIN, 4.35)], 0, 2),
    ],
)
def test_connection_report(case, drawn, holes, connected):
    g = build_road_graph([(MAIN, "road_2"), (SIDE, "road_3")])
    rep = connection_report(g, drawn)
    assert rep["nodes"] == 1
    assert rep["holes"] == holes, case
    assert rep["connected"] == connected, case


# (případ, boční: konec y, s výplní; hlavní s výplní; špatné ústí) – hlavní: půlka šířky 4,35 m, půlka výplně 3,5 m
@pytest.mark.parametrize(
    "case, end_y, side_major, main_major, bad",
    [
        ("výplň do výplně", 1.0, True, True, None),
        ("výplň na okrajové lince", 4.3, True, True, "výplň mimo výplň"),
        ("černá na okraji výplně", 4.3, False, True, None),
        ("černá ve výplni", 0.0, False, True, "černá ve výplni"),
        ("na černou k ose", 0.0, True, False, None),
        ("na černou jen k okraji", 4.3, True, False, "mimo osu černé"),
    ],
)
def test_mouth_rules(case, end_y, side_major, main_major, bad):
    g = build_road_graph([(MAIN, "road_2"), (SIDE, "road_3")])
    drawn = [(MAIN, 4.35, 3.5, main_major), ([(100.0, end_y), (100.0, 80.0)], 4.35, 3.5, side_major)]
    rep = connection_report(g, drawn)
    assert rep["holes"] == 0, case
    kinds = [m["kind"] for m in rep["bad_mouth_list"]]
    assert kinds == ([bad] if bad else []), case


def test_end_on_area_is_not_hole():
    from shapely.geometry import box

    g = build_road_graph([(MAIN, "road_2"), (SIDE, "road_3")])
    drawn = [(MAIN, 4.35), ([(100.0, 15.0), (100.0, 80.0)], 4.35)]
    rep = connection_report(g, drawn, areas=box(90, 5, 110, 15))
    assert (rep["holes"], rep["on_area"]) == (0, 1)
