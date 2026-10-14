"""Doplnění symbolů z novější OCAD sady do OOM sad (symbol_merge)."""

import re
import xml.etree.ElementTree as ET

import pytest

from app.pipeline.oom_symbols import OOM_DIR, symbol_set_path
from app.pipeline.symbol_merge import merge_missing_symbols, normalize_code

BASE = """<map><colors count="2">
<color priority="0" name="Black" c="0" m="0" y="0" k="1" opacity="1"><cmyk method="custom"/><rgb method="cmyk" r="0" g="0" b="0"/></color>
<color priority="1" name="Green" c="0.76" m="0" y="0.91" k="0" opacity="1"><cmyk method="custom"/><rgb method="cmyk" r="0.24" g="1" b="0.09"/></color>
</colors>
<symbols count="2">
<symbol type="4" id="0" code="401" name="Open land"><area_symbol inner_color="1" min_area="0" patterns="0"/></symbol>
<symbol type="2" id="1" code="513" name="Wall"><line_symbol color="0" line_width="100"/></symbol>
</symbols></map>"""

DONOR = """<map><colors count="3">
<color priority="0" name="Black" c="0" m="0" y="0" k="1" opacity="1"><cmyk method="custom"/><rgb method="cmyk" r="0" g="0" b="0"/></color>
<color priority="1" name="Green 100" c="0.76" m="0" y="0.91" k="0" opacity="1"><cmyk method="custom"/><rgb method="cmyk" r="0.24" g="1" b="0.09"/></color>
<color priority="2" name="Yellow 75%" c="0" m="0.1" y="0.75" k="0" opacity="1"><cmyk method="custom"/><rgb method="cmyk" r="1" g="0.9" b="0.25"/></color>
</colors>
<symbols count="5">
<symbol type="4" id="10" code="401.0" name="Open land"><area_symbol inner_color="2" min_area="0" patterns="0"/></symbol>
<symbol type="2" id="11" code="513.100" name="Retaining wall"><line_symbol color="0" line_width="100"/></symbol>
<symbol type="4" id="12" code="512.300" name="Lower level"><area_symbol inner_color="2" min_area="0" patterns="1"><pattern type="1" color="1" line_width="300"/></area_symbol></symbol>
<symbol type="8" id="13" code="900.0" name="Text"><text_symbol/></symbol>
<symbol type="16" id="14" code="512.301" name="Combined"><combined_symbol parts="1"><part symbol="12"/></combined_symbol></symbol>
</symbols></map>"""


def test_normalize_code():
    assert normalize_code("401.0") == "401"
    assert normalize_code("201.2") == "201.2"
    assert normalize_code("512.300") == "512.300"


def test_original_kept_new_added():
    merged, rep = merge_missing_symbols(BASE, DONOR)
    codes = re.findall(r'<symbol[^>]*\bcode="([^"]+)"', merged)
    assert codes.count("401") == 1 and "401.0" not in codes  # původní zůstal
    assert "512.300" in codes and "512.301" in codes and "513.100" in codes
    assert "900.0" not in codes  # text se nepřidává
    assert [c for c, _ in rep.added] == ["513.100", "512.300", "512.301"]
    ET.fromstring(merged)  # platné XML
    ids = re.findall(r'<symbol\b[^>]*\bid="(\d+)"', merged)
    assert len(ids) == len(set(ids))
    assert '<symbols count="5">' in merged


def test_twin_in_same_series_is_skipped_by_name():
    base = BASE.replace(
        "</symbols>",
        '<symbol type="2" id="2" code="532.8" name="Stairway, footprint: 3 m"><line_symbol color="0" line_width="750"/></symbol></symbols>',
    ).replace('<symbols count="2">', '<symbols count="3">')
    donor = DONOR.replace(
        "</symbols>",
        '<symbol type="16" id="20" code="532.1" name="Stairway, 3 m - dvojitá linie"><line_symbol color="0" line_width="750"/></symbol></symbols>',
    ).replace('<symbols count="5">', '<symbols count="6">')
    merged, rep = merge_missing_symbols(base, donor)
    assert "532.1" not in re.findall(r'code="([^"]+)"', merged)
    assert ("532.1", "Stairway, 3 m - dvojitá linie", "532.8") in rep.twins


def test_manual_equivalent_skips_symbol():
    merged, rep = merge_missing_symbols(BASE, DONOR, {"512.300": "401"})
    assert "512.300" not in re.findall(r'code="([^"]+)"', merged)


def test_colors_remapped_and_added():
    merged, rep = merge_missing_symbols(BASE, DONOR)
    # Green 100 (donor 1) → Green (base 1); Yellow 75 % je nová barva 2
    assert rep.color_map[1] == 1
    assert rep.color_map[2] == 2
    assert '<colors count="3">' in merged
    lower = re.search(r'code="512.300".*?</symbol>', merged, re.S).group(0)
    assert 'inner_color="2"' in lower and 'pattern type="1" color="1"' in lower


def test_part_symbol_reference_follows_new_id():
    merged, _ = merge_missing_symbols(BASE, DONOR)
    new_id = re.search(r'<symbol[^>]*\bid="(\d+)"[^>]*code="512.300"', merged).group(1)
    assert f'part symbol="{new_id}"' in merged


def test_merged_symbol_ids_are_sequential_from_zero():
    """Po zařazení musí id odpovídat pořadí <symbol> od 0 (Mapper)."""
    merged, _ = merge_missing_symbols(BASE, DONOR)
    ids = [int(x) for x in re.findall(r'<symbol\b[^>]*\bid="(\d+)"', merged)]
    assert ids == list(range(len(ids)))
    # 512.* za 401, 513.100 za 513; combined part → 512.300.
    codes = re.findall(r'<symbol\b[^>]*\bcode="([^"]+)"', merged)
    assert codes == ["401", "512.300", "512.301", "513", "513.100"]
    lower_id = re.search(r'<symbol[^>]*\bid="(\d+)"[^>]*code="512.300"', merged).group(1)
    assert f'part symbol="{lower_id}"' in merged


@pytest.mark.parametrize(
    "preset,scale,has_new",
    [("forest_10000", 10000, "512.300"), ("sprint_4000", 4000, "512.300")],
)
def test_real_merged_sets_valid(preset, scale, has_new):
    path = symbol_set_path(preset, scale)
    text = path.read_text(encoding="utf-8")
    ET.fromstring(text)
    ids = [int(x) for x in re.findall(r'<symbol\b[^>]*\bid="(\d+)"', text)]
    # id jsou jedinečná jen mezi top-level symboly; soukromé části id nemají
    assert len(ids) == len(set(ids))
    assert ids == list(range(len(ids)))
    if path.parent.name == "merged":
        if preset.startswith("sprint"):
            assert f'code="{has_new}"' in text
        for code in ("401", "214", "201", "412"):
            assert f'code="{code}"' in text  # původní kódy zůstaly
    assert path.parent in (OOM_DIR, OOM_DIR / "merged")
