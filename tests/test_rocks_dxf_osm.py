"""Tests: cliff DXF native parse + OSM rock/cliff as underlay (not auto omap)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.pipeline.cliffs_dem import parse_cliff_ticks_dxf, write_cliff_ticks_dxf
from app.pipeline.cliff_merge import merge_cliff_ticks, shapely_available
from app.pipeline.oom_symbol_map import resolve_rock_area_code
from app.pipeline.osm_paths import (
    OSM_MANUAL_LAYER_SPECS,
    _OSM_ROCK_UNDERLAY_KINDS,
    build_osm_feature_parts,
    classify_osm_feature,
    feature_oom_code,
)


def test_parse_cliff_ticks_dxf_roundtrip(tmp_path: Path):
    ticks = [
        ((100.0, 200.0), (103.0, 200.5)),
        ((110.0, 210.0), (113.0, 210.5)),
    ]
    dest = tmp_path / "c_rock.dxf"
    assert write_cliff_ticks_dxf(ticks, dest) is not None
    got = parse_cliff_ticks_dxf(dest)
    assert len(got) == 2
    assert got[0][0][0] == 100.0
    assert got[1][1][1] == 210.5


def test_sachr_dxf_parses_and_merges_to_polygons():
    """Reálný ZIP DXF (Sachrův 72d0) → nativní parse + morph → >0 ploch."""
    dxf = (
        Path(__file__).resolve().parents[1]
        / "tmp"
        / "rocks-ab"
        / "72d0"
        / "cliffs_rock.dxf"
    )
    if not dxf.is_file():
        return  # artefakt jen při A/B diagnostice
    ticks = parse_cliff_ticks_dxf(dxf)
    assert len(ticks) == 457
    got = merge_cliff_ticks(ticks, as_polygons=True)
    assert len(got.polygons) >= 10
    assert not got.lines


def test_osm_cliff_way_maps_to_201():
    """way/373494169 natural=cliff → klasifikace 201 (podklad osm/, ne auto omap)."""
    assert classify_osm_feature({"natural": "cliff"}, geom="way") == ("cliff", "201")
    assert feature_oom_code("cliff", "forest_10000") == "201"
    assert feature_oom_code("cliff", "sprint_2m") == "201"
    assert feature_oom_code("cliff", "mtbo_10000") == "201"
    assert "cliff" in _OSM_ROCK_UNDERLAY_KINDS


def test_osm_bare_rock_maps_to_area():
    """way/553090030 / 746567272 natural=bare_rock → 206 kód (podklad, ne auto omap)."""
    assert classify_osm_feature({"natural": "bare_rock"}, geom="way") == (
        "bare_rock",
        "201.2",
    )
    assert feature_oom_code("bare_rock", "forest_10000") == "206"
    assert feature_oom_code("bare_rock", "mtbo_10000") == "206"
    assert "bare_rock" in _OSM_ROCK_UNDERLAY_KINDS
    assert OSM_MANUAL_LAYER_SPECS["bare_rock"][0] == "OSM_skaly"
    assert OSM_MANUAL_LAYER_SPECS["cliff"][0] == "OSM_skaly_linie"


def test_osm_rocks_excluded_from_auto_omap(tmp_path: Path):
    """OSM cliff/bare_rock/scree nesmí jít do auto .omap (jen osm/ SHP podklad)."""
    import json

    feats = [
        {
            "type": "Feature",
            "properties": {"kind": "cliff", "oom_code": "201"},
            "geometry": {
                "type": "LineString",
                "coordinates": [[0.0, 0.0], [10.0, 0.0]],
            },
        },
        {
            "type": "Feature",
            "properties": {"kind": "bare_rock", "oom_code": "206"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[0.0, 0.0], [20.0, 0.0], [20.0, 20.0], [0.0, 20.0], [0.0, 0.0]]
                ],
            },
        },
        {
            "type": "Feature",
            "properties": {"kind": "scree", "oom_code": "206"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[30.0, 0.0], [40.0, 0.0], [40.0, 10.0], [30.0, 10.0], [30.0, 0.0]]
                ],
            },
        },
        {
            "type": "Feature",
            "properties": {"kind": "spring", "oom_code": "312"},
            "geometry": {"type": "Point", "coordinates": [5.0, 5.0]},
        },
    ]
    dest = tmp_path / "osm_paths"
    dest.mkdir()
    (dest / "features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}),
        encoding="utf-8",
    )
    with patch("app.pipeline.osm_paths.symbol_index_for_code", return_value=7):
        got = build_osm_feature_parts(
            tmp_path,
            preset_id="forest_10000",
            scale=10000,
            ref_x=0.0,
            ref_y=0.0,
            grivation_deg=0.0,
        )
    assert sum(p.count for p in got) == 1
    assert all("skála" not in p.name.lower() and "sutina" not in p.name.lower() for p in got)
    assert any("pramen" in p.name.lower() for p in got)


def test_forest_rock_area_uses_visible_206_not_hidden_201_2():
    """ISOM 201.2 je is_hidden – LiDAR skály musí jít na viditelný 206."""
    assert resolve_rock_area_code("forest_10000", 10000) == "206"


def test_shapely_required_for_rock_footprint():
    """Docker image musí mít shapely – bez něj morph → 0 ploch (Sachrův bug)."""
    assert shapely_available()
    # Kompaktní mřížka ticků (~skalní masa) → aspoň 1 polygon.
    ticks = []
    for i in range(6):
        for j in range(6):
            x = 100.0 + i * 2.5
            y = 200.0 + j * 2.5
            ticks.append(((x, y), (x + 2.0, y + 0.3)))
    got = merge_cliff_ticks(ticks, as_polygons=True)
    assert len(got.polygons) >= 1
    assert not got.lines
