"""Čistá verze mapy les a MTBO: zeleň a drobný otevřený terén z výšek pryč, data zůstávají."""

from __future__ import annotations

import pytest

from app.pipeline.clean_map import clean_object_parts, full_variant_name, strip_full_tag
from app.pipeline.oom_import import OomObjectPart


def _square(x_m: float, y_m: float, side_m: float, scale: int = 10000, symbol: int = 1) -> str:
    k = 1e6 / scale  # µm mapy na metr
    x0, y0, x1, y1 = (int(v * k) for v in (x_m, y_m, x_m + side_m, y_m + side_m))
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    body = ";".join(f"{x} {y}" for x, y in pts) + f";{x0} {y0} 18;"
    return f'<object type="1" symbol="{symbol}"><coords count="5">{body}</coords></object>'


def _part(name: str, objs: list[str]) -> OomObjectPart:
    return OomObjectPart(name=name, objects_xml="\n".join(objs), count=len(objs))


@pytest.mark.parametrize(
    "objs, kept",
    [
        ([_square(0, 0, 40)], 0),  # osamělá louka 0,16 ha
        ([_square(0, 0, 60)], 1),  # paseka 0,36 ha
        ([_square(0, 0, 40), _square(40, 0, 40)], 2),  # kusy téže plochy (řez objektu) dohromady 0,32 ha
    ],
)
def test_open_terrain_kept_only_as_big_area(objs, kept):
    stats: dict = {}
    out = clean_object_parts([_part("Otevřený terén", objs)], scale=10000, stats=stats)
    assert sum(p.count for p in out) == kept
    assert stats["open_kept"] == kept and stats["open_dropped"] == len(objs) - kept


def test_open_and_rough_open_count_together():
    parts = [_part("Otevřený terén", [_square(0, 0, 40)]), _part("Drsný otevřený terén", [_square(40, 0, 40)])]
    out = clean_object_parts(parts, scale=10000)
    assert [p.name for p in out] == ["Otevřený terén", "Drsný otevřený terén"]


def test_green_and_dem_dropped_data_kept():
    parts = [
        _part("Vegetace chůze", [_square(0, 0, 100)]),
        _part("Vegetace boj", [_square(0, 0, 100)]),
        _part("DEM kandidáti – vektory (srázy 3)", ["<object type=\"0\" symbol=\"2\"/>"]),
        _part("OSM – křoví", [_square(0, 0, 10)]),
        _part("ZABAGED – TrvalyTravniPorost", [_square(0, 0, 10)]),
    ]
    out = clean_object_parts(parts, scale=10000)
    assert [p.name for p in out] == ["OSM – křoví", "ZABAGED – TrvalyTravniPorost"]
    assert parts[0].count == 1  # vstup (plná mapa) beze změny
    kept_dem = clean_object_parts(parts, scale=10000, drop_dem=False)
    assert any(p.name.startswith("DEM kandidáti") for p in kept_dem)


def test_file_names():
    assert full_variant_name("Mapa-les.omap") == "Mapa-les_full.omap"
    assert strip_full_tag("Mapa-mtbo_full") == "Mapa-mtbo"
