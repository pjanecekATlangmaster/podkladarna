"""Výčet kódů, které generátor umí vyprodukovat, proti katalogu symbolů.

Každý kód generátoru musí mít v ``configs/oom/symbol_catalog.yaml`` jednoznačné
mapování na symbol sady (isom, sprint) a geometrie symbolu musí odpovídat tomu,
jak ho generátor použije (bod / čára / plocha). Chybějící kód by se za běhu
potichu zahodil (nebo nahradil fallbackem), proto je test tvrdý.

MTBO (sekce ``mtbo``) jede na ISMTBOM 2022 ``*_cz.omap``; kódy generátoru jsou tam stále v číslování dřívějšího ISMTBOM a katalog je překládá.
"""

from __future__ import annotations

import inspect
import re
import struct
from pathlib import Path

import pytest

import app.pipeline.oom_symbol_map as sm
from app.pipeline import osm_paths
from app.pipeline.contours_gdal import contour_oom_code
from app.pipeline.oom_symbols import OOM_DIR
from app.pipeline.oom_vectorconf import load_vectorconf
from app.pipeline.symbol_catalog import catalog_entry, generator_map, load_catalog
from app.pipeline.vegetation_gdal import vege_class_to_oom_code

# (klíč katalogu, preset, měřítko, soubor vectorconf, starý OOM soubor pro "known")
SETS = [
    ("isom", "forest_10000", 10000, "zabaged_forest.txt", "ISOM_2017-2_10000.omap"),
    ("isom", "forest_10000", 15000, "zabaged_forest.txt", "ISOM_2017-2_15000.omap"),
    ("sprint", "sprint_2m", 4000, "zabaged.txt", "ISSprOM_2019_4000.omap"),
    ("mtbo", "mtbo_10000", 10000, "zabaged_forest.txt", "ISMTBOM_10000.omap"),
    ("mtbo", "mtbo_15000", 15000, "zabaged_forest.txt", "ISMTBOM_15000.omap"),
]
_SET_IDS = [f"{k}-{s}" for k, _p, s, _v, _o in SETS]

_CLIFF_MODES = (
    sm.KP_CLIFF_AUTO,
    sm.KP_CLIFF_EARTH_BANK,
    sm.KP_CLIFF_SYMBOL_206,
    sm.KP_CLIFF_OFF,
)

# Geometrie, kterou generátor pro daný druh objektu kreslí.
_KIND_GEOM = {
    **{k: "point" for k in (
        "bench", "picnic_table", "lamp", "firepit", "playground_equipment", "fitness",
        "barrier_point", "memorial", "landmark_tree", "info_board", "hunting_stand",
        "water_tower", "cave_entrance", "spring", "water_well",
    )},
    **{k: "line" for k in (
        "fence", "wall", "hedge", "power_line", "power_line_major", "cliff",
    )},
    **{k: "area" for k in (
        "wetland", "water_body", "fountain", "farmland", "garden", "playground", "pitch", "parking",
        "pedestrian_area", "platform", "water_well_building", "building", "bare_rock",
        "scree", "rock_area", "residential", "shelter",
    )},
}
# ZABAGED vrstvy s vlastním kódem (viz oom_symbol_map._LAYER_OOM_CODE).
_LAYER_GEOM = {
    "StupenSraz": "line", "SkupinaBalvanu": "point", "LiniovaVegetace": "line",
    "ElektrickeVedeni": "line", "OvocnySadZahrada": "area", "VyznamnyStromLesik": "point",
    "MohylaPomnikNahrobek": "point", "KrizSloupKulturnihoVyznamu": "point",
    "OsamelyBalvanSkalaSkalniSuk": "point", "VezovitaStavba": "point",
    "Zed": "line", "HradbaVal": "line", "RozvalinaZricenina": {"line", "area"},
}
# Pravidla vectorconf podle jména symbolu (bez vrstvy).
_RULE_GEOM = {
    "building": "area", "farm": "area", "settlement": "area", "water": "area",
    "parking": "area", "waterway": "line", "power line": "line", "blackline": "line",
    "road-path": "line", "railway": "line", "tramway": "line", "fence": "line",
}
_DXF_GEOM = {
    "contours.dxf": "line", "cliffs_small.dxf": "line", "cliffs_large.dxf": "line",
    "cliffs_rock.dxf": "line", "dotknolls.dxf": "point", "dotdepressions.dxf": "point",
    "dotpits.dxf": "point",
}


def _geoms(value) -> set[str]:
    return {value} if isinstance(value, str) else set(value)


def _all_highways() -> list[str]:
    base = (
        set(osm_paths.OSM_HIGHWAYS)
        | set(osm_paths.OSM_ROAD_HIGHWAYS)
        | set(osm_paths.ROAD_DRAW_HIGHWAYS)
        | set(osm_paths.TRACK_DRAW_HIGHWAYS)
        | {"sidewalk", "cycleway_paved", "steps", "path", ""}
    )
    out = set(base)
    out.add(osm_paths.OSM_BRIDGE_HIGHWAY)
    out |= {osm_paths.bridge_highway(b) for b in base if b}
    out.add(osm_paths.OSM_TUNNEL_HIGHWAY)
    out |= {osm_paths.tunnel_highway(b) for b in base if b}
    return sorted(out)


def _osm_feature_kinds() -> list[tuple[str, str]]:
    """(kind, uložený výchozí kód) z classify_osm_feature + druhy z feature_oom_code."""
    src = inspect.getsource(osm_paths.classify_osm_feature)
    pairs = set(re.findall(r'return\s+"(\w+)",\s*"([\d.]+)"', src))
    fsrc = inspect.getsource(osm_paths.feature_oom_code)
    kinds = set(re.findall(r'kind == "(\w+)"', fsrc))
    for group in re.findall(r"kind in \{([^}]*)\}", fsrc):
        kinds |= set(re.findall(r'"(\w+)"', group))
    kinds |= set(osm_paths._PAVED_AREA_KINDS)
    known = {k for k, _c in pairs}
    pairs |= {(k, "") for k in kinds if k not in known}
    return sorted(pairs)


def _layers() -> list[str]:
    return sorted(set(sm._LAYER_OOM_CODE) | {"Zed", "HradbaVal", "RozvalinaZricenina", ""})


def collect_generated_codes(
    key: str, preset: str, scale: int, vectorconf: str, legacy_set: str
) -> dict[str, tuple[str, set[str]]]:
    """{kód generátoru: (odkud, přípustné geometrie)} pro jeden preset.

    Kontrola ``known`` v ``oom_code_for_vectorconf_rule`` se tu nahrazuje
    existencí kódu ve STARÉ OOM sadě (tak se kódy před přechodem na katalog
    zjišťovaly); jinak by katalog sám filtroval a chybějící kódy by v testu
    zmizely.
    """
    out: dict[str, tuple[str, set[str]]] = {}

    def add(code: str | None, src: str, geom) -> None:
        if not code:
            return
        if code in out:
            out[code] = (out[code][0], out[code][1] | _geoms(geom))
        else:
            out[code] = (src, _geoms(geom))

    legacy_index = sm._code_to_index(OOM_DIR / legacy_set)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sm, "_has_symbol", lambda _p, _s, code: code in legacy_index)
        for hw in _all_highways():
            add(osm_paths.osm_oom_code(hw, preset), f"osm_oom_code({hw!r})", "line")
        for kind, default in _osm_feature_kinds():
            add(
                osm_paths.feature_oom_code(kind, preset, default),
                f"feature_oom_code({kind!r})",
                _KIND_GEOM[kind],
            )
        for rule in load_vectorconf(vectorconf):
            for layer in _layers():
                geom = _LAYER_GEOM.get(layer) or _RULE_GEOM.get(rule.symbol_name)
                if geom is None:
                    geom = {"point", "line", "area"}  # neznámé → jen existence
                add(
                    sm.oom_code_for_vectorconf_rule(
                        rule.symbol_name, rule.kp_code, layer, preset_id=preset, scale=scale
                    ),
                    f"vectorconf {rule.symbol_name}|{rule.kp_code} layer={layer or '-'}",
                    geom,
                )
    dxf_names = (
        set(sm._DXF_OOM_CODE_SPRINT) | set(sm._DXF_OOM_CODE_FOREST) | sm._CLIFF_DXF | sm._ROCK_DXF
    )
    for name in sorted(dxf_names):
        for mode in _CLIFF_MODES:
            code = sm.oom_code_for_dxf(name, preset_id=preset, cliff_symbol=mode)
            # symbol_206: srázy se sloučí na polygony → plocha 206.
            geom = "area" if code == sm.KP_CLIFF_206_CODE else _DXF_GEOM[name]
            add(code, f"oom_code_for_dxf({name}, {mode})", geom)
    for cls in range(0, 12):
        add(vege_class_to_oom_code(cls), f"vege_class_to_oom_code({cls})", "area")
    for elev, index_m in ((100.0, None), (100.0, 25.0), (102.0, 25.0), (105.0, 5.0)):
        add(
            contour_oom_code(elev, interval_m=5.0, index_m=index_m),
            f"contour_oom_code({elev}, index={index_m})",
            "line",
        )
    add("103", "contours: formline 103", "line")
    add("417", "AOPK památný strom", "point")
    add("708", "AOI rám", "line")
    add("403", "ZABAGED ostatní plocha jako 403", "area")
    add("520", "oliva / dvory", "area")
    add("104", "sráz zem", "line")
    if key == "sprint":
        add("501.0", "residual_paved (sprint)", "area")
    if key == "isom":
        add("501.2", "okraj zpevněné plochy 501.1 (osm_paths)", "line")
    # Plocha skály: resolve_rock_area_code vrací 206, nouzově 210 (201.2 v OCAD
    # sadě není plocha → záměrně nemapováno a přeskočeno).
    add(sm.KP_CLIFF_206_CODE, "resolve_rock_area_code", "area")
    add(sm.KP_CLIFF_DENSE_CODE, "resolve_rock_area_code (nouzově)", "area")
    return out


@pytest.mark.parametrize("key,preset,scale,vectorconf,legacy", SETS, ids=_SET_IDS)
def test_collect_finds_known_codes(key, preset, scale, vectorconf, legacy) -> None:
    """Sanity: výčet opravdu něco vrací (ať test nemlčí kvůli prázdnému vstupu)."""
    codes = collect_generated_codes(key, preset, scale, vectorconf, legacy)
    expected = ("101", "102", "104", "206", "521", "708", "403")
    for code in expected:
        assert code in codes, code
    if key == "sprint":
        for code in ("501.30", "501.10", "532.7", "513.2", "509.1", "505.1"):
            assert code in codes, code
    elif key == "mtbo":
        for code in ("831", "833", "834", "526", "502"):
            assert code in codes, code
    else:
        for code in ("502.1", "512", "532", "513"):
            assert code in codes, code


@pytest.mark.parametrize("key,preset,scale,vectorconf,legacy", SETS, ids=_SET_IDS)
def test_generator_codes_in_catalog(key, preset, scale, vectorconf, legacy) -> None:
    codes = collect_generated_codes(key, preset, scale, vectorconf, legacy)
    mapped = generator_map(key)
    missing = {c: src for c, (src, _g) in sorted(codes.items()) if c not in mapped}
    if missing:
        lines = [f"  {c:<8} <- {src}" for c, src in missing.items()]
        print(f"\n[{key}] kódy generátoru bez mapování v katalogu:\n" + "\n".join(lines))
    assert not missing, f"{key}: chybí v katalogu {sorted(missing)}"


@pytest.mark.parametrize("key,preset,scale,vectorconf,legacy", SETS, ids=_SET_IDS)
def test_catalog_geometry_matches_generator_use(key, preset, scale, vectorconf, legacy) -> None:
    codes = collect_generated_codes(key, preset, scale, vectorconf, legacy)
    mapped = generator_map(key)
    set_file = {
        "isom": "ISOM_2017_10000_cz.omap",
        "sprint": "ISSprOM_2019_4000_cz.omap",
        "mtbo": "ISMTBOM_2022_10000_cz.omap",
    }[key]
    bad = []
    for code, (src, geoms) in sorted(codes.items()):
        ocad = mapped.get(code)
        entry = catalog_entry(OOM_DIR / set_file, ocad) if ocad else None
        if entry is None:
            continue  # chybějící mapování hlídá test_generator_codes_in_catalog
        if entry["geom"] not in geoms:
            bad.append(f"{code} -> {ocad} ({entry['cz_name']}, {entry['geom']}) "
                       f"čekáno {sorted(geoms)} <- {src}")
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize(
    "key,set_files",
    [
        ("isom", ("ISOM_2017_10000_cz.omap", "ISOM_2017_15000_cz.omap")),
        ("sprint", ("ISSprOM_2019_4000_cz.omap",)),
        ("mtbo", ("ISMTBOM_2022_10000_cz.omap", "ISMTBOM_2022_15000_cz.omap")),
    ],
)
def test_catalog_is_consistent_with_set_files(key: str, set_files: tuple[str, ...]) -> None:
    """Kódy sady v katalogu jsou unikátní a existují v *_cz.omap; kód generátoru patří jednomu symbolu."""
    entries = load_catalog()[key]
    codes = [e["ocad_code"] for e in entries]
    assert len(codes) == len(set(codes))
    gens = [g for e in entries for g in e["generator_codes"]]
    assert len(gens) == len(set(gens)), f"{key}: kód generátoru je ve dvou symbolech"
    for name in set_files:
        index = sm._code_to_index(OOM_DIR / name)
        missing = [c for c in codes if c not in index]
        assert not missing, f"{name}: v katalogu je kód, který v sadě není: {missing}"

def test_sprint_path_widths_are_mapped_by_width() -> None:
    """Šířky (OOM footprint → OCAD): 505.1 (1,4 m) → 505.0, 532.7 (2 m) → 532.0 (1,6 m)."""
    m = generator_map("sprint")
    assert m["505.1"] == "505.0"
    assert m["532.7"] == "532.0"
    assert m["509.1"] == "509.100" and m["509.2"] == "509.200"
    assert m["513.2"] == "513.200"
    assert m["501.0"] == "501.0" and m["501.31"] == "501.31"
    iso = generator_map("isom")
    assert iso["201"] == "201.1"  # čára, ne plocha 201.0
    assert iso["203.1"] == "203.100" and iso["203.2"] == "203.101"
    assert iso["513"] == "513.100" and iso["513.2"] == "513.200"
    assert "201.2" not in iso and "201.2" not in m


# --- spy: kódy skutečně požadované kódem generátoru ----------------------------


def _wkb_polygon(coords: list[tuple[float, float]]) -> bytes:
    ring = coords + [coords[0]]
    data = struct.pack("<BIII", 1, 3, 1, len(ring))
    for x, y in ring:
        data += struct.pack("<dd", x, y)
    return data


def _line_feature(highway: str) -> dict:
    return {
        "type": "Feature",
        "properties": {"highway": highway},
        "geometry": {"type": "LineString", "coordinates": [[0.0, 0.0], [50.0, 0.0], [50.0, 40.0]]},
    }


@pytest.mark.parametrize("key,preset,scale", [("isom", "forest_10000", 10000), ("sprint", "sprint_2m", 4000), ("mtbo", "mtbo_10000", 10000)])
def test_spy_osm_paths_codes_in_catalog(tmp_path: Path, seen_generator_codes, key, preset, scale) -> None:
    import json

    out = tmp_path / "osm_paths"
    out.mkdir()
    feats = [_line_feature(hw) for hw in _all_highways() if hw]
    (out / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    sm.drain_unmapped_codes()
    parts = osm_paths.build_osm_path_parts(
        tmp_path, preset_id=preset, scale=scale, ref_x=0.0, ref_y=0.0, grivation_deg=0.0
    )
    assert parts
    assert seen_generator_codes, "špión nic nezachytil"
    assert seen_generator_codes <= set(generator_map(key)), sorted(
        seen_generator_codes - set(generator_map(key))
    )
    # žádný fallback na 506: všechny cesty našly svůj symbol
    assert sm.drain_unmapped_codes() == {}
    # les / MTBO zahazují chodník souběžný se silnicí (všechny cesty fixtury leží na sobě);
    # pěšina ležící celá v pásu silnice / zpevněné se ořízne pryč (path_cut_zone).
    zone = osm_paths.road_zone_geometry(feats)
    cut = osm_paths.path_cut_zone_geometry(tmp_path, feats)
    # sprint nekreslí silniční tunely (tunnel:road_*)
    dropped = 0
    for f in feats:
        hw = f["properties"]["highway"]
        coords = f["geometry"]["coordinates"]
        if key == "sprint":
            if osm_paths.is_tunnel_highway(hw) and osm_paths.path_draw_highway(hw).startswith("road_"):
                dropped += 1
                continue
        elif osm_paths.is_tunnel_highway(hw) and osm_paths.polyline_length(coords) > osm_paths.TUNNEL_MAX_DRAW_M:
            dropped += 1  # les / MTBO: dlouhý tunel se nekreslí
            continue
        elif osm_paths.is_sidewalk_beside_road(hw, coords, zone):
            dropped += 1
            continue
        if (
            cut is not None
            and osm_paths.path_draw_highway(hw) in osm_paths.PATH_CUT_BASES
            and not osm_paths.is_bridge_highway(hw)
            and not osm_paths.is_tunnel_highway(hw)
            and osm_paths._line_in_zone(coords, cut)
        ):
            dropped += 1
    assert sum(p.count for p in parts) == len(feats) - dropped


@pytest.mark.parametrize("key,preset,scale", [("isom", "forest_10000", 10000), ("sprint", "sprint_2m", 4000), ("mtbo", "mtbo_10000", 10000)])
def test_spy_vegetation_codes_in_catalog(
    tmp_path, monkeypatch, seen_generator_codes, key, preset, scale
) -> None:
    import app.pipeline.vegetation_gdal as vg

    square = _wkb_polygon([(0.0, 0.0), (30.0, 0.0), (30.0, 30.0), (0.0, 30.0)])
    rows = [({"cls": c, "code": vege_class_to_oom_code(c)}, square) for c in (1, 2, 3, 4)]
    monkeypatch.setattr(vg, "_iter_vege_rows", lambda _shp: rows)
    (tmp_path / "vegetation").mkdir()
    (tmp_path / "vegetation" / "vegetation.shp").write_bytes(b"placeholder")
    sm.drain_unmapped_codes()
    parts = vg.build_vegetation_parts(
        tmp_path, preset_id=preset, scale=scale, ref_x=0.0, ref_y=0.0, grivation_deg=0.0
    )
    assert parts
    assert seen_generator_codes <= set(generator_map(key)), sorted(
        seen_generator_codes - set(generator_map(key))
    )
    assert sm.drain_unmapped_codes() == {}


@pytest.mark.parametrize("key,preset,scale", [("isom", "forest_10000", 10000), ("sprint", "sprint_2m", 4000), ("mtbo", "mtbo_10000", 10000)])
def test_spy_dxf_codes_in_catalog(seen_generator_codes, key, preset, scale) -> None:
    sm.drain_unmapped_codes()
    names = set(_DXF_GEOM)
    for name in sorted(names):
        for mode in _CLIFF_MODES:
            code = sm.oom_code_for_dxf(name, preset_id=preset, cliff_symbol=mode)
            if code:
                assert sm.symbol_index_for_code(preset, scale, code) is not None, (name, mode, code)
    assert seen_generator_codes <= set(generator_map(key)), sorted(
        seen_generator_codes - set(generator_map(key))
    )
    assert sm.drain_unmapped_codes() == {}


@pytest.mark.parametrize("preset,scale", [("forest_10000", 10000), ("sprint_2m", 4000), ("mtbo_10000", 10000)])
def test_aoi_frame_found_in_built_map(preset: str, scale: int) -> None:
    """Rám AOI (kód generátoru 708) se v sestavené mapě najde přes aoi_frame_bbox (708.0)."""
    import xml.etree.ElementTree as ET

    from app.pipeline.build_oom_map import build_oom_map_xml
    from app.pipeline.oom_preview import aoi_frame_bbox
    from app.pipeline.package_oom import build_aoi_boundary_part

    ref_x, ref_y = -745000.0, -1045000.0
    part = build_aoi_boundary_part(
        (14.40, 50.08, 14.42, 50.09),
        preset_id=preset, scale=scale, ref_x=ref_x, ref_y=ref_y, grivation_deg=0.0,
    )
    assert part is not None and part.count == 1
    xml = build_oom_map_xml(
        map_name="t", scale=scale, ref_x=ref_x, ref_y=ref_y, ref_lat=50.08, ref_lon=14.4,
        declination=0.0, grivation=0.0, templates=[], object_parts=[part], preset_id=preset,
    )
    assert 'code="708.0"' in xml
    root = ET.fromstring(xml.encode("utf-8"))
    bbox = aoi_frame_bbox(root, grivation_deg=0.0)
    assert bbox is not None
    assert bbox[2] > bbox[0] and bbox[3] > bbox[1]