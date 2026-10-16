"""ISOM (les): zpevněná plocha 501.1 je jen výplň → okraj 501.2 jako čára po každém prstenci."""

from __future__ import annotations

import json
import re
from collections import Counter

import pytest

import app.pipeline.oom_symbol_map as sm
from app.pipeline.osm_paths import build_osm_feature_parts

OUTER = [[0.0, 0.0], [60.0, 0.0], [60.0, 60.0], [0.0, 60.0], [0.0, 0.0]]
HOLE = [[20.0, 20.0], [40.0, 20.0], [40.0, 40.0], [20.0, 40.0], [20.0, 20.0]]


def _build(tmp_path, preset: str, scale: int, kind: str = "pitch", clip_bounds=None):
    out = tmp_path / "osm_paths"
    out.mkdir(exist_ok=True)
    (out / "features.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"kind": kind},
                        "geometry": {"type": "Polygon", "coordinates": [OUTER, HOLE]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    parts = build_osm_feature_parts(
        tmp_path, preset_id=preset, scale=scale, ref_x=0.0, ref_y=0.0, grivation_deg=0.0,
        clip_bounds=clip_bounds,
    )
    path = sm.symbol_set_path(preset, scale)
    id2code = {v: k for k, v in sm._code_to_index(path).items()}
    cnt: Counter[str] = Counter()
    for p in parts:
        for m in re.finditer(r'<object\b[^>]*\bsymbol="(\d+)"', p.objects_xml):
            cnt[id2code[int(m.group(1))]] += 1
    return parts, cnt


@pytest.mark.parametrize("scale", [10000, 15000])
def test_forest_paved_area_gets_edge_line_per_ring(tmp_path, scale) -> None:
    sm.drain_unmapped_codes()
    parts, cnt = _build(tmp_path, "forest_10000", scale)
    assert cnt["501.1"] == 1  # výplň
    assert cnt["501.2"] == 2  # vnější obrys + díra
    assert sum(p.count for p in parts) == 3
    assert sm.drain_unmapped_codes() == {}
    # uzavřená cesta: poslední souřadnice nese flag 18 (ClosePoint)
    xml = "\n".join(p.objects_xml for p in parts)
    edge_id = sm._code_to_index(sm.symbol_set_path("forest_10000", scale))["501.2"]
    edge_objs = re.findall(rf'<object\b[^>]*\bsymbol="{edge_id}".*?</object>', xml, re.DOTALL)
    assert len(edge_objs) == 2
    assert all(" 18;" in o for o in edge_objs)


def test_forest_edge_not_drawn_along_clip_boundary(tmp_path) -> None:
    """Plocha oříznutá výřezem: okraj jen po skutečné hranici, ne po hraně výřezu."""
    parts, cnt = _build(tmp_path, "forest_10000", 10000, clip_bounds=(-100.0, -100.0, 30.0, 100.0))
    assert cnt["501.1"] == 1
    assert cnt["501.2"] >= 2  # vnější obrys i díra rozřezané na otevřené kusy
    xml = "\n".join(p.objects_xml for p in parts)
    edge_id = sm._code_to_index(sm.symbol_set_path("forest_10000", 10000))["501.2"]
    edge_objs = re.findall(rf'<object\b[^>]*\bsymbol="{edge_id}".*?</object>', xml, re.DOTALL)
    assert edge_objs and all(" 18;" not in o for o in edge_objs)  # žádná uzavřená čára


def test_forest_edge_closed_when_polygon_fully_inside_clip(tmp_path) -> None:
    parts, cnt = _build(tmp_path, "forest_10000", 10000, clip_bounds=(-100.0, -100.0, 200.0, 200.0))
    assert cnt["501.2"] == 2
    assert sum(p.objects_xml.count(" 18;") for p in parts) >= 4  # výplň 2× + okraje 2×


def test_forest_parking_501_0_has_no_extra_edge(tmp_path) -> None:
    """501.0 má okraj v sobě – žádná duplicitní čára 501.2."""
    _parts, cnt = _build(tmp_path, "forest_10000", 10000, kind="parking")
    assert cnt["501.0"] == 1
    assert cnt["501.2"] == 0 and cnt["501.1"] == 0


def test_sprint_has_no_edge_line(tmp_path) -> None:
    """Sprint: 501.0 (okraj v symbolu); 501.2 je tam plocha dolní úrovně – nesmí se použít."""
    _parts, cnt = _build(tmp_path, "sprint_2m", 4000)
    assert cnt["501.0"] == 1
    assert sum(n for c, n in cnt.items() if c != "501.0") == 0


def test_mtbo_has_no_edge_line(tmp_path) -> None:
    parts, cnt = _build(tmp_path, "mtbo_10000", 10000)
    assert sum(p.count for p in parts) == 1  # jedna plocha 529, bez okraje


def test_edge_is_drawn_above_fill() -> None:
    """Nižší priorita barvy = výš ve výkresu: černý okraj 501.2 nad hnědou výplní 501.1."""
    text = sm.symbol_set_path("forest_10000", 10000).read_text(encoding="utf-8")
    prio = {int(m.group(1)): m.group(2) for m in re.finditer(r'<color priority="(\d+)" name="([^"]*)"', text)}

    def colors(code: str) -> set[int]:
        m = re.search(rf'<symbol\b[^>]*\bcode="{re.escape(code)}"[^>]*>.*?</symbol>', text, re.DOTALL)
        return {int(c) for c in re.findall(r'color="(\d+)"', m.group(0))} - {-1}

    edge, fill = colors("501.2"), colors("501.1")
    assert edge and fill
    assert max(edge) < min(fill), (
        {prio[c] for c in edge}, {prio[c] for c in fill}
    )
