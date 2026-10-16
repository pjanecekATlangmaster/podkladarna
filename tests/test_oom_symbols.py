from __future__ import annotations

from pathlib import Path

import pytest

from app.pipeline.oom_symbols import colors_and_symbols_xml, symbol_set_path


def test_symbol_set_path_sprint():
    path = symbol_set_path("sprint_2m", 4000)
    assert path.name == "ISSprOM_2019_4000_cz.omap"
    assert path.is_file()


def test_symbol_set_path_forest():
    path = symbol_set_path("forest_7500", 7500)
    assert path.name == "ISOM_2017_10000_cz.omap"
    assert symbol_set_path("forest_10000", 10000).name == "ISOM_2017_10000_cz.omap"
    assert symbol_set_path("forest_10000", 15000).name == "ISOM_2017_15000_cz.omap"


def test_symbol_set_path_mtbo():
    path10 = symbol_set_path("mtbo_10000", 10000)
    assert path10.name == "ISMTBOM_2022_10000_cz.omap"
    assert path10.is_file()
    path15 = symbol_set_path("mtbo_15000", 15000)
    assert path15.name == "ISMTBOM_2022_15000_cz.omap"
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
    # Kódy generátoru 101/102 → OCAD 101.0/102.0 (symbol_catalog.yaml).
    for code in ("101.0", "102.0"):
        m = __import__("re").search(
            rf'<symbol\b[^>]*\bcode="{code}"[^>]*>',
            symbols,
        )
        assert m is not None
        assert 'is_protected="true"' in m.group(0)


def test_mtbo_set_keeps_ocad_hidden_flags():
    """ISMTBOM 2022: hidden symboly zůstávají jako v OCADu (odkryjí se až při použití)."""
    path = symbol_set_path("mtbo_10000", 10000)
    raw = path.read_text(encoding="utf-8")
    assert 'is_hidden="true"' in raw
    _, symbols = colors_and_symbols_xml(path)
    assert 'is_hidden="true"' in symbols
    for code in ("101.0", "815.0", "817.0", "818.0", "103.0", "111.0", "408.0"):
        assert f'code="{code}"' in symbols


def test_mtbo_catalog_translates_generator_codes():
    """Kódy generátoru (číslování dřívějšího ISMTBOM) → kódy OCAD sady ISMTBOM 2022."""
    from app.pipeline.symbol_catalog import generator_map

    gm = generator_map("mtbo")
    assert gm["101"] == "101.0"
    assert gm["526"] == "521.0"  # budova
    assert gm["831"] == "815.0" and gm["833"] == "817.0" and gm["834"] == "816.0"
    assert gm["415"] == "412.0"
    assert gm["416"] == "416.1"


def test_sprint_colors_and_symbols_drop_issom_legacy():
    path = symbol_set_path("sprint_2m", 4000)
    _, symbols = colors_and_symbols_xml(path)
    assert 'code="203.9"' not in symbols
    assert 'code="203.1"' in symbols  # OCAD: Jeskyně
    assert "from ISSOM" not in symbols
    assert "is_hidden" not in symbols


def test_mtbo_set_has_own_ocad_symbols_instead_of_isom_overlay():
    """ISMTBOM 2022 má zemní sráz, kopeček, pole i hranici vegetace vlastní (bez ISOM overlay)."""
    import re

    path = symbol_set_path("mtbo_10000", 10000)
    _, symbols = colors_and_symbols_xml(path)

    def tag(code: str) -> str:
        m = re.search(rf'<symbol\b[^>]*\bcode="{re.escape(code)}"[^>]*>', symbols)
        assert m is not None, code
        return m.group(0)

    assert 'type="2"' in tag("104.0")  # zemní sráz = čára
    assert 'type="1"' in tag("109.0")  # kupka = bod
    for code in ("412.0", "416.1", "521.0"):
        tag(code)  # existují (hidden se odkryjí až při použití)


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
    """ISSprOM: hnědé barvy ulic 501.x musí být nad žlutou 401, jinak cesty zmizí.

    Nová OCAD sada má vlastní názvy barev (bez OOM „Lower brown …“).
    """
    import re

    text = symbol_set_path("sprint_2m", 4000).read_text(encoding="utf-8")
    by_name: dict[str, int] = {}
    by_pri: dict[int, str] = {}
    for m in re.finditer(
        r'<color\b[^>]*\bpriority="(\d+)"[^>]*\bname="([^"]+)"', text
    ):
        pri = int(m.group(1))
        by_name[m.group(2)] = pri
        by_pri[pri] = m.group(2)
    yellow = by_name["Yellow"]
    # Nižší priority = výše ve výkresu.
    assert by_name["Brown 50% - street infill"] < yellow
    assert by_name["Brown 30% - street infill"] < yellow
    assert by_name["Black - street sideline"] < yellow
    # OCAD heavy streets (501.30+) – brown 50% / street infill nad žlutou.
    road = re.search(
        r'<symbol[^>]*\bcode="501\.30"[^>]*>.*?</symbol>\s*(?=<symbol)', text, re.DOTALL
    )
    assert road is not None
    road_colors = {int(c) for c in re.findall(r'\bcolor="(\d+)"', road.group(0))}
    assert road_colors - {-1}
    for pri in road_colors:
        if pri < 0:
            continue
        assert pri < yellow, f"color {pri} ({by_pri.get(pri)}) must be above yellow"


def test_symbol_set_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.oom_symbols.OOM_DIR",
        tmp_path / "missing",
    )
    with pytest.raises(FileNotFoundError):
        symbol_set_path("sprint_2m", 4000)
