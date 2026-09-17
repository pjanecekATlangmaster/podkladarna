from __future__ import annotations

from pathlib import Path

import pytest

from app.pipeline.oom_symbols import colors_and_symbols_xml, symbol_set_path


def test_symbol_set_path_sprint():
    path = symbol_set_path("sprint_2m", 4000)
    assert path.name == "ISSprOM_2019_4000.omap"
    assert path.is_file()


def test_symbol_set_path_forest():
    path = symbol_set_path("forest_7500", 7500)
    assert path.name == "ISOM_2017-2_10000.omap"


def test_symbol_set_path_mtbo():
    path10 = symbol_set_path("mtbo_10000", 10000)
    assert path10.name == "ISMTBOM_10000.omap"
    assert path10.is_file()
    path15 = symbol_set_path("mtbo_15000", 15000)
    assert path15.name == "ISMTBOM_15000.omap"
    assert path15.is_file()


def test_colors_and_symbols_xml():
    path = symbol_set_path("sprint_2m", 4000)
    colors, symbols = colors_and_symbols_xml(path)
    assert colors.startswith('<colors count="')
    assert symbols.startswith('<symbols count="')
    assert "line_symbol" in symbols


def test_contour_symbols_are_protected():
    path = symbol_set_path("forest_10000", 10000)
    _, symbols = colors_and_symbols_xml(path)
    for code in ("101", "102"):
        m = __import__("re").search(
            rf'<symbol\b[^>]*\bcode="{code}"[^>]*>',
            symbols,
        )
        assert m is not None
        assert 'is_protected="true"' in m.group(0)


def test_mtbo_embedded_symbols_are_unhidden():
    """ISMTBOM má skryté legacy 504–507 i vegetaci – v .omap musí jít kreslit."""
    path = symbol_set_path("mtbo_10000", 10000)
    raw = path.read_text(encoding="utf-8")
    assert 'is_hidden="true"' in raw
    _, symbols = colors_and_symbols_xml(path)
    assert "is_hidden" not in symbols
    for code in ("502", "831", "833", "834", "408", "410"):
        assert f'code="{code}"' in symbols


def test_mtbo_overlays_isom_earth_bank_knoll_farmland():
    """MTBO bere z ISOM zemní sráz, kopeček a obdělávané pole (správný typ/vzhled)."""
    path = symbol_set_path("mtbo_10000", 10000)
    _, symbols = colors_and_symbols_xml(path)
    earth = __import__("re").search(
        r'<symbol\b[^>]*\bcode="104"[^>]*>.*?</symbol>',
        symbols,
        __import__("re").DOTALL,
    )
    assert earth is not None
    assert 'type="2"' in earth.group(0)  # line, ne point Slope line
    assert "Earth bank" in earth.group(0) or "line_symbol" in earth.group(0)

    knoll = __import__("re").search(
        r'<symbol\b[^>]*\bcode="109"[^>]*>',
        symbols,
    )
    assert knoll is not None
    assert 'type="1"' in knoll.group(0)  # point knoll

    farm = __import__("re").search(
        r'<symbol\b[^>]*\bcode="415"[^>]*>',
        symbols,
    )
    assert farm is not None
    # Combined cultivated land z ISOM 412.
    assert 'type="16"' in farm.group(0) or "Cultivated" in symbols


def test_mtbo_building_is_gray_not_solid_black():
    """ISMTBOM 526 Building = Black 70 % (šedá), ne plná černá."""
    import re

    path = symbol_set_path("mtbo_10000", 10000)
    text = path.read_text(encoding="utf-8")
    building = re.search(
        r'<symbol[^>]*\bcode="526"[^>]*>.*?</symbol>',
        text,
        re.DOTALL,
    )
    assert building is not None
    assert 'inner_color="3"' in building.group(0)
    # priority="3" = Black 70 %
    assert re.search(
        r'<color[^>]*\bpriority="3"[^>]*\bname="Black 70%"',
        text,
    )


def test_symbol_color_refs_exist_in_palette():
    """Footprinty/zpevněné plochy musí odkazovat na existující barvy v sadě."""
    import re

    for preset, scale in (
        ("sprint_2m", 4000),
        ("mtbo_10000", 10000),
        ("mtbo_15000", 15000),
    ):
        text = symbol_set_path(preset, scale).read_text(encoding="utf-8")
        priorities = {
            int(m.group(1))
            for m in re.finditer(r'<color\b[^>]*\bpriority="(\d+)"', text)
        }
        missing: set[tuple[str, int]] = set()
        for sm in re.finditer(r"<symbol\b.*?</symbol>", text, re.DOTALL):
            code_m = re.search(r'\bcode="([^"]+)"', sm.group(0))
            code = code_m.group(1) if code_m else "?"
            for cm in re.finditer(
                r'\b(?:inner_|outer_)?color="(\d+)"', sm.group(0)
            ):
                c = int(cm.group(1))
                if c not in priorities:
                    missing.add((code, c))
        assert not missing, f"{preset}: dangling color refs {sorted(missing)[:20]}"


def test_issprom_lower_brown_above_yellow():
    """ISSprOM: Lower brown (ulice 501.x) musí být nad žlutou 401, jinak cesty zmizí."""
    import re

    text = symbol_set_path("sprint_2m", 4000).read_text(encoding="utf-8")
    by_name: dict[str, int] = {}
    for m in re.finditer(
        r'<color\b[^>]*\bpriority="(\d+)"[^>]*\bname="([^"]+)"', text
    ):
        by_name[m.group(2)] = int(m.group(1))
    # Nižší priority = výše ve výkresu.
    assert by_name["Lower brown 50%"] < by_name["Yellow 100%"]
    assert by_name["Lower brown 30%"] < by_name["Yellow 100%"]
    assert by_name["Black below lower light brown"] < by_name["Yellow 100%"]
    # Footprint ulice používá tyto barvy.
    road = re.search(
        r'<symbol[^>]*\bcode="501.17"[^>]*>.*?</symbol>', text, re.DOTALL
    )
    assert road is not None
    assert 'color="13"' in road.group(0)
    assert 'color="15"' in road.group(0)


def test_symbol_set_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.oom_symbols.OOM_DIR",
        tmp_path / "missing",
    )
    with pytest.raises(FileNotFoundError):
        symbol_set_path("sprint_2m", 4000)
