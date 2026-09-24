"""Testy inverzní zpevněné plochy (residential − všechny OSM objekty)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.residual_paved import (
    RESIDUAL_SPARSE_STEM,
    _PART_NAME,
    _RESIDUAL_BANDS_DIR,
    _subjects_and_mask_wkbs,
    build_residual_paved_parts,
    path_mask_half_width_m,
    residual_band_stem,
    residual_max_m2,
)


def _sq_wgs(lat0, lon0, dlat, dlon):
    """Uzavřený ring v lat/lon (přibližně čtverec)."""
    return [
        {"lat": lat0, "lon": lon0},
        {"lat": lat0, "lon": lon0 + dlon},
        {"lat": lat0 + dlat, "lon": lon0 + dlon},
        {"lat": lat0 + dlat, "lon": lon0},
        {"lat": lat0, "lon": lon0},
    ]


def _write_cache(work: Path, elements: list[dict]) -> None:
    dest = work / "osm_paths"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "residual_osm_all.json").write_text(
        json.dumps({"elements": elements}),
        encoding="utf-8",
    )


def test_path_mask_half_width_has_outline_reserve():
    assert path_mask_half_width_m("road_3") > path_mask_half_width_m("path")
    assert path_mask_half_width_m("road_2") >= 2.5 + 0.8
    assert path_mask_half_width_m("sidewalk") >= 0.7 + 0.8
    assert path_mask_half_width_m("footway") < path_mask_half_width_m("sidewalk")


def test_school_polygon_is_in_mask_not_subject():
    """way/365640538 amenity=school musí jít do masky, ne do subject."""
    residential = {
        "type": "way",
        "id": 1,
        "tags": {"landuse": "residential"},
        "geometry": _sq_wgs(50.10, 14.56, 0.002, 0.003),
    }
    school = {
        "type": "way",
        "id": 365640538,
        "tags": {"amenity": "school"},
        "geometry": _sq_wgs(50.1005, 14.561, 0.0004, 0.0005),
    }
    subjects, mask = _subjects_and_mask_wkbs([residential, school])
    assert len(subjects) == 1
    assert len(mask) >= 1
    from shapely import from_wkb

    assert from_wkb(mask[0]).area < from_wkb(subjects[0]).area


def test_residual_cuts_school_out_of_residential(tmp_path: Path):
    residential = {
        "type": "way",
        "id": 1,
        "tags": {"landuse": "residential"},
        "geometry": _sq_wgs(50.10, 14.56, 0.002, 0.003),
    }
    # Škola + velká budova pokrývají většinu – zbytek pod prahem 25 %.
    school = {
        "type": "way",
        "id": 365640538,
        "tags": {"amenity": "school"},
        "geometry": _sq_wgs(50.1002, 14.5605, 0.0012, 0.0015),
    }
    building = {
        "type": "way",
        "id": 2,
        "tags": {"building": "yes"},
        "geometry": _sq_wgs(50.1001, 14.5601, 0.0018, 0.0028),
    }
    _write_cache(tmp_path, [residential, school, building])
    parts = build_residual_paved_parts(
        tmp_path,
        preset_id="sprint_2m",
        scale=4000,
        ref_x=0.0,
        ref_y=0.0,
        grivation_deg=0.0,
        max_piece_m2=float("inf"),
    )
    assert len(parts) == 1
    assert parts[0].name == _PART_NAME
    assert parts[0].count >= 1
    assert 'symbol="' in parts[0].objects_xml


def test_highway_line_goes_to_mask_as_buffer():
    road = {
        "type": "way",
        "id": 2,
        "tags": {"highway": "residential"},
        "geometry": [
            {"lat": 50.10, "lon": 14.56},
            {"lat": 50.101, "lon": 14.562},
        ],
    }
    subjects, mask = _subjects_and_mask_wkbs([road])
    assert subjects == []
    assert len(mask) == 1


def test_residual_off_for_forest_preset(tmp_path: Path):
    _write_cache(
        tmp_path,
        [
            {
                "type": "way",
                "id": 1,
                "tags": {"landuse": "residential"},
                "geometry": _sq_wgs(50.0, 14.0, 0.001, 0.001),
            }
        ],
    )
    assert (
        build_residual_paved_parts(
            tmp_path,
            preset_id="forest_10000",
            scale=10000,
            ref_x=0.0,
            ref_y=0.0,
            grivation_deg=0.0,
        )
        == []
    )


def test_residual_empty_without_residential(tmp_path: Path):
    _write_cache(
        tmp_path,
        [
            {
                "type": "way",
                "id": 365640538,
                "tags": {"amenity": "school"},
                "geometry": _sq_wgs(50.0, 14.0, 0.001, 0.001),
            }
        ],
    )
    assert (
        build_residual_paved_parts(
            tmp_path,
            preset_id="sprint_2m",
            scale=4000,
            ref_x=0.0,
            ref_y=0.0,
            grivation_deg=0.0,
        )
        == []
    )


def test_sparse_residential_is_skipped(tmp_path: Path):
    """Residential skoro bez OSM uvnitř → přeskočit (jinak přikryje KP)."""
    # Velký residential, uvnitř jen malá budova → zbytek >> 25 %.
    residential = {
        "type": "way",
        "id": 1275344912,
        "tags": {"landuse": "residential"},
        "geometry": _sq_wgs(50.10, 14.56, 0.003, 0.004),
    }
    tiny = {
        "type": "way",
        "id": 99,
        "tags": {"building": "yes"},
        "geometry": _sq_wgs(50.101, 14.561, 0.00005, 0.00005),
    }
    _write_cache(tmp_path, [residential, tiny])
    assert (
        build_residual_paved_parts(
            tmp_path,
            preset_id="sprint_2m",
            scale=4000,
            ref_x=0.0,
            ref_y=0.0,
            grivation_deg=0.0,
        )
        == []
    )
    sparse_shp = tmp_path / _RESIDUAL_BANDS_DIR / f"{RESIDUAL_SPARSE_STEM}.shp"
    assert sparse_shp.is_file()


def test_dense_residential_is_kept(tmp_path: Path):
    """Residential hustě vyplněný OSM maskou → zbytek pod prahem, kreslit."""
    residential = {
        "type": "way",
        "id": 1275344910,
        "tags": {"landuse": "residential"},
        "geometry": _sq_wgs(50.10, 14.56, 0.001, 0.001),
    }
    # Budova pokrývá většinu plochy.
    building = {
        "type": "way",
        "id": 100,
        "tags": {"building": "yes"},
        "geometry": _sq_wgs(50.10005, 14.56005, 0.0009, 0.0009),
    }
    _write_cache(tmp_path, [residential, building])
    parts = build_residual_paved_parts(
        tmp_path,
        preset_id="sprint_2m",
        scale=4000,
        ref_x=0.0,
        ref_y=0.0,
        grivation_deg=0.0,
        max_piece_m2=float("inf"),
    )
    assert len(parts) == 1
    assert parts[0].count >= 1


def test_residual_size_filter_and_band_shp(tmp_path: Path):
    """Do .omap jen ≤ max; SHP má i větší pásmo."""
    assert residual_max_m2("small") == 500.0
    assert residual_band_stem(100.0).endswith("_mensi")
    assert residual_band_stem(1000.0).endswith("_stredni")
    assert residual_band_stem(5000.0).endswith("_velke")

    # Malý residential, budova nechá zbytek ~16 % (~1200 m²) → střední pásmo.
    residential = {
        "type": "way",
        "id": 1,
        "tags": {"landuse": "residential"},
        "geometry": _sq_wgs(50.10, 14.56, 0.001, 0.001),
    }
    building = {
        "type": "way",
        "id": 2,
        "tags": {"building": "yes"},
        "geometry": _sq_wgs(50.10005, 14.56005, 0.0009, 0.0009),
    }
    _write_cache(tmp_path, [residential, building])
    # small (≤500) → do omap nic, ale SHP stredni ano
    assert (
        build_residual_paved_parts(
            tmp_path,
            preset_id="sprint_2m",
            scale=4000,
            ref_x=0.0,
            ref_y=0.0,
            grivation_deg=0.0,
            max_piece_m2=500.0,
        )
        == []
    )
    band_dir = tmp_path / _RESIDUAL_BANDS_DIR
    assert (band_dir / "OSM_residential_zbytek_stredni.shp").is_file()
    # medium → už v omap
    parts = build_residual_paved_parts(
        tmp_path,
        preset_id="sprint_2m",
        scale=4000,
        ref_x=0.0,
        ref_y=0.0,
        grivation_deg=0.0,
        max_piece_m2=2000.0,
    )
    assert len(parts) == 1
    assert parts[0].count >= 1
