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
    # krátká zalomená čára: buffer okrajové linky je MultiPolygon (job 3082b1ae8292, MTBO)
    hook = [(8.7, -8.5), (8.4, -9.5), (7.3, -9.0)]
    assert ps.road_fill_strips(network, [([(0.0, -6.0), (100.0, -6.0)], 4.0, 3.0), (hook, 4.0, 3.0)], "forest_10000", 10000).area == pytest.approx(200.0, rel=0.05)


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


def test_fill_strips_survive_geos_topology_error(monkeypatch):
    # Job 61ce38ad5427 (MTBO Barrandov): GEOS odmítl sjednocení pásů (TopologyException). Překryv se zopakuje
    # s přichycením na mřížku; když selže i to, dotažení výplně se vynechá a mapa vznikne.
    import shapely
    from shapely.errors import GEOSException

    network = box(0, 0, 100, 40)
    bands = [([(0.0, -6.0), (100.0, -6.0)], 4.0, 3.0)]
    real = shapely.union_all

    def flaky(geoms, grid_size=None, **kw):
        if grid_size is None:
            raise GEOSException("TopologyException: unable to assign free hole to a shell")
        return real(geoms, grid_size=grid_size, **kw)

    monkeypatch.setattr(shapely, "union_all", flaky)
    strips = ps.road_fill_strips(network, bands, "forest_10000", 10000)
    assert strips is not None and strips.area == pytest.approx(200.0, rel=0.05)

    def broken(*a, **kw):
        raise GEOSException("TopologyException")

    monkeypatch.setattr(shapely, "union_all", broken)
    assert ps.road_fill_strips(network, bands, "forest_10000", 10000) is None


@pytest.mark.parametrize(
    "op_name, fail_grids, expect_none",
    [
        ("union_all", {None, 0.01}, False),  # 1 cm selže, 5 cm projde
        ("difference", {None}, False),
        ("difference", {None, 0.01, 0.05}, True),
    ],
)
def test_overlay_retries_on_grid(monkeypatch, caplog, op_name, fail_grids, expect_none):
    import shapely
    from shapely.errors import GEOSException

    real = getattr(shapely, op_name)

    def flaky(*a, grid_size=None, **kw):
        if grid_size in fail_grids:
            raise GEOSException("TopologyException")
        return real(*a, grid_size=grid_size, **kw)

    monkeypatch.setattr(shapely, op_name, flaky)
    out = ps.road_fill_strips(box(0, 0, 100, 40), [([(0.0, -6.0), (100.0, -6.0)], 4.0, 3.0)], "forest_10000", 10000)
    if expect_none:
        assert out is None and "dotažení výplně se vynechá" in caplog.text
    else:
        assert out is not None and out.area == pytest.approx(200.0, rel=0.05)


def test_overlay_survives_make_valid_error(monkeypatch):
    import shapely
    from shapely.errors import GEOSException

    def broken(*a, **kw):
        raise GEOSException("TopologyException")

    monkeypatch.setattr(shapely, "union_all", broken)
    monkeypatch.setattr(shapely, "make_valid", broken)
    assert ps._overlay(shapely.union_all, [box(0, 0, 1, 1)]) is None


# (případ, budova v zálivu [m²], záliv zacelený)
@pytest.mark.parametrize(
    "case, building_m2, closed",
    [
        ("záliv u kiosku 8 m² se zacelí (kiosek je bod 521.3)", 8.0, True),
        ("záliv u budovy 40 m² zůstane (budova je překážka)", 40.0, False),
        ("bez budovy se záliv zacelí", 0.0, True),
    ],
)
def test_generalize_paved_feats_closes_bay_unless_building(tmp_path, case, building_m2, closed):
    import json
    import math

    from shapely.geometry import mapping, shape

    # Parkoviště 60 × 40 m se zálivem 3 m širokým a 3 m hlubokým v horní hraně (obrys OSM kolem kiosku).
    bay = box(28, 37, 31, 40.001)
    parking = box(0, 0, 60, 40).difference(bay)
    feats = [{"type": "Feature", "properties": {"kind": "parking"}, "geometry": mapping(parking)}]
    (tmp_path / "osm_paths").mkdir()
    buildings = []
    if building_m2:
        side = math.sqrt(building_m2)
        b = box(29.5 - side / 2, 38.5 - side / 2, 29.5 + side / 2, 38.5 + side / 2)
        buildings.append({"type": "Feature", "properties": {"kind": "building"}, "geometry": mapping(b)})
    (tmp_path / "osm_paths" / "features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": buildings}), encoding="utf-8"
    )
    out = ps.generalize_paved_feats(tmp_path, feats, "forest_10000", 10000)
    assert len(out) == 1 and out[0]["properties"]["kind"] == "parking", case
    g = shape(out[0]["geometry"])
    assert g.contains(Point(29.5, 38.5)) == closed, case
    assert g.area == pytest.approx(60 * 40 if closed else parking.area, rel=0.03), case  # zaoblení rohů
    # sprint beze změny
    assert ps.generalize_paved_feats(tmp_path, feats, "sprint_4000", 4000) is feats
