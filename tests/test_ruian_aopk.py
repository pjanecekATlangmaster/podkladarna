"""RÚIAN budovy + AOPK památné stromy + doplnky."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from app.pipeline.aopk_trees import (
    build_aopk_tree_parts,
    filter_osm_landmark_trees_near_aopk,
)
from app.pipeline.oom_symbol_map import oom_code_for_vectorconf_rule
from app.pipeline.oom_vectorconf import load_vectorconf, match_feature
from app.pipeline.package_oom import DOPLNKY_README, build_oom_zip
from app.pipeline.ruian_buildings import (
    ZABAGED_OMIT_BUILDING_LAYERS,
    build_ruian_building_parts,
)


def test_vectorconf_vyznamny_strom_lesik():
    rules = load_vectorconf("zabaged.txt")
    rule = match_feature({"vrstva": "VyznamnyStromLesik"}, rules)
    assert rule is not None
    code = oom_code_for_vectorconf_rule(
        rule.symbol_name,
        rule.kp_code,
        "VyznamnyStromLesik",
        preset_id="sprint_2m",
        scale=4000,
    )
    assert code == "417"
    mtbo = oom_code_for_vectorconf_rule(
        rule.symbol_name,
        rule.kp_code,
        "VyznamnyStromLesik",
        preset_id="mtbo_10000",
        scale=10000,
    )
    assert mtbo == "418"


def test_build_ruian_building_parts_521(tmp_path: Path):
    gj = tmp_path / "ruian.geojson"
    gj.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-745000.0, -1045000.0],
                                    [-744990.0, -1045000.0],
                                    [-744990.0, -1044990.0],
                                    [-745000.0, -1044990.0],
                                    [-745000.0, -1045000.0],
                                ]
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    parts = build_ruian_building_parts(
        gj,
        preset_id="sprint_2m",
        scale=4000,
        ref_x=-745000.0,
        ref_y=-1045000.0,
        grivation_deg=0.0,
    )
    assert len(parts) == 1
    assert parts[0].count == 1
    assert "RÚIAN" in parts[0].name
    assert "budovy" in parts[0].name.lower()


def test_build_ruian_building_parts_mtbo_526(tmp_path: Path):
    gj = tmp_path / "ruian.geojson"
    gj.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-745000.0, -1045000.0],
                                    [-744990.0, -1045000.0],
                                    [-744990.0, -1044990.0],
                                    [-745000.0, -1044990.0],
                                    [-745000.0, -1045000.0],
                                ]
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    parts = build_ruian_building_parts(
        gj,
        preset_id="mtbo_10000",
        scale=10000,
        ref_x=-745000.0,
        ref_y=-1045000.0,
        grivation_deg=0.0,
    )
    assert parts and parts[0].count == 1


def test_build_aopk_tree_parts_417(tmp_path: Path):
    gj = tmp_path / "aopk.geojson"
    gj.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Point",
                            "coordinates": [-745000.0, -1045000.0],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    parts = build_aopk_tree_parts(
        gj,
        preset_id="sprint_2m",
        scale=4000,
        ref_x=-745000.0,
        ref_y=-1045000.0,
        grivation_deg=0.0,
    )
    assert len(parts) == 1
    assert parts[0].count == 1
    assert "AOPK" in parts[0].name


def test_aopk_dedup_osm_landmark_tree():
    aopk = [(-745000.0, -1045000.0)]
    feats = [
        {
            "type": "Feature",
            "properties": {"kind": "landmark_tree"},
            "geometry": {"type": "Point", "coordinates": [-745001.0, -1045001.0]},
        },
        {
            "type": "Feature",
            "properties": {"kind": "landmark_tree"},
            "geometry": {"type": "Point", "coordinates": [-746000.0, -1046000.0]},
        },
        {
            "type": "Feature",
            "properties": {"kind": "bench"},
            "geometry": {"type": "Point", "coordinates": [-745000.0, -1045000.0]},
        },
    ]
    kept, dropped = filter_osm_landmark_trees_near_aopk(feats, aopk)
    assert dropped == 1
    assert len(kept) == 2
    kinds = [f["properties"]["kind"] for f in kept]
    assert kinds.count("landmark_tree") == 1
    assert "bench" in kinds


def test_build_osm_feature_parts_skips_buildings(tmp_path: Path):
    from app.pipeline.osm_paths import build_osm_feature_parts

    osm = tmp_path / "osm_paths"
    osm.mkdir()
    (osm / "features.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"kind": "building", "oom_code": "521"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-745000.0, -1045000.0],
                                    [-744990.0, -1045000.0],
                                    [-744990.0, -1044990.0],
                                    [-745000.0, -1044990.0],
                                    [-745000.0, -1045000.0],
                                ]
                            ],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {"kind": "water_well", "oom_code": "311"},
                        "geometry": {
                            "type": "Point",
                            "coordinates": [-745005.0, -1045005.0],
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    parts = build_osm_feature_parts(
        tmp_path,
        preset_id="sprint_2m",
        scale=4000,
        ref_x=-745000.0,
        ref_y=-1045000.0,
        grivation_deg=0.0,
    )
    names = " ".join(p.name for p in parts)
    assert "budov" not in names.lower()
    assert any("studn" in p.name.lower() for p in parts)


def test_doplnky_in_oom_zip(tmp_path: Path):
    kp = tmp_path / "kp"
    kp.mkdir()
    (kp / "pullautus.png").write_bytes(b"png")
    (kp / "pullautus.pgw").write_text(
        "1\n0\n0\n-1\n0\n0\n", encoding="utf-8"
    )
    osm = kp / "osm_paths"
    osm.mkdir()
    (osm / "buildings.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}),
        encoding="utf-8",
    )
    zab = tmp_path / "zabaged.zip"
    with zipfile.ZipFile(zab, "w") as zf:
        for layer in ZABAGED_OMIT_BUILDING_LAYERS:
            zf.writestr(f"{layer}.shp", b"shp")
            zf.writestr(f"{layer}.shx", b"shx")
            zf.writestr(f"{layer}.dbf", b"dbf")
            zf.writestr(f"{layer}.prj", b"prj")
        zf.writestr("Pesina.shp", b"shp")
        zf.writestr("Pesina.shx", b"shx")
        zf.writestr("Pesina.dbf", b"dbf")

    out = tmp_path / "out.zip"
    build_oom_zip(
        kp,
        out,
        zabaged_clean=zab,
        metadata={"scale": 4000, "label": "test"},
        include_png=False,
        include_dxf=False,
    )
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert "doplnky/README.txt" in names
    assert "doplnky/osm_budovy.geojson" in names
    assert any(n.startswith("doplnky/zabaged_budovy/") for n in names)
    assert any("BudovaJednotlivaNeboBlokBudov.shp" in n for n in names)
    assert not any("doplnky/zabaged_budovy/Pesina.shp" in n for n in names)
    assert DOPLNKY_README.strip().startswith("Doplňky")
