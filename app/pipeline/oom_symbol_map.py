from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from app.pipeline.oom_symbols import symbol_set_path

# KP vectorconf číslo ≠ vždy stejné IOF číslo v ISSprOM / ISOM.
_SYMBOL_NAME_TO_OOM: dict[str, str] = {
    "building": "521",
    "farm": "401",
    "settlement": "520",
    "water": "301",
    "waterway": "306",
    "power line": "510",
    "blackline": "416",
}

_SPRINT_ROAD_KP_TO_OOM: dict[str, str] = {
    "503": "501.11",
    "504": "506",
    "505": "505.1",
    "507": "507",
}

_FOREST_ROAD_KP_TO_OOM: dict[str, str] = {
    "503": "503",
    "504": "504",
    "505": "505",
    "507": "507",
}

# Výjimky podle vrstvy ZABAGED (blackline má víc významů).
_LAYER_OOM_CODE: dict[str, str] = {
    "StupenSraz": "104",
    "SkupinaBalvanu": "207",
    "LiniovaVegetace": "416",
    "ElektrickeVedeni": "510",
    "LesniPudaSKrovinatymPorostem": "405",
    "OvocnySadZahrada": "413",
    "VyznamnyStromLesik": "417",
    "MohylaPomnikNahrobek": "526",
    "KrizSloupKulturnihoVyznamu": "526",
    "OsamelyBalvanSkalaSkalniSuk": "204",
    "VezovitaStavba": "524",
}

_DXF_OOM_CODE_SPRINT: dict[str, str] = {
    "contours.dxf": "101",
    # KP c2g/c3g → zemní sráz 104 (201 jen ručně při mapování)
    "cliffs_small.dxf": "104",
    "cliffs_large.dxf": "104",
    "dotknolls.dxf": "109",
}

_DXF_OOM_CODE_FOREST: dict[str, str] = {
    "contours.dxf": "101",
    "cliffs_small.dxf": "104",
    "cliffs_large.dxf": "104",
    "dotknolls.dxf": "109",
}


def _is_sprint(preset_id: str) -> bool:
    return preset_id.startswith("sprint")


@lru_cache(maxsize=8)
def _code_to_index(symbol_set: Path) -> dict[str, int]:
    text = symbol_set.read_text(encoding="utf-8")
    out: dict[str, int] = {}
    for m in re.finditer(
        r'<symbol[^>]*\bid="(\d+)"[^>]*\bcode="([^"]+)"',
        text,
    ):
        out[m.group(2)] = int(m.group(1))
    return out


def symbol_index_for_code(preset_id: str, scale: int, code: str) -> int | None:
    path = symbol_set_path(preset_id, scale)
    return _code_to_index(path).get(code)


def _layer_oom_code(layer: str, preset_id: str) -> str | None:
    code = _LAYER_OOM_CODE.get(layer)
    if code:
        return code
    if layer == "Zed":
        return "513.2" if _is_sprint(preset_id) else "513"
    if layer == "HradbaVal":
        return "518" if _is_sprint(preset_id) else "513"
    if layer == "RozvalinaZricenina":
        return "521" if _is_sprint(preset_id) else "523"
    return None


def oom_code_for_vectorconf_rule(
    symbol_name: str,
    kp_code: str,
    layer: str,
    *,
    preset_id: str,
    scale: int,
) -> str | None:
    layer_code = _layer_oom_code(layer, preset_id)
    if layer_code:
        return layer_code

    kp = kp_code.rstrip("Tt")
    if symbol_name == "road-path":
        road_map = _SPRINT_ROAD_KP_TO_OOM if _is_sprint(preset_id) else _FOREST_ROAD_KP_TO_OOM
        return road_map.get(kp)
    if symbol_name == "railway":
        return "509.1" if _is_sprint(preset_id) else "509"
    if symbol_name == "tramway":
        return "509.2" if _is_sprint(preset_id) else "509"
    if symbol_name == "parking":
        return "501" if _is_sprint(preset_id) else "501.1"
    if symbol_name == "fence":
        return "518" if _is_sprint(preset_id) else "516"

    known = _code_to_index(symbol_set_path(preset_id, scale))
    mapped = _SYMBOL_NAME_TO_OOM.get(symbol_name)
    if mapped and mapped in known:
        return mapped
    if kp in known:
        return kp
    return None


def oom_code_for_dxf(filename: str, *, preset_id: str) -> str | None:
    table = _DXF_OOM_CODE_SPRINT if _is_sprint(preset_id) else _DXF_OOM_CODE_FOREST
    return table.get(filename)
