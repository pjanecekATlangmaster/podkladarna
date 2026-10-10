import re

from app.pipeline.build_oom_map import build_oom_map_xml
from app.pipeline.oom_symbols import (
    drop_symbols_by_id,
    hidden_symbol_ids,
    symbol_set_path,
)


def _build(preset: str, scale: int, parts=None) -> str:
    return build_oom_map_xml(
        map_name="t", scale=scale, ref_x=0, ref_y=0, ref_lat=50, ref_lon=14,
        declination=0, grivation=0, templates=[], object_parts=parts,
        preset_id=preset,
    )


def test_drop_symbols_by_id_handles_nested_and_count():
    xml = (
        '<symbols count="3">\n'
        '<symbol type="1" id="0" code="1"><a/></symbol>\n'
        '<symbol type="4" id="1" code="2" is_hidden="true">'
        '<symbol type="1" code="x"></symbol></symbol>\n'
        '<symbol type="1" id="2" code="3"></symbol>\n</symbols>'
    )
    assert hidden_symbol_ids(xml) == {"1": "2"}
    out = drop_symbols_by_id(xml, {"1"})
    assert 'count="2"' in out and 'code="2"' not in out
    assert 'code="1"' in out and 'code="3"' in out


def test_mtbo_map_drops_unused_hidden_symbols_but_keeps_ids():
    raw = symbol_set_path("mtbo_10000", 10000).read_text(encoding="utf-8")
    hidden = hidden_symbol_ids(raw)
    assert hidden
    xml = _build("mtbo_10000", 10000)
    present = set(re.findall(r'<symbol\b[^>]*\bid="(\d+)"', xml))
    # Zhasnuté a nepoužité pryč (kromě záměrně odkrytých z ISOM overlay).
    gone = {i for i in hidden if i not in present}
    assert len(gone) > 30
    # Viditelné symboly a jejich id zůstávají beze změny.
    assert "101" in re.findall(r'\bcode="([^"]+)"', xml)
    assert not re.findall(r'is_hidden="true"', xml)


def test_non_mtbo_map_keeps_all_symbols():
    raw = symbol_set_path("forest_10000", 10000).read_text(encoding="utf-8")
    n_raw = len(re.findall(r'<symbol\b[^>]*\bid="\d+"', raw))
    xml = _build("forest_10000", 10000)
    assert len(re.findall(r'<symbol\b[^>]*\bid="\d+"', xml)) == n_raw
