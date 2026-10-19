import json

from app.pipeline import dtm_import as di
from app.pipeline import dtm_sidewalks as ds


def _rect(x0, y0, x1, y1):
    return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]


def test_unify_drops_osm_inside_dtm_and_keeps_remainder():
    dtm = [_rect(0, 0, 30, 2)]
    inside = [(2.0, 1.0), (28.0, 1.0)]  # celé uvnitř DTM chodníku → duplicitní
    extra = [(30.0, 1.0), (30.0, 21.0)]  # navazuje na konec DTM, DTM ho nezná
    geom = ds.unify_sidewalks(dtm, [inside, extra])
    assert geom.geom_type == "Polygon"  # zbytek se napojil na DTM
    assert geom.bounds[3] > 20.0  # zbytek z OSM je v ploše
    assert geom.area < 30 * 2 + 20 * 1.4 + 5


def test_unify_without_osm_remainder_is_dtm_only():
    geom = ds.unify_sidewalks([_rect(0, 0, 30, 2)], [[(2.0, 1.0), (28.0, 1.0)]])
    assert abs(geom.area - 60.0) < 1.0


def test_unify_filters_short_remainders_and_small_holes():
    ring = _rect(0, 0, 20, 20)
    ring.append([(5, 5), (5.5, 5), (5.5, 5.5), (5, 5.5), (5, 5)])  # díra 0,25 m²
    geom = ds.unify_sidewalks([ring], [[(20.0, 10.0), (21.5, 10.0)]])  # zbytek 1,5 m → zahodit
    assert not list(geom.interiors)
    assert geom.bounds[2] < 20.5


def test_unify_osm_only_when_no_dtm_polygon():
    geom = ds.unify_sidewalks([], [[(0.0, 0.0), (20.0, 0.0)]])
    assert abs(geom.area - 20 * ds.OSM_WIDTH_M) < 3.0


def test_unify_drops_parallel_osm_line_beside_dtm_but_keeps_extension():
    dtm = [_rect(0, 0, 40, 2)]
    parallel = [(2.0, 3.5), (38.0, 3.5)]  # posunutý OSM chodník 1,5 m od plochy
    away = [(40.0, 1.0), (40.0, 31.0)]  # pokračování kolmo pryč
    geom = ds.unify_sidewalks(dtm, [parallel])
    assert abs(geom.area - 80.0) < 1.0  # souběžná čára se nekreslí
    geom = ds.unify_sidewalks(dtm, [parallel, away])
    assert geom.bounds[3] > 30.0 and geom.area < 80 + 30 * 1.4 + 5


def test_clip_outside_cuts_footway_inside_dtm():
    cover = ds.dtm_union([_rect(10, -2, 20, 2)]).buffer(ds.OSM_COVER_M)
    pieces = ds.clip_outside([(0.0, 0.0), (40.0, 0.0)], cover)
    assert len(pieces) == 2
    assert max(x for x, _y in pieces[0]) < 10 and min(x for x, _y in pieces[1]) > 20


def test_is_surface_sidewalk_excludes_bridges_and_steps():
    assert di.is_surface_sidewalk("sidewalk")
    assert not di.is_surface_sidewalk("steps")
    assert not di.is_surface_sidewalk("path")


def test_sidewalks_parts_and_osm_paths_skip_sidewalks(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "sidewalks.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1000, -970, -998)]]}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()
    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"highway": "sidewalk"},
                "geometry": {"type": "LineString", "coordinates": [[-1000, -999], [-970, -999]]},
            },
            {
                "type": "Feature",
                "properties": {"highway": "path"},
                "geometry": {"type": "LineString", "coordinates": [[-1000, -980], [-970, -980]]},
            },
        ],
    }
    (work / "osm_paths" / "paths_mixed.geojson").write_text(json.dumps(gj), encoding="utf-8")
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-985.0, ref_y=-999.0, grivation_deg=0.0)
    parts = di.build_dtm_sidewalk_parts(work, **kw)
    assert [p.name for p in parts] == ["Chodníky (DTM + OSM)"] and parts[0].count == 1
    paths = op.build_osm_path_parts(work, **kw)
    assert paths and paths[0].count == 1  # jen pěšina, chodník nahradila plocha
    assert di.build_dtm_sidewalk_parts(work, **{**kw, "preset_id": "forest_10000"}) == []
