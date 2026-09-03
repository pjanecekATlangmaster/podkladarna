from app.pipeline.osm_paths import (
    _way_skip_reason,
    filter_osm_against_zabaged,
    overlap_fraction,
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
    kept, dropped = filter_osm_against_zabaged(osm_dup + osm_new, zab, near_m=12)
    assert dropped >= 1
    assert any(abs(pt[1] - 80) < 1 for line in kept for pt in line)


def test_filter_keeps_forest_tail_of_road_way():
    zab = [[(0.0, 0.0), (100.0, 0.0)]]
    # 80 m po silnici, pak 40 m do lesa
    osm = [[(0.0, 1.0), (80.0, 1.0), (80.0, 41.0)]]
    kept, dropped = filter_osm_against_zabaged(osm, zab, near_m=12)
    assert dropped == 0
    assert len(kept) == 1
    assert polyline_length(kept[0]) >= 20
    assert all(y > 10 for _, y in kept[0][1:])


def test_unique_parts_splits_middle_overlap():
    index = _SegmentIndex()
    index.add_line([(40.0, 0.0), (60.0, 0.0)])
    line = [(0.0, 0.0), (100.0, 0.0)]
    parts = unique_polyline_parts(line, index, near_m=5, sample_m=10)
    assert len(parts) == 2
    assert polyline_length(parts[0]) > 20
    assert polyline_length(parts[1]) > 20


def test_overlap_high_when_coincident():
    index = _SegmentIndex()
    index.add_line([(0.0, 0.0), (100.0, 0.0)])
    frac = overlap_fraction([(0.0, 1.0), (100.0, 1.0)], index, near_m=12)
    assert frac > 0.9


def test_polyline_length_and_sample():
    pts = [(0.0, 0.0), (30.0, 0.0)]
    assert abs(polyline_length(pts) - 30) < 1e-6
    samples = sample_polyline(pts, 10)
    assert len(samples) >= 3
