from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from app.settings import CONFIG_DIR

OOM_DIR = CONFIG_DIR / "oom"

# Vrstevnice GDAL – zamčené, ať se při editaci omylem nepohnou.
_PROTECTED_SYMBOL_CODES = frozenset({"101", "102"})


def symbol_set_path(preset_id: str, scale: int) -> Path:
    """Vrátí oficiální OOM symbol set (OpenOrienteering/mapper, GPL)."""
    if preset_id.startswith("sprint"):
        path = OOM_DIR / "ISSprOM_2019_4000.omap"
    elif scale >= 15000:
        path = OOM_DIR / "ISOM_2017-2_15000.omap"
    else:
        # ISOM 2017-2 pro 1:7500 i 1:10000 (OOM nemá samostatný set pro 7500)
        path = OOM_DIR / "ISOM_2017-2_10000.omap"
    if not path.is_file():
        raise FileNotFoundError(f"Chybí symbol set OOM: {path}")
    return path


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


def colors_and_symbols_xml(symbol_set: Path) -> tuple[str, str]:
    colors, symbols = _load_fragments(str(symbol_set.resolve()))
    return colors, protect_symbol_codes(symbols, _PROTECTED_SYMBOL_CODES)
