import io
import json
from unittest.mock import patch

import urllib.error

from app.pipeline.osm_paths import (
    OVERPASS_URLS,
    _way_skip_reason,
    build_osm_feature_parts,
    build_osm_path_parts,
    classify_osm_feature,
    fetch_osm_path_elements,
    filter_osm_against_zabaged,
    osm_feature_to_5514,
    overlap_fraction,
    parse_osm_api_map_xml,
    polyline_length,
    sample_polyline,
    osm_way_to_5514,
    unique_polyline_parts,
    _SegmentIndex,
)


def test_skip_sidewalk_and_crossing():
    # Les / default: chodník i přejezd pryč.
    assert _way_skip_reason({"highway": "footway", "footway": "sidewalk"})
    assert _way_skip_reason({"highway": "footway", "footway": "crossing"})
    # Sprint: chodník bereme, přejezd ne.
    assert (
        _way_skip_reason(
            {"highway": "footway", "footway": "sidewalk"}, allow_sidewalk=True
        )
        is None
    )
    assert _way_skip_reason(
        {"highway": "footway", "footway": "crossing"}, allow_sidewalk=True
    )
    assert _way_skip_reason({"highway": "path"}) is None
    assert _way_skip_reason({"highway": "footway"}) is None
    assert _way_skip_reason({"highway": "track"}) is None
    assert _way_skip_reason({"highway": "bridleway"}) is None
    assert _way_skip_reason({"highway": "cycleway"}) is None
    assert _way_skip_reason({"highway": "cycleway", "foot": "no"})


def test_classify_osm_well_and_playground():
    assert classify_osm_feature({"man_made": "water_well", "building": "yes"}) == (
        "water_well_building",
        "521",
    )
    assert classify_osm_feature({"man_made": "water_well"}) == ("water_well", "311")
    assert classify_osm_feature({"amenity": "fountain"}) == ("water_well", "311")
    assert classify_osm_feature({"natural": "spring"}) == ("spring", "312")
    assert classify_osm_feature({"leisure": "playground"}) == ("playground", "501")
    assert classify_osm_feature(
        {"leisure": "pitch", "sport": "basketball"}
    ) == ("pitch", "501")
    assert classify_osm_feature({"leisure": "track"}) == ("pitch", "501")
    assert classify_osm_feature({"leisure": "sports_centre"}) == ("pitch", "501")
    assert classify_osm_feature(
        {"leisure": "sports_centre", "building": "yes"}
    ) == ("building", "521")
    assert classify_osm_feature({"leisure": "ice_rink"}) == ("pitch", "501")
    assert classify_osm_feature({"leisure": "multi"}) == ("pitch", "501")
    assert classify_osm_feature({"leisure": "pitch"}, geom="node") is None
    assert classify_osm_feature({"amenity": "bench"}) == ("bench", "531")
    assert classify_osm_feature({"highway": "street_lamp"}) == ("lamp", "530")
    assert classify_osm_feature({"tourism": "information", "information": "board"}) == (
        "info_board",
        "531",
    )
    assert classify_osm_feature({"leisure": "firepit"}) == ("firepit", "531")
    assert classify_osm_feature({"amenity": "bbq"}) == ("firepit", "531")
    assert classify_osm_feature(
        {"playground": "swing"}, geom="node"
    ) == ("playground_equipment", "531")
    assert classify_osm_feature(
        {"leisure": "playground"}, geom="node"
    ) == ("playground_equipment", "531")
    assert classify_osm_feature(
        {"playground": "climbingframe"}, geom="node"
    ) == ("playground_equipment", "531")
    assert classify_osm_feature({"playground": "sandpit"}, geom="way") is None
    assert classify_osm_feature({"barrier": "fence"}) == ("fence", "518")
    assert classify_osm_feature({"barrier": "wall"}) == ("wall", "513.2")
    assert classify_osm_feature({"barrier": "gate"}, geom="node") == (
        "barrier_point",
        "531",
    )
    assert classify_osm_feature({"historic": "memorial"}, geom="node") == (
        "memorial",
        "526",
    )
    assert classify_osm_feature({"amenity": "shelter"}) == ("shelter", "522")
    assert classify_osm_feature({"leisure": "fitness_station"}) == ("fitness", "531")
    assert classify_osm_feature(
        {"natural": "tree", "denotation": "landmark"}, geom="node"
    ) == ("landmark_tree", "417")
    assert classify_osm_feature({"natural": "wetland"}) == ("wetland", "308")
    assert classify_osm_feature(
        {"landuse": "reservoir"}
    ) == ("water_body", "301")
    assert classify_osm_feature(
        {"natural": "water", "water": "reservoir"}
    ) == ("water_body", "301")
    assert classify_osm_feature(
        {"natural": "water", "water": "basin"}
    ) == ("water_body", "301")
    assert classify_osm_feature({"natural": "water"}) == ("water_body", "301")
    assert classify_osm_feature({"landuse": "basin"}) == ("water_body", "301")
    assert classify_osm_feature({"landuse": "farmland"}) == ("farmland", "412")
    assert classify_osm_feature({"leisure": "garden"}) == ("garden", "520")
    assert classify_osm_feature({"leisure": "garden"}, geom="node") is None
    assert classify_osm_feature({"natural": "cave_entrance"}) == (
        "cave_entrance",
        "203.1",
    )
    # Dřevěný chodník → cesta, ne samostatný feature.
    assert classify_osm_feature({"highway": "footway", "footway": "boardwalk"}) is None
    assert classify_osm_feature({"man_made": "boardwalk"}) is None
    assert classify_osm_feature({"highway": "path"}) is None
    # way/1097076511 kavárna + way/1097076512 building=yes → 521
    assert classify_osm_feature({"building": "yes"}) == ("building", "521")
    assert classify_osm_feature(
        {"amenity": "cafe", "building": "yes", "cuisine": "coffee_shop"}
    ) == ("building", "521")
    assert classify_osm_feature(
        {"tourism": "information", "building": "yes"}
    ) == ("building", "521")
    assert classify_osm_feature({"building": "yes"}, geom="node") is None


def test_skip_subway():
    assert _way_skip_reason({"railway": "subway"}) == "metro"
    assert _way_skip_reason({"highway": "path", "railway": "subway"}) == "metro"
    assert osm_feature_to_5514(
        {
            "type": "way",
            "tags": {"railway": "subway"},
            "geometry": [{"lat": 50.0, "lon": 14.4}, {"lat": 50.001, "lon": 14.4}],
        }
    ) is None


def test_garden_multipolygon_keeps_inner_hole():
    from app.pipeline.osm_paths import osm_area_polygons_5514

    # Mimic Overpass out geom for relation 14172772 (outer + inner).
    el = {
        "type": "relation",
        "tags": {"leisure": "garden", "type": "multipolygon"},
        "members": [
            {
                "type": "way",
                "role": "outer",
                "geometry": [
                    {"lat": 50.0, "lon": 14.40},
                    {"lat": 50.0, "lon": 14.41},
                    {"lat": 50.01, "lon": 14.41},
                    {"lat": 50.01, "lon": 14.40},
                    {"lat": 50.0, "lon": 14.40},
                ],
            },
            {
                "type": "way",
                "role": "inner",
                "geometry": [
                    {"lat": 50.002, "lon": 14.402},
                    {"lat": 50.002, "lon": 14.404},
                    {"lat": 50.004, "lon": 14.404},
                    {"lat": 50.004, "lon": 14.402},
                    {"lat": 50.002, "lon": 14.402},
                ],
            },
        ],
    }
    polys = osm_area_polygons_5514(el)
    assert len(polys) == 1
    assert len(polys[0]) == 2
    assert polys[0][0][0] == polys[0][0][-1]
    assert polys[0][1][0] == polys[0][1][-1]


def test_boardwalk_maps_as_path():
    assert _way_skip_reason({"highway": "footway", "footway": "boardwalk"}) is None
    assert _way_skip_reason({"man_made": "boardwalk"}) is None
    el = {
        "tags": {"highway": "footway", "footway": "boardwalk"},
        "geometry": [{"lat": 50.0, "lon": 14.4}, {"lat": 50.001, "lon": 14.4}],
    }
    pts = osm_way_to_5514(el)
    assert pts is not None
    assert len(pts) >= 2


def test_feature_oom_code_preset():
    from app.pipeline.osm_paths import feature_oom_code

    assert feature_oom_code("cave_entrance", "sprint_2m") == "203.1"
    assert feature_oom_code("cave_entrance", "forest_10000") == "203.2"
    assert feature_oom_code("spring", "forest_10000") == "312"
    assert feature_oom_code("lamp", "sprint_2m") == "530"
    assert feature_oom_code("bench", "sprint_2m") == "531"
    assert feature_oom_code("firepit", "forest_10000") == "531"
    assert feature_oom_code("playground_equipment", "forest_7500") == "531"
    assert feature_oom_code("fence", "sprint_2m") == "518"
    assert feature_oom_code("fence", "forest_10000") == "516"
    assert feature_oom_code("wall", "sprint_2m") == "513.2"
    assert feature_oom_code("hedge", "forest_10000") == "416"
    assert feature_oom_code("playground", "sprint_2m") == "501"
    assert feature_oom_code("playground", "forest_7500") == "501.1"
    assert feature_oom_code("pitch", "sprint_2m") == "501"
    assert feature_oom_code("pitch", "forest_10000") == "501.1"
    assert feature_oom_code("garden", "sprint_2m") == "520"
    assert feature_oom_code("garden", "forest_10000") == "520"
    assert feature_oom_code("water_body", "sprint_2m") == "301"
    assert feature_oom_code("farmland", "forest_7500") == "412"


def test_point_in_ring_and_farmland_dedup():
    from app.pipeline.osm_paths import (
        _point_in_ring,
        filter_osm_area_features_against_zabaged,
    )

    square = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0), (0.0, 0.0)]
    assert _point_in_ring(50.0, 50.0, square)
    assert not _point_in_ring(150.0, 50.0, square)

    # Bez ZABAGED ZIP se plochy nechají.
    feat_in = {
        "type": "Feature",
        "properties": {"kind": "farmland", "oom_code": "412"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[10.0, 10.0], [20.0, 10.0], [20.0, 20.0], [10.0, 20.0], [10.0, 10.0]]
            ],
        },
    }
    kept, dropped = filter_osm_area_features_against_zabaged([feat_in], None)
    assert dropped == 0 and kept == [feat_in]


def test_osm_priority_overpass_includes_barriers():
    from app.pipeline.osm_paths import _overpass_ql

    ql = _overpass_ql(50.0, 14.0, 50.1, 14.1, osm_priority=True)
    assert "barrier" in ql
    assert "fitness_station" in ql
    assert 'way["building"]' in ql
    assert 'relation["type"="multipolygon"]["building"]' in ql
    assert "playground" in ql
    assert "pitch" in ql
    assert "sports_centre" in ql
    assert "ice_rink" in ql
    assert "reservoir" in ql
    assert "farmland" in ql
    assert 'natural"="water"' in ql
    assert 'amenity"="bench"' not in ql
    assert "street_lamp" not in ql
    assert 'node["playground"]' not in ql
    ql_eq = _overpass_ql(
        50.0, 14.0, 50.1, 14.1, include_playground_equipment=True
    )
    assert 'node["playground"]' in ql_eq
    ql_b = _overpass_ql(50.0, 14.0, 50.1, 14.1, include_benches=True)
    assert 'amenity"="bench"' in ql_b
    ql_l = _overpass_ql(50.0, 14.0, 50.1, 14.1, include_lamps=True)
    assert "street_lamp" in ql_l
    ql_off = _overpass_ql(50.0, 14.0, 50.1, 14.1, osm_priority=False)
    assert "fitness_station" not in ql_off
    assert "barrier" not in ql_off
    assert 'way["building"]' not in ql_off

def test_highway_to_zabaged_vrstva():
    from app.pipeline.osm_paths import highway_to_zabaged_vrstva, paths_geojson_for_kp

    assert highway_to_zabaged_vrstva("track") == "Cesta"
    assert highway_to_zabaged_vrstva("path") == "Pesina"
    assert highway_to_zabaged_vrstva("footway") == "Pesina"
    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"highway": "track"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[0, 0], [10, 0]],
                },
            },
            {
                "type": "Feature",
                "properties": {"highway": "path"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[0, 1], [10, 1]],
                },
            },
        ],
    }
    out = paths_geojson_for_kp(gj)
    assert len(out["features"]) == 2
    assert out["features"][0]["properties"]["vrstva"] == "Cesta"
    assert out["features"][1]["properties"]["vrstva"] == "Pesina"


def test_osm_oom_code_paths_only():
    from app.pipeline.osm_paths import osm_oom_code

    assert osm_oom_code("path", "sprint_2m") == "507"
    assert osm_oom_code("track", "sprint_2m") == "506"
    assert osm_oom_code("track", "forest_10000") == "504"
    assert osm_oom_code("steps", "sprint_2m") == "532.7"
    assert osm_oom_code("steps", "forest_10000") == "532"


def test_dedup_osm_prefers_steps_over_path():
    from app.pipeline.osm_paths import dedup_osm_prefer_wider

    steps = ([(0.0, 0.0), (200.0, 0.0)], "steps")
    path = ([(0.0, 2.0), (200.0, 2.0)], "path")
    kept, dropped = dedup_osm_prefer_wider([path, steps])
    assert dropped == 1
    assert len(kept) == 1
    assert kept[0][1] == "steps"


def test_filter_keeps_steps_on_zabaged():
    from app.pipeline.osm_paths import filter_osm_items_against_zabaged

    zab = [[(0.0, 0.0), (100.0, 0.0)]]
    steps = ([(1.0, 1.0), (80.0, 2.0)], "steps")
    path = ([(1.0, 1.0), (80.0, 2.0)], "path")
    kept_s, drop_s = filter_osm_items_against_zabaged([steps], zab)
    kept_p, drop_p = filter_osm_items_against_zabaged([path], zab)
    assert drop_s == 0 and len(kept_s) == 1 and kept_s[0][1] == "steps"
    assert drop_p >= 1 and not kept_p


def test_filter_zabaged_yields_to_osm_centerline():
    """Sprint OOM: ZABAGED Pesina ustoupí stejné OSM střednici."""
    from app.pipeline.osm_paths import filter_lines_against_centerlines

    osm = [[(0.0, 0.0), (200.0, 0.0)]]
    zab_dup = [[(1.0, 1.0), (180.0, 2.0)]]
    zab_new = [[(0.0, 40.0), (100.0, 40.0)]]
    kept, dropped = filter_lines_against_centerlines(zab_dup + zab_new, osm)
    assert dropped >= 1
    assert any(abs(pt[1] - 40) < 1 for line in kept for pt in line)
    assert not any(abs(pt[1]) < 5 for line in kept for pt in line)


def test_load_osm_path_lines(tmp_path):
    from app.pipeline.osm_paths import load_osm_path_lines

    dest = tmp_path / "osm_paths"
    dest.mkdir()
    (dest / "paths.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"highway": "path"},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[0, 0], [10, 0]],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    lines = load_osm_path_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0][0] == (0.0, 0.0)

def test_paths_geojson_for_kp_skips_steps():
    from app.pipeline.osm_paths import highway_to_zabaged_vrstva, paths_geojson_for_kp

    assert highway_to_zabaged_vrstva("steps") is None
    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"highway": "steps"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[0, 0], [10, 0]],
                },
            },
            {
                "type": "Feature",
                "properties": {"highway": "path"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[0, 1], [10, 1]],
                },
            },
        ],
    }
    out = paths_geojson_for_kp(gj)
    assert len(out["features"]) == 1
    assert out["features"][0]["properties"]["highway"] == "path"


def test_osm_bench_to_point():
    el = {
        "type": "node",
        "tags": {"amenity": "bench"},
        "lat": 50.083,
        "lon": 14.325,
    }
    kind, code, pts = osm_feature_to_5514(el)
    assert kind == "bench"
    assert code == "531"
    assert len(pts) == 1


def test_osm_feature_closed_well_building():
    el = {
        "type": "way",
        "tags": {"building": "yes", "man_made": "water_well"},
        "geometry": [
            {"lat": 50.083, "lon": 14.325},
            {"lat": 50.0831, "lon": 14.325},
            {"lat": 50.0831, "lon": 14.3252},
            {"lat": 50.083, "lon": 14.3252},
            {"lat": 50.083, "lon": 14.325},
        ],
    }
    kind, code, pts = osm_feature_to_5514(el)
    assert kind == "water_well_building"
    assert code == "521"
    assert len(pts) >= 4
    assert pts[0] == pts[-1]


def test_dedup_osm_prefers_track_over_path():
    from app.pipeline.osm_paths import dedup_osm_prefer_wider

    track = ([(0.0, 0.0), (200.0, 0.0)], "track")
    path = ([(0.0, 2.0), (200.0, 2.0)], "path")
    kept, dropped = dedup_osm_prefer_wider([path, track])
    assert dropped == 1
    assert len(kept) == 1
    assert kept[0][1] == "track"


def test_dedup_osm_keeps_parallel_distinct():
    from app.pipeline.osm_paths import dedup_osm_prefer_wider

    track = ([(0.0, 0.0), (200.0, 0.0)], "track")
    path = ([(0.0, 20.0), (200.0, 20.0)], "path")
    kept, dropped = dedup_osm_prefer_wider([path, track])
    assert dropped == 0
    assert len(kept) == 2


def test_osm_way_to_5514_skips_sidewalk():
    el = {
        "tags": {"highway": "footway", "footway": "sidewalk"},
        "geometry": [{"lat": 50.0, "lon": 14.4}, {"lat": 50.001, "lon": 14.4}],
    }
    assert osm_way_to_5514(el) is None
    assert osm_way_to_5514(el, allow_sidewalk=True) is not None


def test_filter_keeps_sidewalk_on_zabaged():
    from app.pipeline.osm_paths import filter_osm_items_against_zabaged

    zab = [[(0.0, 0.0), (100.0, 0.0)]]
    sidewalk = ([(1.0, 1.0), (80.0, 2.0)], "sidewalk")
    kept, dropped = filter_osm_items_against_zabaged([sidewalk], zab)
    assert dropped == 0 and len(kept) == 1 and kept[0][1] == "sidewalk"


def test_osm_oom_code_sidewalk():
    from app.pipeline.osm_paths import (
        highway_to_zabaged_vrstva,
        osm_oom_code,
        sprint_line_highway,
    )

    assert osm_oom_code("sidewalk", "sprint_2m") == "501.6"
    assert osm_oom_code("sidewalk", "forest_10000") == "507"
    assert highway_to_zabaged_vrstva("sidewalk") == "Pesina"
    # way/613443110: footway + asphalt bez footway=sidewalk → sprint chodník.
    assert (
        sprint_line_highway(
            {"highway": "footway", "surface": "asphalt"}, "footway"
        )
        == "sidewalk"
    )
    assert sprint_line_highway({"highway": "footway"}, "footway") == "footway"


def test_filter_drops_line_on_zabaged():
    zab = [[(0.0, 0.0), (100.0, 0.0)]]
    osm_dup = [[(1.0, 1.0), (80.0, 2.0)]]
    osm_new = [[(0.0, 80.0), (40.0, 80.0)]]
    kept, dropped = filter_osm_against_zabaged(osm_dup + osm_new, zab)
    assert dropped >= 1
    assert any(abs(pt[1] - 80) < 1 for line in kept for pt in line)


def test_filter_drops_coincident_centerline():
    """OSM prakticky přes ZABAGED (stejná střednice) musí zmizet."""
    zab = [[(0.0, 0.0), (200.0, 0.0)]]
    osm = [[(0.0, 2.0), (200.0, 2.0)]]
    kept, dropped = filter_osm_against_zabaged(osm, zab)
    assert dropped == 1
    assert kept == []


def test_filter_keeps_parallel_distinct_path():
    """Paralelní pěšina ~15 m vedle silnice není duplicita střednice – nechat."""
    zab = [[(0.0, 0.0), (200.0, 0.0)]]
    osm = [[(0.0, 15.0), (200.0, 15.0)]]
    kept, dropped = filter_osm_against_zabaged(osm, zab)
    assert dropped == 0
    assert len(kept) == 1


def test_filter_keeps_partial_overlap_whole_way():
    """Částečný souběh se silnicí ≠ shodná cesta – celou OSM linii nechat (vč. konců)."""
    zab = [[(0.0, 0.0), (100.0, 0.0)]]
    # 80 m po silnici, pak 40 m do lesa (~67 % cover < COVER_DROP).
    osm = [[(0.0, 1.0), (80.0, 1.0), (80.0, 41.0)]]
    kept, dropped = filter_osm_against_zabaged(osm, zab)
    assert dropped == 0
    assert len(kept) == 1
    assert kept[0] == osm[0]
    assert abs(polyline_length(kept[0]) - 120.0) < 1e-6


def test_unique_parts_splits_middle_overlap():
    index = _SegmentIndex()
    index.add_line([(40.0, 0.0), (60.0, 0.0)])
    line = [(0.0, 0.0), (100.0, 0.0)]
    parts = unique_polyline_parts(line, index, near_m=6, sample_m=5)
    assert len(parts) == 2
    assert polyline_length(parts[0]) > 12
    assert polyline_length(parts[1]) > 12


def test_overlap_high_when_coincident():
    index = _SegmentIndex()
    index.add_line([(0.0, 0.0), (100.0, 0.0)])
    frac = overlap_fraction([(0.0, 1.0), (100.0, 1.0)], index, near_m=6)
    assert frac > 0.9


def test_polyline_length_and_sample():
    pts = [(0.0, 0.0), (30.0, 0.0)]
    assert abs(polyline_length(pts) - 30) < 1e-6
    samples = sample_polyline(pts, 10)
    assert len(samples) >= 3


def test_fetch_osm_tries_next_mirror_after_504():
    payload = {
        "elements": [
            {
                "type": "way",
                "geometry": [{"lat": 50.0, "lon": 14.4}, {"lat": 50.001, "lon": 14.4}],
                "tags": {"highway": "path"},
            }
        ]
    }
    calls: list[str] = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                req.full_url, 504, "Gateway Timeout", hdrs=None, fp=None
            )
        return _Resp(json.dumps(payload).encode("utf-8"))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        got = fetch_osm_path_elements((14.4, 50.0, 14.41, 50.01))
    assert len(got) == 1
    assert calls[0] == OVERPASS_URLS[0]
    assert calls[1] == OVERPASS_URLS[1]


def test_parse_osm_api_map_xml_keeps_paths_only():
    xml = """<?xml version="1.0"?>
    <osm>
      <node id="1" lat="50.0" lon="14.4"/>
      <node id="2" lat="50.001" lon="14.4"/>
      <node id="3" lat="50.002" lon="14.401"/>
      <way id="10">
        <nd ref="1"/><nd ref="2"/>
        <tag k="highway" v="path"/>
      </way>
      <way id="11">
        <nd ref="2"/><nd ref="3"/>
        <tag k="highway" v="residential"/>
      </way>
      <way id="12">
        <nd ref="1"/><nd ref="3"/>
        <tag k="highway" v="footway"/><tag k="footway" v="sidewalk"/>
      </way>
      <way id="13">
        <nd ref="1"/><nd ref="2"/>
        <tag k="highway" v="track"/>
      </way>
    </osm>
    """
    got = parse_osm_api_map_xml(xml)
    assert len(got) == 3
    assert {e["tags"]["highway"] for e in got} == {"path", "footway", "track"}
    assert len(got[0]["geometry"]) == 2


def test_fetch_osm_falls_back_to_api_map():
    osm_xml = """<?xml version="1.0"?>
    <osm>
      <node id="1" lat="50.0" lon="14.4"/>
      <node id="2" lat="50.001" lon="14.4"/>
      <way id="10">
        <nd ref="1"/><nd ref="2"/>
        <tag k="highway" v="path"/>
      </way>
    </osm>
    """
    calls: list[str] = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        if "api.openstreetmap.org" in req.full_url:
            return _Resp(osm_xml.encode("utf-8"))
        raise urllib.error.HTTPError(
            req.full_url, 504, "Gateway Timeout", hdrs=None, fp=None
        )

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        got = fetch_osm_path_elements((14.4, 50.0, 14.41, 50.01))
    assert len(got) == 1
    assert got[0]["tags"]["highway"] == "path"
    assert any("api.openstreetmap.org" in u for u in calls)
    assert len(calls) == len(OVERPASS_URLS) + 1


def _write_osm_geojson(work_dir, name, features):
    out = work_dir / "osm_paths"
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(
        json.dumps({"type": "FeatureCollection", "features": features}),
        encoding="utf-8",
    )


def _build_kwargs():
    return dict(preset_id="isom2017", scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)


def test_osm_paths_are_clipped_to_map_bounds(tmp_path):
    # Cesta vede z výřezu ven; po ořezu má zůstat jen kus uvnitř.
    _write_osm_geojson(
        tmp_path,
        "paths.geojson",
        [
            {
                "type": "Feature",
                "properties": {"highway": "path"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-500.0, 0.0], [500.0, 0.0]],
                },
            }
        ],
    )
    with patch("app.pipeline.osm_paths.symbol_index_for_code", return_value=7):
        full = build_osm_path_parts(tmp_path, **_build_kwargs())
        clipped = build_osm_path_parts(
            tmp_path, **_build_kwargs(), clip_bounds=(-100.0, -100.0, 100.0, 100.0)
        )
    assert full and clipped
    assert clipped[0].count == 1
    assert "-50000 0;50000 0;" in full[0].objects_xml
    assert "-10000 0;10000 0;" in clipped[0].objects_xml


def test_osm_path_fully_outside_bounds_is_dropped(tmp_path):
    _write_osm_geojson(
        tmp_path,
        "paths.geojson",
        [
            {
                "type": "Feature",
                "properties": {"highway": "path"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[400.0, 400.0], [500.0, 500.0]],
                },
            }
        ],
    )
    with patch("app.pipeline.osm_paths.symbol_index_for_code", return_value=7):
        got = build_osm_path_parts(
            tmp_path, **_build_kwargs(), clip_bounds=(-100.0, -100.0, 100.0, 100.0)
        )
    assert got == []


def test_osm_point_feature_outside_bounds_is_dropped(tmp_path):
    _write_osm_geojson(
        tmp_path,
        "features.geojson",
        [
            {
                "type": "Feature",
                "properties": {"kind": "spring", "oom_code": "312"},
                "geometry": {"type": "Point", "coordinates": [inside, 0.0]},
            }
            for inside in (0.0, 900.0)
        ],
    )
    with patch("app.pipeline.osm_paths.symbol_index_for_code", return_value=7):
        got = build_osm_feature_parts(
            tmp_path, **_build_kwargs(), clip_bounds=(-100.0, -100.0, 100.0, 100.0)
        )
    assert sum(p.count for p in got) == 1
