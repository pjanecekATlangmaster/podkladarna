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


def test_dtm_area_features_empty_without_dtm(tmp_path):
    assert ps.dtm_area_features(tmp_path, "forest_10000", 10000) == []
    assert ps.dtm_area_features(tmp_path, "sprint_4000", 4000) == []
