from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

from app.pipeline import symbol_catalog as _sc
from app.pipeline.oom_symbols import symbol_set_path
from app.pipeline.symbol_catalog import catalog_entry, generator_map, uses_catalog
from app.pipeline.symbol_catalog import translate as catalog_translate

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

# ISMTBOM 2022 – čísla man-made se liší od ISOM (budova 526, ne 521).
_SYMBOL_NAME_TO_OOM_MTBO: dict[str, str] = {
    "building": "526",
    "farm": "401",
    "settlement": "527",
    "water": "301",
    "waterway": "306",
    "power line": "516",
    "blackline": "416",
}

# KP vectorconf → ISSprOM: ulice/chodníky jako footprinty z klíče (ne 501.1 hrana).
# Nezřetelnou 507 nepoužíváme – nejmenší je 506.
_SPRINT_ROAD_KP_TO_OOM: dict[str, str] = {
    "503": "501.31",  # sjízdná ulice (dřív OOM 501.19 ≈ 4 m → OCAD 501.31)
    "504": "506",  # nesjízdná / úzká
    "505": "505.1",  # cesta (unpaved footprint 1.4 m)
    "506": "506",  # pěšina (small unpaved)
    "507": "506",  # legacy KP 507 → 506
}

_FOREST_ROAD_KP_TO_OOM: dict[str, str] = {
    "503": "503",
    "504": "504",
    "505": "505",
    "506": "506",
    "507": "506",  # legacy – nezřetelnou pěšinu nekreslíme
}

# ISMTBOM: silnice 502/503; cesty/pěšiny řada 831–838 (track/path × rychlost).
# Legacy ISOM 504–507 jsou v sadě is_hidden a v OOM se nevykreslí.
_MTBO_ROAD_KP_TO_OOM: dict[str, str] = {
    "503": "502",  # silnice / sjízdná → Major road
    "504": "831",  # širší nesjízdná → Track: fast riding
    "505": "833",  # cesta → Track: medium riding
    "506": "834",  # pěšina → Path: medium riding
    "507": "834",  # legacy
}

# Výjimky podle vrstvy ZABAGED (blackline má víc významů).
_LAYER_OOM_CODE: dict[str, str] = {
    "StupenSraz": "104",
    "SkupinaBalvanu": "207",
    "LiniovaVegetace": "416",
    "ElektrickeVedeni": "510",
    "OvocnySadZahrada": "520",  # KP oliva 527 → OOM 520 (ne 413 sad)
    "VyznamnyStromLesik": "417",
    "MohylaPomnikNahrobek": "526",
    "KrizSloupKulturnihoVyznamu": "526",
    "OsamelyBalvanSkalaSkalniSuk": "204",
    "VezovitaStavba": "524",
}

_LAYER_OOM_CODE_MTBO: dict[str, str] = {
    "StupenSraz": "104",
    "SkupinaBalvanu": "207",
    "LiniovaVegetace": "416",
    "ElektrickeVedeni": "516",
    "OvocnySadZahrada": "527",
    "VyznamnyStromLesik": "418",
    "MohylaPomnikNahrobek": "537",
    "KrizSloupKulturnihoVyznamu": "537",
    # ISOM 204 Boulder → přebíráme vzhled z ISOM (v MTBO 204 je rocky pit).
    "OsamelyBalvanSkalaSkalniSuk": "204",
    "VezovitaStavba": "535",
}

_DXF_OOM_CODE_SPRINT: dict[str, str] = {
    "contours.dxf": "101",
    # c2g/c3g → default 104; cliffs_rock → 201 (viz oom_code_for_dxf).
    "cliffs_small.dxf": "104",
    "cliffs_large.dxf": "104",
    "dotknolls.dxf": "109",
    "dotdepressions.dxf": "111",
    "dotpits.dxf": "112",
}

_DXF_OOM_CODE_FOREST: dict[str, str] = {
    "contours.dxf": "101",
    "cliffs_small.dxf": "104",
    "cliffs_large.dxf": "104",
    "dotknolls.dxf": "109",
    "dotdepressions.dxf": "111",
    "dotpits.dxf": "112",
}

_CLIFF_DXF = frozenset({"cliffs_small.dxf", "cliffs_large.dxf"})
_ROCK_DXF = frozenset({"cliffs_rock.dxf"})
KP_CLIFF_EARTH_BANK = "earth_bank"
KP_CLIFF_AUTO = "auto"
# ISOM 206 = Gigantic boulder / massive cliff (area, plan shape).
KP_CLIFF_SYMBOL_206 = "symbol_206"
KP_CLIFF_OFF = "off"
# Hustý shluk skalních čárek → plocha (preferuj 201.2 / 206 před 210).
KP_CLIFF_DENSE_CODE = "210"
KP_CLIFF_206_CODE = "206"
KP_CLIFF_FACE_AREA_CODE = "201.2"


@lru_cache(maxsize=64)
def _uses_catalog(path: Path) -> bool:
    return uses_catalog(path)


@lru_cache(maxsize=8192)
def _translate_cached(path: Path, gen_code: str, catalog_file: Path) -> str | None:
    return catalog_translate(path, gen_code)


def _translate(path: Path, gen_code: str) -> str | None:
    """Čistý překlad kódu generátoru na kód sady (cachovaný; počítadlo chybějících
    kódů je mimo cache). ``catalog_file`` v klíči kvůli testům s jiným katalogem."""
    return _translate_cached(path, gen_code, _sc.CATALOG_PATH)

# Kódy generátoru bez symbolu v katalogové sadě: {(soubor sady, kód): počet dotazů}.
_UNMAPPED: Counter[tuple[str, str]] = Counter()


def set_code(preset_id: str, scale: int, gen_code: str) -> str | None:
    """Kód generátoru → kód symbolu v sadě použité pro (preset, měřítko).

    Nové ``*_cz.omap`` sady: překlad přes ``symbol_catalog.yaml`` (bez „+.0“ a bez
    fallbacku na syrový kód; neznámý kód → ``None``). Staré sady (OOM / merged /
    MTBO): identita.
    """
    path = symbol_set_path(preset_id, scale)
    if not _uses_catalog(path):
        return gen_code
    code = _translate(path, gen_code)
    if code is None:
        _UNMAPPED[(path.name, gen_code)] += 1
    return code


def preset_uses_catalog(preset_id: str, scale: int) -> bool:
    """True = preset kreslí do katalogové ``*_cz.omap`` sady (fallbacky na jiný kód
    se tu nesmí použít, chybějící kód se hlásí přes ``unmapped_codes_message``)."""
    return _uses_catalog(symbol_set_path(preset_id, scale))


def display_code(preset_id: str, scale: int, gen_code: str) -> str:
    """Kód pro texty a atributy výstupu (kód sady; při neznámém kódu původní)."""
    path = symbol_set_path(preset_id, scale)
    if not _uses_catalog(path):
        return gen_code
    return _translate(path, gen_code) or gen_code


def describe_code(gen_code: str) -> str:
    """Kód pro statické texty ve ZIPu (README) bez znalosti presetu.

    ZIP může obsahovat sprint i les; když se kód sady v ISOM a ISSprOM liší,
    vypíše se u obou. Kód, který katalog nezná, zůstane beze změny.
    """
    isom = generator_map("isom").get(gen_code)
    sprint = generator_map("sprint").get(gen_code)
    if isom and sprint:
        return isom if isom == sprint else f"ISOM {isom} / ISSprOM {sprint}"
    return isom or sprint or gen_code


def drain_unmapped_codes() -> dict[tuple[str, str], int]:
    """Vrátí a vynuluje {(sada, kód generátoru): počet dotazů bez symbolu}."""
    out = dict(_UNMAPPED)
    _UNMAPPED.clear()
    return out


def unmapped_codes_message(unmapped: dict[tuple[str, str], int]) -> str | None:
    """Text varování do logu jobu (nebo ``None``, když nic nechybí)."""
    if not unmapped:
        return None
    parts = [f"{code}×{n}" for (_set, code), n in sorted(unmapped.items())]
    set_name = sorted({s for s, _c in unmapped})[0]
    return (
        f"!!! Sada {set_name}: kód generátoru bez symbolu v katalogu – objekty "
        f"zahozeny nebo nahrazeny (kód×počet dotazů): {', '.join(parts)}"
    )


def _symbol_visible(preset_id: str, scale: int, code: str) -> bool:
    """True = symbol existuje a není ``is_hidden`` (Mapper by ho neukázal)."""
    path = symbol_set_path(preset_id, scale)
    if _uses_catalog(path):
        translated = _translate(path, code)
        if translated is None:
            return False
        code = translated
    text = path.read_text(encoding="utf-8")
    for m in re.finditer(r"<symbol\b([^>]*)>", text):
        attrs = m.group(1)
        if f'code="{code}"' not in attrs:
            continue
        if 'is_hidden="true"' in attrs:
            return False
        return True
    return False


def resolve_rock_area_code(preset_id: str, scale: int) -> str | None:
    """Kód plochy pro skalní shluk: ISOM 201.2, jinak 206, nouzově 210.

    Ve sprintu/MTBO není 201.2 plocha (je to čára tagů) – hned 206.
    ISOM 201.2 je v sadě ``is_hidden`` (migrace ISOM2000) → přeskočit, jinak
    objekty v .omap jsou, ale Mapper je nekreslí (0 viditelných skal).
    """
    candidates: list[str] = []
    if not _is_sprint(preset_id) and not _is_mtbo(preset_id):
        candidates.append(KP_CLIFF_FACE_AREA_CODE)
    candidates.extend([KP_CLIFF_206_CODE, KP_CLIFF_DENSE_CODE])
    path = symbol_set_path(preset_id, scale)
    for code in candidates:
        if not _has_symbol(preset_id, scale, code):
            continue
        if not _symbol_visible(preset_id, scale, code):
            continue
        if _uses_catalog(path) and not _is_area_symbol(path, code):
            # V OCAD sadě je např. ISOM 201.2 čára (útes bez čárek), ne plocha.
            continue
        return code
    return None


def _is_area_symbol(path: Path, gen_code: str) -> bool:
    ocad = _translate(path, gen_code)
    entry = catalog_entry(path, ocad) if ocad else None
    return bool(entry) and entry.get("geom") == "area"


def _is_sprint(preset_id: str) -> bool:
    return preset_id.startswith("sprint")


def _is_mtbo(preset_id: str) -> bool:
    return preset_id.startswith("mtbo")


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
    if _uses_catalog(path):
        set_symbol_code = _translate(path, code)
        if set_symbol_code is None:
            _UNMAPPED[(path.name, code)] += 1
            return None
        return _code_to_index(path).get(set_symbol_code)
    return _code_to_index(path).get(code)


def _has_symbol(preset_id: str, scale: int, code: str) -> bool:
    """Jako ``symbol_index_for_code(...) is not None``, ale bez záznamu do _UNMAPPED
    (jen sondování kandidátů)."""
    path = symbol_set_path(preset_id, scale)
    if _uses_catalog(path):
        translated = _translate(path, code)
        return translated is not None and translated in _code_to_index(path)
    return code in _code_to_index(path)


def _layer_oom_code(layer: str, preset_id: str) -> str | None:
    table = _LAYER_OOM_CODE_MTBO if _is_mtbo(preset_id) else _LAYER_OOM_CODE
    code = table.get(layer)
    if code:
        return code
    if layer == "Zed":
        if _is_sprint(preset_id):
            return "513.2"
        if _is_mtbo(preset_id):
            return "521"  # Stone wall
        return "513"
    if layer == "HradbaVal":
        if _is_sprint(preset_id):
            return "518"
        if _is_mtbo(preset_id):
            return "521"
        return "513"
    if layer == "RozvalinaZricenina":
        if _is_sprint(preset_id):
            return "521"
        if _is_mtbo(preset_id):
            return "530"
        return "523"
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
        if _is_sprint(preset_id):
            road_map = _SPRINT_ROAD_KP_TO_OOM
        elif _is_mtbo(preset_id):
            road_map = _MTBO_ROAD_KP_TO_OOM
        else:
            road_map = _FOREST_ROAD_KP_TO_OOM
        return road_map.get(kp)
    if symbol_name == "railway":
        if _is_sprint(preset_id):
            return "509.1"
        if _is_mtbo(preset_id):
            return "515"
        return "509"
    if symbol_name == "tramway":
        if _is_sprint(preset_id):
            return "509.2"
        if _is_mtbo(preset_id):
            return "515"
        return "509"
    if symbol_name == "parking":
        # Zpevněná plocha musí být v CRT POD silnicemi (nižší symbol id).
        # Forest 501.1 / MTBO 529 sedí nad road-path → Ostatní plocha překryje Ulice.
        if _is_sprint(preset_id):
            return "501.0"
        if _is_mtbo(preset_id):
            return "501.0"
        return "501"
    if symbol_name == "fence":
        if _is_sprint(preset_id):
            return "518"
        if _is_mtbo(preset_id):
            return "522"
        return "516"

    name_map = _SYMBOL_NAME_TO_OOM_MTBO if _is_mtbo(preset_id) else _SYMBOL_NAME_TO_OOM
    mapped = name_map.get(symbol_name)
    if mapped and _has_symbol(preset_id, scale, mapped):
        return mapped
    if _has_symbol(preset_id, scale, kp):
        return kp
    return None


def oom_code_for_dxf(
    filename: str,
    *,
    preset_id: str,
    cliff_symbol: str = KP_CLIFF_AUTO,
) -> str | None:
    """``auto``: zem z cliffs_small/large (104), skála z cliffs_rock (201→plocha).

    ``earth_bank`` / ``symbol_206`` přebarví obě sady.
    ``off`` srázy vynechá. Knolly (dotknolls) symbol nemění.
    """
    if filename in _ROCK_DXF or filename in _CLIFF_DXF:
        if cliff_symbol == KP_CLIFF_OFF:
            return None
        if cliff_symbol == KP_CLIFF_SYMBOL_206:
            return KP_CLIFF_206_CODE
        if cliff_symbol == KP_CLIFF_EARTH_BANK:
            return "104"
        if filename in _ROCK_DXF:
            return "201"
        return "104"
    if filename == "dotdepressions.dxf" and preset_id.startswith("mtbo"):
        return "112"  # ISMTBOM nemá Small depression 111
    table = _DXF_OOM_CODE_SPRINT if _is_sprint(preset_id) else _DXF_OOM_CODE_FOREST
    return table.get(filename)
