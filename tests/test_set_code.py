"""Testy překladu kódů generátoru na kódy sady (set_code)."""

from __future__ import annotations

from pathlib import Path

import pytest

import app.pipeline.oom_symbol_map as sm
import app.pipeline.symbol_catalog as sc

OOM = Path(__file__).resolve().parent.parent / "configs" / "oom"

_SYNTH = """\
isom:
  - ocad_code: "101.0"
    cz_name: "Základní vrstevnice"
    generator_codes: ["101"]
    geom: line
    review: false
  - ocad_code: "203.100"
    cz_name: "Kamenná jáma"
    generator_codes: ["203.1", "203.9"]
    geom: point
    review: true
  - ocad_code: "206.0"
    cz_name: "Obrovský balvan"
    generator_codes: ["206"]
    geom: area
    review: false
  - ocad_code: "201.2"
    cz_name: "Nepřekonatelný sráz bez čárek"
    generator_codes: ["201.2"]
    geom: line
    review: true
sprint:
  - ocad_code: "501.0"
    cz_name: "Zpevněná plocha"
    generator_codes: ["501"]
    geom: area
    review: false
"""


@pytest.fixture()
def synth_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "symbol_catalog.yaml"
    p.write_text(_SYNTH, encoding="utf-8")
    monkeypatch.setattr(sc, "CATALOG_PATH", p)
    sm.drain_unmapped_codes()
    yield p
    sm.drain_unmapped_codes()


def _use_set(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(sm, "symbol_set_path", lambda preset_id, scale: OOM / name)


def test_uses_catalog_by_filename() -> None:
    assert sc.uses_catalog(Path("ISOM_2017_10000_cz.omap"))
    assert sc.uses_catalog(Path("ISSprOM_2019_4000_cz.omap"))
    assert sc.uses_catalog(Path("ISMTBOM_2022_10000_cz.omap"))
    assert not sc.uses_catalog(Path("ISOM_2017-2_10000.omap"))
    assert not sc.uses_catalog(Path("ISSprOM_2019_4000.omap"))
    assert not sc.uses_catalog(Path("ISMTBOM_10000.omap"))


def test_translate_synthetic(synth_catalog, monkeypatch) -> None:
    _use_set(monkeypatch, "ISOM_2017_10000_cz.omap")
    assert sm.set_code("forest_10000", 10000, "101") == "101.0"
    assert sm.set_code("forest_10000", 10000, "203.1") == "203.100"
    assert sm.set_code("forest_10000", 10000, "203.9") == "203.100"  # víc kódů → jeden symbol


def test_unknown_code_is_none_and_no_implicit_dot_zero(synth_catalog, monkeypatch) -> None:
    _use_set(monkeypatch, "ISOM_2017_10000_cz.omap")
    assert sm.set_code("forest_10000", 10000, "999") is None
    # žádné implicitní „+.0“ ani fallback na syrový kód
    assert sm.set_code("forest_10000", 10000, "206.0") is None
    assert sm.set_code("forest_10000", 10000, "203") is None
    assert sm.set_code("forest_10000", 10000, "101.0") is None


def test_sprint_uses_own_catalog_section(synth_catalog, monkeypatch) -> None:
    _use_set(monkeypatch, "ISSprOM_2019_4000_cz.omap")
    assert sm.set_code("sprint_2m", 4000, "501") == "501.0"
    assert sm.set_code("sprint_2m", 4000, "101") is None  # 101 je jen v sekci isom


@pytest.mark.parametrize(
    "old_set",
    [
        "ISOM_2017-2_10000.omap",
        "ISOM_2017-2_15000.omap",
        "ISSprOM_2019_4000.omap",
        "ISMTBOM_10000.omap",
        "ISMTBOM_15000.omap",
    ],
)
def test_old_sets_are_identity(synth_catalog, monkeypatch, old_set: str) -> None:
    _use_set(monkeypatch, old_set)
    for code in ("101", "501.0", "203.2", "neexistuje", "999"):
        assert sm.set_code("forest_10000", 10000, code) == code
    assert sm.drain_unmapped_codes() == {}


def test_mtbo_uses_catalog_section() -> None:
    """MTBO jede na ISMTBOM 2022 *_cz.omap přes sekci ``mtbo`` katalogu."""
    sm.drain_unmapped_codes()
    for preset, scale in (("mtbo_10000", 10000), ("mtbo_15000", 15000)):
        assert sm.set_code(preset, scale, "101") == "101.0"
        assert sm.set_code(preset, scale, "834") == "818.0"
        assert sm.set_code(preset, scale, "101.0") is None  # žádné implicitní „.0“
    sm.drain_unmapped_codes()


def test_forest_and_sprint_use_catalog_by_default() -> None:
    sm.drain_unmapped_codes()
    for preset, scale in (("sprint_2m", 4000), ("forest_10000", 10000), ("forest_10000", 15000)):
        assert sm.set_code(preset, scale, "101") == "101.0"
        assert sm.set_code(preset, scale, "101.0") is None
    sm.drain_unmapped_codes()


def test_unmapped_codes_are_counted(synth_catalog, monkeypatch) -> None:
    _use_set(monkeypatch, "ISOM_2017_10000_cz.omap")
    sm.set_code("forest_10000", 10000, "777")
    sm.set_code("forest_10000", 10000, "777")
    sm.set_code("forest_10000", 10000, "101")
    drained = sm.drain_unmapped_codes()
    assert drained == {("ISOM_2017_10000_cz.omap", "777"): 2}
    assert sm.drain_unmapped_codes() == {}
    msg = sm.unmapped_codes_message({("ISOM_2017_10000_cz.omap", "777"): 2})
    assert msg and "777×2" in msg
    assert sm.unmapped_codes_message({}) is None


def test_rock_area_skips_line_symbol_in_catalog_set(synth_catalog, monkeypatch) -> None:
    """V OCAD sadě je 201.2 čára → plocha skály se přeskočí na 206."""
    _use_set(monkeypatch, "ISOM_2017_10000_cz.omap")
    monkeypatch.setattr(sm, "_code_to_index", lambda path: {"201.2": 5, "206.0": 6})
    monkeypatch.setattr(sm, "_symbol_visible", lambda preset_id, scale, code: True)
    assert sm.resolve_rock_area_code("forest_10000", 10000) == "206"


def test_has_symbol_does_not_count_unmapped(synth_catalog, monkeypatch) -> None:
    _use_set(monkeypatch, "ISOM_2017_10000_cz.omap")
    monkeypatch.setattr(sm, "_code_to_index", lambda path: {"101.0": 1})
    assert sm._has_symbol("forest_10000", 10000, "101")
    assert not sm._has_symbol("forest_10000", 10000, "555")
    assert sm.drain_unmapped_codes() == {}


# --- reálný katalog ----------------------------------------------------------


def test_real_catalog_isom(monkeypatch) -> None:
    _use_set(monkeypatch, "ISOM_2017_10000_cz.omap")
    sm.drain_unmapped_codes()
    assert sm.set_code("forest_10000", 10000, "101") == "101.0"
    assert sm.set_code("forest_10000", 10000, "206") == "206.0"
    assert sm.set_code("forest_10000", 10000, "neexistuje") is None
    # +.0 se nikdy nepoužívá
    assert sm.set_code("forest_10000", 10000, "101.0") is None
    sm.drain_unmapped_codes()


def test_real_catalog_sprint_and_symbol_index(monkeypatch) -> None:
    _use_set(monkeypatch, "ISSprOM_2019_4000_cz.omap")
    sm.drain_unmapped_codes()
    path = OOM / "ISSprOM_2019_4000_cz.omap"
    index = sm._code_to_index(path)
    assert sm.set_code("sprint_2m", 4000, "101") == "101.0"
    assert sm.symbol_index_for_code("sprint_2m", 4000, "101") == index["101.0"]
    assert sm.symbol_index_for_code("sprint_2m", 4000, "101.0") is None
    sm.drain_unmapped_codes()


def test_real_catalog_rock_area_is_206(monkeypatch) -> None:
    _use_set(monkeypatch, "ISOM_2017_10000_cz.omap")
    sm.drain_unmapped_codes()
    assert sm.resolve_rock_area_code("forest_10000", 10000) == "206"
    sm.drain_unmapped_codes()


def test_protected_codes_translated_for_catalog_sets() -> None:
    from app.pipeline.oom_symbols import colors_and_symbols_xml

    _colors, symbols = colors_and_symbols_xml(OOM / "ISOM_2017_10000_cz.omap")
    import re

    protected = re.findall(r'<symbol\b[^>]*\bis_protected="true"[^>]*\bcode="([^"]+)"', symbols)
    protected += re.findall(r'<symbol\b[^>]*\bcode="([^"]+)"[^>]*\bis_protected="true"', symbols)
    assert "101.0" in protected and "102.0" in protected
    assert "101" not in protected


def test_rock_area_sprint_is_206() -> None:
    sm.drain_unmapped_codes()
    assert sm.resolve_rock_area_code("sprint_2m", 4000) == "206"
    # sondování kandidátů nesmí generovat falešná varování o chybějících symbolech
    assert sm.drain_unmapped_codes() == {}


def test_describe_code_for_static_texts() -> None:
    assert sm.describe_code("214") == "214.0"
    assert sm.describe_code("501") == "501.0"
    assert sm.describe_code("505.1") == "505.0"  # jen sprint
    assert sm.describe_code("neexistuje") == "neexistuje"
    assert sm.describe_code("513.2") == "513.200"  # v obou sadách stejný kód
    assert sm.describe_code("502") == "502.2"  # jen ISOM


def test_readme_uses_set_codes() -> None:
    from app.pipeline.package_oom import OSM_FOLDER_README, oom_readme

    text = oom_readme({"preset_id": "forest_10000", "scale": 10000})
    assert "Vrstevnice (101.0/102.0)" in text
    assert "skála (206.0)" in text
    assert "holá skála 214.0" in OSM_FOLDER_README


def test_rock_area_forest_is_206_without_false_warning() -> None:
    """Kandidát 201.2 se zkouší bez zápisu do počítadla (jinak falešné „!!!“ v každém jobu)."""
    sm.drain_unmapped_codes()
    assert sm.resolve_rock_area_code("forest_10000", 10000) == "206"
    assert sm.resolve_rock_area_code("forest_10000", 15000) == "206"
    assert sm.drain_unmapped_codes() == {}
    assert sm.symbol_index_for_code("forest_10000", 10000, "206") is not None


def test_prohibited_area_520_is_fill_only() -> None:
    """Zakázaná plocha: jen výplň bez černého obrysu (ISOM 520.4, sprint 520.1)."""
    from app.pipeline.symbol_catalog import generator_map

    assert generator_map("isom")["520"] == "520.4"
    assert generator_map("sprint")["520"] == "520.1"


def test_translation_cache_keeps_unmapped_counter(monkeypatch) -> None:
    """Překlad je cachovaný, ale každý dotaz na neznámý kód se musí dál počítat."""
    _use_set(monkeypatch, "ISOM_2017_10000_cz.omap")
    sm.drain_unmapped_codes()
    for _ in range(3):
        assert sm.symbol_index_for_code("forest_10000", 10000, "neznamy") is None
    assert sm.drain_unmapped_codes() == {("ISOM_2017_10000_cz.omap", "neznamy"): 3}
