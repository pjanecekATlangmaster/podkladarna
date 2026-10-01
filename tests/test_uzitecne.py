"""Testy složky uzitecne/ a knolly mimo auto .omap."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from app.pipeline.oom_symbol_map import oom_code_for_dxf
from app.pipeline.package_oom import build_oom_zip, oom_metadata
from app.pipeline.uzitecne_export import prepare_uzitecne_dir


def test_kopecky_not_in_auto_omap_objects():
    assert oom_code_for_dxf("kopecky.dxf", preset_id="sprint_2m") is None
    assert oom_code_for_dxf("dotknolls.dxf", preset_id="forest_10000") is None


def test_uzitecne_dir_has_readme_and_kopecky(tmp_path: Path):
    kp = tmp_path / "work"
    temp = kp / "temp"
    temp.mkdir(parents=True)
    (temp / "dotknolls.dxf").write_text("knoll points XX", encoding="utf-8")
    dest = prepare_uzitecne_dir(kp)
    assert (dest / "README.txt").is_file()
    assert (dest / "kopecky.dxf").is_file()
    assert "Kopečky" in (dest / "README.txt").read_text(encoding="utf-8")


def test_uzitecne_courtyard_olive_shp(tmp_path: Path):
    kp = tmp_path / "work"
    osm = kp / "osm_paths"
    osm.mkdir(parents=True)
    # Budova s dírou (outer + hole) v S-JTSK metrech.
    features = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"kind": "building", "oom_code": "521"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[0, 0], [40, 0], [40, 40], [0, 40], [0, 0]],
                        [[10, 10], [30, 10], [30, 30], [10, 30], [10, 10]],
                    ],
                },
            }
        ],
    }
    (osm / "features.geojson").write_text(
        json.dumps(features), encoding="utf-8"
    )
    dest = prepare_uzitecne_dir(kp)
    assert (dest / "OSM_dvory_oliva.shp").is_file()


def test_build_oom_zip_includes_uzitecne(tmp_path: Path):
    kp = tmp_path / "work"
    kp.mkdir()
    (kp / "pullautus.png").write_bytes(b"png")
    (kp / "pullautus.pgw").write_text("1\n0\n0\n-1\n0\n0\n", encoding="utf-8")
    temp = kp / "temp"
    temp.mkdir()
    (temp / "out2.dxf").write_text("contours XX", encoding="utf-8")
    (temp / "dotknolls.dxf").write_text("knolls points XX", encoding="utf-8")

    dest = tmp_path / "out.zip"
    meta = oom_metadata("sprint_2m", {"scalefactor": 0.4}, {"scalefactor": 0.4})
    build_oom_zip(kp, dest, zabaged_clean=None, metadata=meta)

    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
    assert "uzitecne/README.txt" in names
    assert "uzitecne/kopecky.dxf" in names
    # base/kopecky.dxf jen když collect_dxf projde (≥8 B) – u krátkých fixture fallback jen uzitecne.
    assert "base/dotknolls.dxf" not in names


def test_benches_off_not_in_omap_but_in_features(tmp_path: Path):
    """Checkbox vypnutý → nábytek není v auto .omap; features.geojson ho má (SHP)."""
    from unittest.mock import patch

    from app.pipeline.osm_paths import build_osm_feature_parts

    osm = tmp_path / "osm_paths"
    osm.mkdir()
    feats = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"kind": "bench", "oom_code": "531"},
                "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
            },
            {
                "type": "Feature",
                "properties": {"kind": "spring", "oom_code": "312"},
                "geometry": {"type": "Point", "coordinates": [1.0, 0.0]},
            },
        ],
    }
    (osm / "features.geojson").write_text(json.dumps(feats), encoding="utf-8")
    kwargs = dict(
        preset_id="sprint_2m",
        scale=4000,
        ref_x=0.0,
        ref_y=0.0,
        grivation_deg=0.0,
    )
    with patch("app.pipeline.osm_paths.symbol_index_for_code", return_value=7):
        off = build_osm_feature_parts(
            tmp_path, **kwargs, include_benches=False
        )
        on = build_osm_feature_parts(
            tmp_path, **kwargs, include_benches=True
        )
    assert sum(p.count for p in off) == 1  # jen spring
    assert sum(p.count for p in on) == 2
    assert any("lavič" in p.name.lower() for p in on)
    assert not any("lavič" in p.name.lower() for p in off)
    # Data pro SHP zůstávají ve features (stahují se vždy).
    stored = json.loads((osm / "features.geojson").read_text(encoding="utf-8"))
    kinds = {f["properties"]["kind"] for f in stored["features"]}
    assert "bench" in kinds


def test_residual_off_writes_shp_not_omap(tmp_path: Path):
    """max_piece_m2=0 (checkbox off) → SHP pásma ano, OOM part ne."""
    from app.pipeline.residual_paved import (
        _RESIDUAL_BANDS_DIR,
        build_residual_paved_parts,
    )

    def _sq_wgs(lat, lon, dlat, dlon):
        return [
            {"lat": lat, "lon": lon},
            {"lat": lat, "lon": lon + dlon},
            {"lat": lat + dlat, "lon": lon + dlon},
            {"lat": lat + dlat, "lon": lon},
            {"lat": lat, "lon": lon},
        ]

    osm = tmp_path / "osm_paths"
    osm.mkdir()
    (osm / "residual_osm_all.json").write_text(
        json.dumps(
            {
                "elements": [
                    {
                        "type": "way",
                        "id": 1,
                        "tags": {"landuse": "residential"},
                        "geometry": _sq_wgs(50.10, 14.56, 0.001, 0.001),
                    },
                    {
                        "type": "way",
                        "id": 2,
                        "tags": {"building": "yes"},
                        "geometry": _sq_wgs(50.10005, 14.56005, 0.0009, 0.0009),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    parts = build_residual_paved_parts(
        tmp_path,
        preset_id="sprint_2m",
        scale=4000,
        ref_x=0.0,
        ref_y=0.0,
        grivation_deg=0.0,
        max_piece_m2=0.0,
        write_shapefiles=True,
    )
    assert parts == []
    band_dir = tmp_path / _RESIDUAL_BANDS_DIR
    assert any(band_dir.glob("OSM_residential_zbytek_*.shp"))
