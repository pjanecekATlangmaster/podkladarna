from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from app.settings import CONFIG_DIR

OOM_DIR = CONFIG_DIR / "oom"

# Vrstevnice GDAL – zamčené, ať se při editaci omylem nepohnou.
_PROTECTED_SYMBOL_CODES = frozenset({"101", "102"})

# MTBO sada: cílový kód → ISOM kód (vzhled z lesního klíče).
# ISMTBOM má u těchto čísel jiný význam, špatný typ, nebo skrytý symbol.
_MTBO_ISOM_OVERLAY: dict[str, str] = {
    "104": "104",  # Earth bank (ne Slope line)
    "109": "109",  # Small knoll (ne Erosion gully)
    "204": "204",  # Boulder
    "206": "206",  # Gigantic boulder (plocha)
    "207": "207",  # Boulder cluster
    "408": "408",  # Vegetation: walk
    "409": "409",
    "410": "410",  # Vegetation: fight
    "415": "412",  # Cultivated land (ISOM 412 → náš farmland kód 415)
    "416": "416",  # Distinct vegetation boundary
    "522": "516",  # Fence
    "530": "523",  # Ruin
    "537": "526",  # Cairn (ISOM 526; v MTBO 526 = budova)
}


def symbol_set_path(preset_id: str, scale: int) -> Path:
    """Vrátí oficiální OOM symbol set (OpenOrienteering/mapper, GPL)."""
    if preset_id.startswith("sprint"):
        path = OOM_DIR / "ISSprOM_2019_4000.omap"
    elif preset_id.startswith("mtbo"):
        # ISMTBOM: základní 1:15000; 1:10000 je 1.5× zvětšení (viz OOM README).
        if scale >= 15000:
            path = OOM_DIR / "ISMTBOM_15000.omap"
        else:
            path = OOM_DIR / "ISMTBOM_10000.omap"
    elif scale >= 15000:
        path = OOM_DIR / "ISOM_2017-2_15000.omap"
    else:
        # ISOM 2017-2 pro 1:7500 i 1:10000 (OOM nemá samostatný set pro 7500)
        path = OOM_DIR / "ISOM_2017-2_10000.omap"
    if not path.is_file():
        raise FileNotFoundError(f"Chybí symbol set OOM: {path}")
    return path


def _isom_donor_path(mtbo_path: Path) -> Path:
    if "15000" in mtbo_path.name:
        return OOM_DIR / "ISOM_2017-2_15000.omap"
    return OOM_DIR / "ISOM_2017-2_10000.omap"


@lru_cache(maxsize=8)
def _load_fragments(path_str: str) -> tuple[str, str]:
    text = Path(path_str).read_text(encoding="utf-8")
    colors = re.search(r"<colors\s+count=\"\d+\"[^>]*>.*?</colors>", text, re.DOTALL)
    symbols = re.search(r"<symbols\s+count=\"\d+\"[^>]*>.*?</symbols>", text, re.DOTALL)
    if not colors or not symbols:
        raise ValueError(f"Neplatný symbol set OOM: {path_str}")
    return colors.group(0), symbols.group(0)


def protect_symbol_codes(symbols_xml: str, codes: frozenset[str] | set[str]) -> str:
    """Nastaví is_protected=\"true\" u symbolů s danými ISOM kódy."""

    def _patch(match: re.Match[str]) -> str:
        tag = match.group(0)
        code = match.group(1)
        if code not in codes:
            return tag
        if re.search(r'\bis_protected="', tag):
            return re.sub(r'\bis_protected="[^"]*"', 'is_protected="true"', tag)
        return tag[:-1] + ' is_protected="true">'

    return re.sub(
        r'<symbol\b[^>]*\bcode="([^"]+)"[^>]*/?>',
        _patch,
        symbols_xml,
    )


def unhide_all_symbols(symbols_xml: str) -> str:
    """OOM u is_hidden nevykreslí objekty – v generovaném .omap vše odkrýt."""
    return re.sub(r'\s+is_hidden="true"', "", symbols_xml)


def _extract_symbol_element(symbols_xml: str, code: str) -> str | None:
    """Vrátí celý top-level <symbol …>…</symbol> s daným code (včetně vnořených)."""
    m = re.search(
        rf'<symbol\b(?=[^>]*\bid=")(?=[^>]*\bcode="{re.escape(code)}")[^>]*>',
        symbols_xml,
    )
    if not m:
        return None
    start = m.start()
    pos = m.end()
    depth = 1
    while depth and pos < len(symbols_xml):
        nxt_open = symbols_xml.find("<symbol", pos)
        nxt_close = symbols_xml.find("</symbol>", pos)
        if nxt_close < 0:
            return None
        if nxt_open >= 0 and nxt_open < nxt_close:
            depth += 1
            pos = nxt_open + 7
        else:
            depth -= 1
            pos = nxt_close + len("</symbol>")
    return symbols_xml[start:pos]


def _parse_color_entries(colors_xml: str) -> list[tuple[int, str]]:
    """[(priority, full <color>…</color>), …]"""
    out: list[tuple[int, str]] = []
    for m in re.finditer(r"<color\b[^>]*>.*?</color>", colors_xml, re.DOTALL):
        pri_m = re.search(r'\bpriority="(\d+)"', m.group(0))
        if not pri_m:
            continue
        out.append((int(pri_m.group(1)), m.group(0)))
    return out


def _color_refs_in_symbol(symbol_xml: str) -> set[int]:
    refs: set[int] = set()
    for m in re.finditer(
        r'\b(?:inner_|outer_)?color="(-?\d+)"',
        symbol_xml,
    ):
        val = int(m.group(1))
        if val >= 0:
            refs.add(val)
    return refs


def _remap_colors_in_xml(xml: str, color_map: dict[int, int]) -> str:
    def repl(match: re.Match[str]) -> str:
        prefix, num = match.group(1), int(match.group(2))
        if num < 0:
            return match.group(0)
        return f'{prefix}="{color_map.get(num, num)}"'

    return re.sub(
        r'\b((?:inner_|outer_)?color)="(-?\d+)"',
        repl,
        xml,
    )


def _next_symbol_id(symbols_xml: str) -> int:
    ids = [int(x) for x in re.findall(r'<symbol\b[^>]*\bid="(\d+)"', symbols_xml)]
    return (max(ids) + 1) if ids else 0


def _replace_symbol_keep_id_code(
    symbols_xml: str,
    target_code: str,
    new_symbol_xml: str,
) -> str:
    """Nahradí symbol target_code; zachová id a code z cílového setu."""
    old = _extract_symbol_element(symbols_xml, target_code)
    if not old:
        return symbols_xml
    id_m = re.search(r'\bid="(\d+)"', old)
    if not id_m:
        return symbols_xml
    target_id = id_m.group(1)
    # Přepsat type/id/code v otevíracím tagu donoru.
    patched = re.sub(
        r"<symbol\b([^>]*)>",
        lambda m: _retarget_open_tag(m.group(1), target_id, target_code),
        new_symbol_xml,
        count=1,
    )
    patched = re.sub(r'\s+is_hidden="true"', "", patched)
    return symbols_xml.replace(old, patched, 1)


def _retarget_open_tag(attrs: str, symbol_id: str, code: str) -> str:
    attrs = re.sub(r'\bid="[^"]*"', f'id="{symbol_id}"', attrs)
    attrs = re.sub(r'\bcode="[^"]*"', f'code="{code}"', attrs)
    attrs = re.sub(r'\s+is_hidden="true"', "", attrs)
    if not re.search(r'\bid="', attrs):
        attrs += f' id="{symbol_id}"'
    if not re.search(r'\bcode="', attrs):
        attrs += f' code="{code}"'
    return f"<symbol{attrs}>"


def merge_isom_overlay_into_mtbo(
    colors_xml: str,
    symbols_xml: str,
    isom_path: Path,
    overlay: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Do ISMTBOM doplní vzhled vybraných symbolů z ISOM (barvy + definice)."""
    overlay = overlay or _MTBO_ISOM_OVERLAY
    _isom_colors, isom_symbols = _load_fragments(str(isom_path.resolve()))
    isom_color_entries = _parse_color_entries(_isom_colors)
    isom_by_pri = {pri: xml for pri, xml in isom_color_entries}

    mtbo_colors = _parse_color_entries(colors_xml)
    next_pri = max((pri for pri, _ in mtbo_colors), default=-1) + 1
    color_map: dict[int, int] = {}
    extra_colors: list[str] = []

    # ISOM 401 Open land – part cultivated land; mapuj na MTBO 401 pokud je.
    mtbo_401 = _extract_symbol_element(symbols_xml, "401")
    mtbo_401_id = None
    if mtbo_401:
        m = re.search(r'\bid="(\d+)"', mtbo_401)
        if m:
            mtbo_401_id = int(m.group(1))

    extras_to_append: list[str] = []
    part_id_map: dict[int, int] = {}  # ISOM symbol id → new/existing id

    _ISOM_TO_MTBO_SPOTCOLOR = {
        0: 0,   # Purple
        2: 1,   # Black
        3: 17,  # Green
        5: 9,   # Blue
        6: 6,   # Brown
        32: 21, # Yellow
    }

    def ensure_isom_color(isom_pri: int) -> int:
        nonlocal next_pri
        if isom_pri in color_map:
            return color_map[isom_pri]
        src = isom_by_pri.get(isom_pri)
        if not src:
            color_map[isom_pri] = isom_pri
            return isom_pri
        new_xml = re.sub(
            r'\bpriority="\d+"',
            f'priority="{next_pri}"',
            src,
            count=1,
        )
        new_xml = re.sub(
            r'\bspotcolor="(\d+)"',
            lambda m: f'spotcolor="{_ISOM_TO_MTBO_SPOTCOLOR.get(int(m.group(1)), m.group(1))}"',
            new_xml,
        )
        extra_colors.append(new_xml)
        color_map[isom_pri] = next_pri
        next_pri += 1
        return color_map[isom_pri]

    def import_isom_symbol_xml(isom_code: str) -> str | None:
        frag = _extract_symbol_element(isom_symbols, isom_code)
        if not frag:
            return None
        for pri in _color_refs_in_symbol(frag):
            ensure_isom_color(pri)
        return _remap_colors_in_xml(frag, color_map)

    # Cultivated land: nejdřív 412.1 pattern jako extra symbol.
    pattern = import_isom_symbol_xml("412.1")
    pattern_new_id = None
    if pattern and overlay.get("415") == "412":
        pattern_new_id = _next_symbol_id(symbols_xml)
        pattern = re.sub(r'\bid="\d+"', f'id="{pattern_new_id}"', pattern, count=1)
        pattern = re.sub(r'\s+is_hidden="true"', "", pattern)
        extras_to_append.append(pattern)
        raw_pat = _extract_symbol_element(isom_symbols, "412.1")
        if raw_pat:
            pm = re.search(r'\bid="(\d+)"', raw_pat)
            if pm:
                part_id_map[int(pm.group(1))] = pattern_new_id

    if mtbo_401_id is not None:
        raw_401 = _extract_symbol_element(isom_symbols, "401")
        if raw_401:
            pm = re.search(r'\bid="(\d+)"', raw_401)
            if pm:
                part_id_map[int(pm.group(1))] = mtbo_401_id

    for target_code, isom_code in overlay.items():
        donor = import_isom_symbol_xml(isom_code)
        if not donor:
            continue
        # Remap combined parts.
        if part_id_map:

            def _part_repl(match: re.Match[str]) -> str:
                old = int(match.group(1))
                return f'part symbol="{part_id_map.get(old, old)}"'

            donor = re.sub(r'part symbol="(\d+)"', _part_repl, donor)
        if _extract_symbol_element(symbols_xml, target_code):
            symbols_xml = _replace_symbol_keep_id_code(
                symbols_xml, target_code, donor
            )
        else:
            # Přidej nový kód na konec.
            new_id = _next_symbol_id(symbols_xml + "".join(extras_to_append))
            donor = re.sub(r'\bid="\d+"', f'id="{new_id}"', donor, count=1)
            donor = re.sub(r'\bcode="[^"]*"', f'code="{target_code}"', donor, count=1)
            extras_to_append.append(donor)

    if extras_to_append:
        symbols_xml = re.sub(
            r"</symbols>\s*$",
            "".join(extras_to_append) + "</symbols>",
            symbols_xml,
            count=1,
        )
        # Update count.
        n = len(re.findall(r'<symbol\b[^>]*\bid="\d+"', symbols_xml))
        symbols_xml = re.sub(
            r'<symbols count="\d+"',
            f'<symbols count="{n}"',
            symbols_xml,
            count=1,
        )

    if extra_colors:
        # Insert before </colors>
        colors_xml = re.sub(
            r"</colors>\s*$",
            "".join(extra_colors) + "</colors>",
            colors_xml,
            count=1,
        )
        n = len(_parse_color_entries(colors_xml))
        colors_xml = re.sub(
            r'<colors count="\d+"',
            f'<colors count="{n}"',
            colors_xml,
            count=1,
        )

    return colors_xml, symbols_xml


def colors_and_symbols_xml(symbol_set: Path) -> tuple[str, str]:
    colors, symbols = _load_fragments(str(symbol_set.resolve()))
    symbols = unhide_all_symbols(symbols)
    if symbol_set.name.startswith("ISMTBOM"):
        colors, symbols = merge_isom_overlay_into_mtbo(
            colors,
            symbols,
            _isom_donor_path(symbol_set),
        )
    return colors, protect_symbol_codes(symbols, _PROTECTED_SYMBOL_CODES)
