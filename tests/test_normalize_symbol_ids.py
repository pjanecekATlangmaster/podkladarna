"""Normalizace ID symbolů při skládání .omap (Mapper vyžaduje 0..n-1)."""

from __future__ import annotations

import re

from app.pipeline.build_oom_map import _normalize_symbol_ids, build_oom_map_xml
from app.pipeline.oom_import import OomObjectPart


def test_normalize_symbol_ids_rewrites_single_space_attributes():
    """Běžné OOM tagy mají jednu mezeru před id=/symbol= — regex to musí chytit."""
    symbols = (
        '<symbols count="3">\n'
        '<symbol type="1" id="0" code="A"></symbol>\n'
        '<symbol type="16" id="99" code="B">'
        '<combined_symbol parts="1"><part symbol="0"/></combined_symbol>'
        "</symbol>\n"
        '<symbol type="1" id="2" code="C"></symbol>\n'
        "</symbols>"
    )
    parts = (
        '<objects count="3">\n'
        '<object type="1" symbol="99"/>\n'
        '<object type="1" symbol="2"/>\n'
        '<object type="1" symbol="-1"/>\n'
        "</objects>"
    )
    out_sym, out_parts = _normalize_symbol_ids(symbols, parts)
    ids = [int(x) for x in re.findall(r'<symbol\b[^>]*\bid="(\d+)"', out_sym)]
    assert ids == [0, 1, 2]
    assert 'part symbol="0"' in out_sym
    assert re.findall(r'<object\b[^>]*\bsymbol="(-?\d+)"', out_parts) == [
        "1",
        "2",
        "-1",
    ]


def test_build_oom_map_xml_symbol_ids_are_sequential():
    for preset, scale in (
        ("sprint_2m", 4000),
        ("forest_10000", 10000),
        ("mtbo_10000", 10000),
    ):
        xml = build_oom_map_xml(
            map_name="t",
            scale=scale,
            ref_x=0,
            ref_y=0,
            ref_lat=50,
            ref_lon=14,
            declination=0,
            grivation=0,
            templates=[],
            preset_id=preset,
            object_parts=[
                OomObjectPart(
                    name="x",
                    objects_xml='<object type="1" symbol="0"/>',
                    count=1,
                )
            ],
        )
        body = re.search(r"<symbols\b[^>]*>(.*?)</symbols>", xml, re.S).group(1)
        ids = [int(x) for x in re.findall(r'<symbol\b[^>]*\bid="(\d+)"', body)]
        assert ids == list(range(len(ids))), preset
        prefs = re.findall(r'part symbol="(\d+)"', xml)
        assert all(int(p) < len(ids) for p in prefs), preset
