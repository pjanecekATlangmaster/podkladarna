import io
import json
from unittest.mock import patch

import urllib.error

from app.pipeline.osm_paths import (
    OVERPASS_URLS,
    _way_skip_reason,
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
    assert _way_skip_reason({"highway": "footway", "footway": "sidewalk"})
    assert _way_skip_reason({"highway": "footway", "footway": "crossing"})
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
    assert classify_osm_feature({"leisure": "playground"}) == ("playground", "401")
    assert classify_osm_feature({"amenity": "bench"}) == ("bench", "531")
    assert classify_osm_feature({"tourism": "information", "information": "board"}) == (
        "info_board",
        "531",
    )
    assert classify_osm_feature({"natural": "wetland"}) == ("wetland", "308")
    assert classify_osm_feature({"natural": "cave_entrance"}) == (
        "cave_entrance",
        "203.1",
    )
    assert classify_osm_feature({"highway": "footway", "footway": "boardwalk"}) == (
        "boardwalk",
        "512.1",
    )
    assert classify_osm_feature({"highway": "path"}) is None
    assert classify_osm_feature({"tourism": "information", "building": "yes"}) is None


def test_feature_oom_code_preset():
    from app.pipeline.osm_paths import feature_oom_code

    assert feature_oom_code("cave_entrance", "sprint_2m") == "203.1"
    assert feature_oom_code("cave_entrance", "forest_10000") == "203.2"
    assert feature_oom_code("boardwalk", "sprint_2m") == "512.1"
    assert feature_oom_code("boardwalk", "forest_10000") == "512"


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


def test_filter_keeps_forest_tail_of_road_way():
    zab = [[(0.0, 0.0), (100.0, 0.0)]]
    # 80 m po silnici, pak 40 m do lesa
    osm = [[(0.0, 1.0), (80.0, 1.0), (80.0, 41.0)]]
    kept, dropped = filter_osm_against_zabaged(osm, zab)
    assert dropped == 0
    assert len(kept) == 1
    assert polyline_length(kept[0]) >= 12
    # Ocas do lesa (sever), ne zbytek podél silnice.
    assert kept[0][-1][1] >= 30
    assert sum(1 for _, y in kept[0] if y > 10) >= len(kept[0]) // 2


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
