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


def test_symbol_set_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.oom_symbols.OOM_DIR",
        tmp_path / "missing",
    )
    with pytest.raises(FileNotFoundError):
        symbol_set_path("sprint_2m", 4000)
