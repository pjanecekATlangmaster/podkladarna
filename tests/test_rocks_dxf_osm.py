"""Tests: cliff DXF native parse + OSM rock/cliff → 201/206."""
from __future__ import annotations

from pathlib import Path

from app.pipeline.cliffs_dem import parse_cliff_ticks_dxf, write_cliff_ticks_dxf
from app.pipeline.cliff_merge import merge_cliff_ticks, shapely_available
from app.pipeline.oom_symbol_map import resolve_rock_area_code
from app.pipeline.osm_paths import classify_osm_feature, feature_oom_code


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
    """way/373494169 natural=cliff → linie 201 (les i sprint)."""
    assert classify_osm_feature({"natural": "cliff"}, geom="way") == ("cliff", "201")
    assert feature_oom_code("cliff", "forest_10000") == "201"
    assert feature_oom_code("cliff", "sprint_2m") == "201"
    assert feature_oom_code("cliff", "mtbo_10000") == "201"


def test_osm_bare_rock_maps_to_area():
    """way/746567272 natural=bare_rock → plocha 206 (201.2 is_hidden v ISOM)."""
    assert classify_osm_feature({"natural": "bare_rock"}, geom="way") == (
        "bare_rock",
        "201.2",
    )
    assert feature_oom_code("bare_rock", "forest_10000") == "206"
    assert feature_oom_code("bare_rock", "mtbo_10000") == "206"


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
