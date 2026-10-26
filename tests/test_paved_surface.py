"""Les a MTBO: jedna plocha zpevněného povrchu a okraj z hotových čar (``app.pipeline.paved_surface``)."""

import pytest
from shapely.geometry import Point, box

from app.pipeline import paved_surface as ps
from app.pipeline.dtm_import import JUNCTION_EXTRA_M


def test_unified_only_outside_sprint():
    assert ps.unified("forest_10000") and ps.unified("mtbo_10000")
    assert not ps.unified("sprint_4000") and not ps.unified("")


def test_edge_omit_mouth_is_line_band_only():
    # Silnice (půlka kresby 4 m) kolmo zdola do plochy: okraj se vynechá jen v šířce kresby + 2 × 0,3 m na čele.
    network = box(0, 0, 100, 40)
    zone = ps.edge_omit_zone(network, [([(50.0, -30.0), (50.0, 0.0)], 4.0)], "forest_10000", 10000)
    assert zone is not None
    cut = network.boundary.intersection(zone)
    assert cut.length == pytest.approx(2 * (4.0 + JUNCTION_EXTRA_M), abs=0.1)
    assert zone.contains(Point(50, 0)) and not zone.contains(Point(50, 40)) and not zone.contains(Point(60, 0))


def test_edge_omit_parallel_line_removes_edge_along_it():
    # Silnice souběžná se spodní hranou blíž než mezera čitelnosti: celý souběžný úsek okraje se vynechá;
    # daleká silnice nic.
    network = box(0, 0, 100, 40)
    zone = ps.edge_omit_zone(network, [([(0.0, -5.0), (100.0, -5.0)], 4.0)], "forest_10000", 10000)
    assert zone is not None
    assert network.boundary.intersection(zone).length > 95
    assert zone.contains(Point(50, 0)) and not zone.contains(Point(50, 40))
    assert ps.edge_omit_zone(network, [([(0.0, -25.0), (100.0, -25.0)], 4.0)], "forest_10000", 10000) is None


def test_fill_strip_reaches_border_of_parallel_road():
    # Silnice (půlka kresby 4 m, osa y = -6) souběžná se spodní hranou plochy: mezi hranou (y = 0) a vnější hranou
    # okrajové linky (y = -2) se doplní výplň, aby okraj plochy navázal na linku silnice; kolmá silnice ani pěšina
    # bez výplně pás nedají.
    network = box(0, 0, 100, 40)
    strips = ps.road_fill_strips(network, [([(0.0, -6.0), (100.0, -6.0)], 4.0, 3.0)], "forest_10000", 10000)
    assert strips is not None
    assert strips.bounds == pytest.approx((0.0, -2.0, 100.0, 0.0), abs=0.2)
    assert strips.area == pytest.approx(200.0, rel=0.05)
    assert ps.road_fill_strips(network, [([(50.0, -30.0), (50.0, 0.0)], 4.0, 3.0)], "forest_10000", 10000) is None
    assert ps.road_fill_strips(network, [([(0.0, -6.0), (100.0, -6.0)], 4.0, 0.0)], "forest_10000", 10000) is None


def test_short_edge_piece_between_mouths_is_not_drawn():
    # Dvě ústí na spodní hraně 5 m od sebe: úlomek okraje mezi nimi (pod 0,8 mm) se nekreslí, zbylé dva kusy ano.
    import re

    from shapely.ops import unary_union

    from app.pipeline import dtm_import as di

    network = box(0, 0, 100, 40)
    omit = unary_union([box(30, -1, 38, 1), box(43, -1, 51, 1)])
    kw = dict(preset_id="forest_10000", scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0, clip_bounds=None)
    parts = di._geometry_part(network, name="x", code="501.1", fill_code="501.1", edge_code="501.2", outline_separate=True, edge_omit=omit, edge_only=True, **kw)
    n = sum(len(re.findall(r"<object\b", p.objects_xml)) for p in parts)
    assert n == 2  # bez EDGE_PIECE_MIN_MM by byly tři
    assert di.EDGE_PIECE_MIN_MM * 10 > 5


def test_dtm_area_features_empty_without_dtm(tmp_path):
    assert ps.dtm_area_features(tmp_path, "forest_10000", 10000) == []
    assert ps.dtm_area_features(tmp_path, "sprint_4000", 4000) == []
