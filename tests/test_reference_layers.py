from __future__ import annotations

from pathlib import Path

from app.pipeline.build_oom_map import build_oom_map_xml, write_oom_map
from app.pipeline.oom_layers import OomTemplate
from app.pipeline.georef import PgwGeoref, read_pgw
from app.pipeline.reference_layers import (
    HILLSHADE_VARIANTS,
    _pick_osm_zoom,
    _write_osm_vrt,
    reference_metadata,
)


def test_reference_metadata():
    meta = reference_metadata()
    assert meta["hillshade_source"] == "ČÚZK DMR 5G WMS"
    assert meta["hillshade_tool"] == "WMS ImageServer"
    assert len(meta["hillshade_variants"]) == len(HILLSHADE_VARIANTS)
    assert "osm.png" in meta["map_layers"]
    assert meta["dmpok_preview"] == "dmpok_nahled.png"


def test_pick_osm_zoom_small_bbox():
    z = _pick_osm_zoom(14.4, 50.08, 14.42, 50.09)
    assert 12 <= z <= 18


def test_write_osm_vrt(tmp_path):
    tile = tmp_path / "t.png"
    tile.write_bytes(b"x")
    vrt = tmp_path / "m.vrt"
    _write_osm_vrt([(tile, 10, 20)], 15, 10, 20, vrt)
    text = vrt.read_text(encoding="utf-8")
    assert "EPSG:3857" in text
    assert "SimpleSource" in text


def test_pgw_roundtrip(tmp_path):
    pgw = tmp_path / "a.pgw"
    PgwGeoref(0.5, 0.0, 0.0, -0.5, 100.0, 200.0).write(pgw)
    got = read_pgw(pgw)
    assert got.pixel_x == 0.5
    assert got.origin_x == 100.0


def test_osm_pgw_path(tmp_path):
    out_dir = tmp_path / "references"
    out_dir.mkdir()
    osm_pgw = (out_dir / "osm.png").with_suffix(".pgw")
    assert osm_pgw.name == "osm.pgw"


def test_laz_needs_ground_filter():
    from app.pipeline.reference_layers import _laz_needs_ground_filter

    assert _laz_needs_ground_filter(Path("dmr_ground_0.laz")) is False
    assert _laz_needs_ground_filter(Path("merged_crop.laz")) is True


def test_dem_resolution_m(tmp_path):
    from app.pipeline.georef import PgwGeoref
    from app.pipeline.reference_layers import _dem_resolution_m, _hillshade_dem_resolution_m

    mini_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x04\x00\x00\x00\x04"
        b"\x08\x02\x00\x00\x00\x26\x93\x09\x29\x00\x00\x00\x12IDATx\x9cc\x60\x60"
        b"\x60\x00\x00\x00\x04\x00\x01\x5c\xcd\xff\x69\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    png = tmp_path / "t.png"
    png.write_bytes(mini_png)
    pgw = tmp_path / "t.pgw"
    PgwGeoref(1.0, 0.0, 0.0, -1.0, 0.0, 4.0).write(pgw)
    res = _dem_resolution_m(png, pgw)
    assert 0.25 <= res <= 8.0
    assert abs(res - 1.0) < 0.01
    hill = _hillshade_dem_resolution_m(png, pgw)
    assert hill == 1.0


def test_pdal_dem_output_type():
    from app.pipeline.reference_layers import _pdal_dem_output_type

    assert _pdal_dem_output_type(Path("dmr_ground_0.laz")) == "max"
    assert _pdal_dem_output_type(Path("merged_crop.laz")) == "max"


def test_build_oom_map_xml_contains_templates():
    xml = build_oom_map_xml(
        map_name="Test",
        scale=10000,
        ref_x=500000.0,
        ref_y=1200000.0,
        ref_lat=50.0,
        ref_lon=14.5,
        declination=5.15,
        grivation=13.02,
        preset_id="forest_10000",
        templates=[
            OomTemplate("image", "Ortofoto", "references/orthophoto.png"),
            OomTemplate("image", "KP", "basemap/pullautus.png"),
        ],
    )
    assert "+proj=krovak" in xml
    assert "<parameter>5514</parameter>" in xml
    assert "references/orthophoto.png" in xml
    assert "basemap/pullautus.png" in xml
    assert 'scale="10000"' in xml
    assert "ref_point_deg" in xml
    assert 'declination="5.15"' in xml
    assert 'grivation="13.02"' in xml
    assert '<symbols count="' in xml
    assert 'code="101"' in xml


def test_write_oom_map_file(tmp_path):
    dest = tmp_path / "podkladarna.omap"
    write_oom_map(
        dest,
        map_name="Šance",
        scale=4000,
        ref_x=1.0,
        ref_y=2.0,
        ref_lat=50.0,
        ref_lon=14.5,
        preset_id="sprint_2m",
        templates=[OomTemplate("image", "KP", "basemap/pullautus.png")],
    )
    assert dest.is_file()
    assert "Šance" in dest.read_text(encoding="utf-8")
