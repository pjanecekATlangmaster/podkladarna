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


def test_mtbo_map_drops_unused_hidden_symbols_and_renumbers_ids():
    raw = symbol_set_path("mtbo_10000", 10000).read_text(encoding="utf-8")
    hidden = hidden_symbol_ids(raw)
    assert hidden
    hidden_codes = {c for c in hidden.values() if c}
    xml = _build("mtbo_10000", 10000)
    present_codes = set(re.findall(r'<symbol\b[^>]*\bcode="([^"]+)"', xml))
    # Zhasnuté a nepoužité pryč (kromě záměrně odkrytých z ISOM overlay).
    gone_codes = hidden_codes - present_codes
    assert len(gone_codes) > 30
    assert "101" in present_codes
    assert not re.findall(r'is_hidden="true"', xml)
    # Po dropu se ID znovu seřadí od 0 (Mapper).
    ids = [int(x) for x in re.findall(r'<symbol\b[^>]*\bid="(\d+)"', xml)]
    assert ids == list(range(len(ids)))


def test_forest_map_drops_isom2000_legacy_keeps_rest():
    raw = symbol_set_path("forest_10000", 10000).read_text(encoding="utf-8")
    n_raw = len(re.findall(r'<symbol\b[^>]*\bid="\d+"', raw))
    xml = _build("forest_10000", 10000)
    n_out = len(re.findall(r'<symbol\b[^>]*\bid="\d+"', xml))
    # ISOM2000 / starší migrace pryč (104.9, 201.2, 203.9, …), zbytek zůstává.
    assert n_out < n_raw
    assert n_raw - n_out >= 5
    assert 'code="203.9"' not in xml
    assert 'code="201.2"' not in xml
    assert 'code="101"' in xml


def test_sprint_map_uses_ocad_501_scheme():
    """Sprint: OCAD 501 (light/heavy), bez OOM footprintů 501.6–19 a ISSOM legacy."""
    raw = symbol_set_path("sprint_2m", 4000).read_text(encoding="utf-8")
    assert 'code="203.9"' in raw or 'code="501.0"' in raw
    xml = _build("sprint_2m", 4000)
    for code in ("201.8", "201.9", "202.8", "202.9", "203.9"):
        assert f'code="{code}"' not in xml
    # Staré OOM heavy/light footprinty pryč (nahrazeny OCAD 501.10–13 / 501.30–33).
    for code in (
        "501.6",
        "501.7",
        "501.8",
        "501.9",
        "501.16",
        "501.17",
        "501.18",
        "501.19",
        "532.6",
        "532.8",
    ):
        assert f'code="{code}"' not in xml
    assert 'code="203"' in xml
    assert 'code="203.1"' in xml
    assert 'code="501.0"' in xml
    assert 'code="501.10"' in xml  # chodník
    assert 'code="501.13"' in xml  # light 4 m
    assert 'code="501.30"' in xml
    assert 'code="501.31"' in xml  # dřívější 501.19
    assert 'code="501.100"' in xml  # hrana (dřív OOM 501.1)
    assert 'code="532.7"' in xml
    ids = [int(x) for x in re.findall(r'<symbol\b[^>]*\bid="(\d+)"', xml)]
    assert ids == list(range(len(ids)))
