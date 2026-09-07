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

# KP vectorconf → ISSprOM: 501.11 je PLOCHA; ulice musí být liniový footprint.
_SPRINT_ROAD_KP_TO_OOM: dict[str, str] = {
    "503": "501.17",  # sjízdná ulice / silnice (~2 m, heavy traffic line)
    "504": "506",  # nesjízdná / úzká
    "505": "505.1",  # cesta
    "507": "507",  # pěšina
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
    "OvocnySadZahrada": "520",  # KP oliva 527 → OOM 520 (ne 413 sad)
    "VyznamnyStromLesik": "417",
    "MohylaPomnikNahrobek": "526",
    "KrizSloupKulturnihoVyznamu": "526",
    "OsamelyBalvanSkalaSkalniSuk": "204",
    "VezovitaStavba": "524",
}

_DXF_OOM_CODE_SPRINT: dict[str, str] = {
    "contours.dxf": "101",
    # KP c2g/c3g → default 104; volba rock_face → 201 (viz oom_code_for_dxf).
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

_CLIFF_DXF = frozenset({"cliffs_small.dxf", "cliffs_large.dxf"})
KP_CLIFF_EARTH_BANK = "earth_bank"
KP_CLIFF_ROCK_FACE = "rock_face"
# Hustý shluk KP čárek → kamenitý povrch (plocha), ne 206 (bod).
KP_CLIFF_DENSE_CODE = "210"


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


def oom_code_for_dxf(
    filename: str,
    *,
    preset_id: str,
    cliff_symbol: str = KP_CLIFF_EARTH_BANK,
) -> str | None:
    if filename in _CLIFF_DXF:
        if cliff_symbol == KP_CLIFF_ROCK_FACE:
            return "201"
        return "104"
    table = _DXF_OOM_CODE_SPRINT if _is_sprint(preset_id) else _DXF_OOM_CODE_FOREST
    return table.get(filename)
