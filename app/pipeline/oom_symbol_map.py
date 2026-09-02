from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from app.pipeline.oom_symbols import symbol_set_path

# KP vectorconf číslo ≠ vždy stejné IOF číslo v ISSprOM – přemapování podle typu symbolu.
_SYMBOL_NAME_TO_OOM: dict[str, str] = {
    "building": "521",
    "parking": "501",
    "farm": "401",
    "settlement": "520",
    "water": "301",
    "waterway": "306",
    "railway": "509.1",
    "tramway": "509.2",
    "power line": "510",
    "fence": "518",
    "blackline": "416",
}

_ROAD_PATH_KP_TO_OOM: dict[str, str] = {
    "503": "501.11",
    "504": "506",
    "505": "505.1",
    "507": "507",
}

# Výjimky podle vrstvy ZABAGED (blackline má víc významů).
_LAYER_OOM_CODE: dict[str, str] = {
    "StupenSraz": "201",
    "SkupinaBalvanu": "212",
    "LiniovaVegetace": "416",
    "ElektrickeVedeni": "510",
}

_DXF_OOM_CODE: dict[str, str] = {
    "contours.dxf": "101",
    "cliffs_small.dxf": "201",
    "cliffs_large.dxf": "201",
    "dotknolls.dxf": "109",
}


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


def oom_code_for_vectorconf_rule(
    symbol_name: str,
    kp_code: str,
    layer: str,
    *,
    preset_id: str,
    scale: int,
) -> str | None:
    layer_code = _LAYER_OOM_CODE.get(layer)
    if layer_code:
        return layer_code
    kp = kp_code.rstrip("Tt")
    if symbol_name == "road-path":
        return _ROAD_PATH_KP_TO_OOM.get(kp)
    known = _code_to_index(symbol_set_path(preset_id, scale))
    return _SYMBOL_NAME_TO_OOM.get(symbol_name) or (kp if kp in known else None)


def oom_code_for_dxf(filename: str) -> str | None:
    return _DXF_OOM_CODE.get(filename)
