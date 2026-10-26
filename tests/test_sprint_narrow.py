import json
from unittest.mock import patch

from app.pipeline import dtm_import as di
from app.pipeline import sprint_narrow as sn


def _rect(x0, y0, x1, y1):
    return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]


def _job(tmp_path, sidewalk_polys, lines):
    (tmp_path / "dtm").mkdir()
    (tmp_path / "dtm" / "sidewalks.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in poly] for poly in sidewalk_polys]}), encoding="utf-8"
    )
    (tmp_path / "osm_paths").mkdir()
    feats = [
        {"type": "Feature", "properties": {"highway": hw}, "geometry": {"type": "LineString", "coordinates": [list(p) for p in pts]}}
        for hw, pts in lines
    ]
    gj = json.dumps({"type": "FeatureCollection", "features": feats})
    for name in ("paths_osm.geojson", "paths_mixed.geojson"):
        (tmp_path / "osm_paths" / name).write_text(gj, encoding="utf-8")


def test_wedge_beside_paved_cycleway_replaced_by_line(tmp_path):
    """Klín DTM chodníku 1,4–2,9 m vedle osy cyklostezky (49.5756, 16.0517) → plochu nahradí čára 501.13."""
    wedge, square = _rect(0, 1.4, 60, 2.9), _rect(60, -15, 90, 15)
    _job(tmp_path, [wedge, square], [("cycleway_paved", [(0, 0), (100, 0)])])
    zone = sn.narrow_line_zone(tmp_path, "mixed", "sprint_2_5m", 4000)
    assert zone is not None and zone.contains(sn._area(tmp_path, "mixed").intersection(zone).representative_point())
    assert zone.bounds[2] < 65  # široké náměstí zůstává plochou
    geom = sn.minus_zone(di.sidewalk_geometry(tmp_path, "mixed"), zone)
    assert geom is not None and geom.bounds[0] >= 55  # klín pryč, náměstí zůstává


def test_wide_or_varying_area_stays_area(tmp_path):
    wide = _rect(0, -5, 60, 5)  # 10 m > 4,8 m + 1 m
    _job(tmp_path, [wide], [("cycleway_paved", [(0, 0), (60, 0)])])
    assert sn.narrow_line_zone(tmp_path, "mixed", "sprint_2_5m", 4000) is None
    assert sn.narrow_line_zone(tmp_path, "mixed", "forest_10000", 10000) is None  # jen sprint


def test_narrow_dtm_sidewalk_drawn_as_line(tmp_path):
    """Samostatný chodník z DTM široký 2 m (≤ 2,2 m kresby 501.10 + 1 m) → čára chodníku, ne plocha."""
    from app.pipeline.osm_paths import build_osm_path_parts

    _job(tmp_path, [_rect(0, -1, 50, 1)], [("sidewalk", [(0, 0), (50, 0)])])
    kw = dict(preset_id="sprint_2_5m", scale=4000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    with patch("app.pipeline.osm_paths.symbol_index_for_code", return_value=109):
        parts = build_osm_path_parts(tmp_path, **kw)
    assert sum(p.count for p in parts) == 1
    with patch("app.pipeline.dtm_import._geometry_part", side_effect=lambda geom, **k: [geom] if geom is not None else []):
        assert di.build_dtm_sidewalk_parts(tmp_path, **kw) == []


def test_short_gap_between_runs_is_bridged(tmp_path):
    """Křížení s tunelem (krátké rozšíření do 5 m) nenechá mezi úseky čáry kousek plochy."""
    strip_a, portal, strip_b = _rect(0, 1.4, 40, 2.9), _rect(40, -6, 43, 6), _rect(43, 1.4, 80, 2.9)
    _job(tmp_path, [strip_a, portal, strip_b], [("cycleway_paved", [(0, 0), (80, 0)])])
    zone = sn.narrow_line_zone(tmp_path, "mixed", "sprint_2_5m", 4000)
    geom = sn.minus_zone(di.sidewalk_geometry(tmp_path, "mixed"), zone)
    from shapely.geometry import box

    # v pásu čáry nic nezbude, plocha dál od osy (příjezd k portálu) zůstává
    assert geom is not None and geom.intersection(box(0, -4.8, 80, 4.8)).area < 0.01
