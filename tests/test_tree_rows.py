"""Aleje: body 418 místo čáry 416 (3.28.0): detekce stromů z CHM, prořídnutí, podpora zelení / bílou, dedup, záloha bez CHM."""

from __future__ import annotations

import json
import math
import re

import numpy as np
import pytest
from shapely.geometry import LineString, Point, box

from app.pipeline import tree_rows as tr
from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import MAP_COORD_CLOSE_POINT, OomObjectPart, _point_object
from app.pipeline.oom_symbol_map import symbol_index_for_code
from app.pipeline.osm_paths import classify_osm_feature
from app.pipeline.path_clearance import _format_coords

SIZE = 100
CHM_GT = (0.0, 1.0, 0.0, float(SIZE), 0.0, -1.0)  # 1 m pixel, sever nahoře
LINE = [(10.0, 50.0), (90.0, 50.0)]
PRESET = "forest_10000"


def _row_chm(offset=2.0, spacing=7.0, height=6.0, xs=None):
    chm = np.zeros((SIZE, SIZE), dtype=np.float32)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    centers = [(x, 50.0 + offset) for x in (xs if xs is not None else np.arange(14.0, 88.0, spacing))]
    for cx, cy in centers:
        d2 = (xx + 0.5 - cx) ** 2 + (SIZE - (yy + 0.5) - cy) ** 2
        chm = np.maximum(chm, (height * np.exp(-d2 / (2 * 1.5**2))).astype(np.float32))
    return chm, centers


@pytest.mark.parametrize(
    ("offset", "spacing", "height"),
    [(2.0, 7.0, 6.0), (-2.0, 7.0, 6.0), (0.0, 9.0, 8.0), (3.5, 6.0, 5.0)],
)
def test_detect_row_trees_finds_the_row(offset, spacing, height):
    chm, centers = _row_chm(offset, spacing, height)
    pts = tr.detect_row_trees(LINE, chm, CHM_GT)
    assert len(pts) >= len(centers) - 1
    assert [p[0] for p in pts] == sorted(p[0] for p in pts)  # seřazeno podél linie
    for x, y in pts:
        assert min(math.hypot(x - cx, y - cy) for cx, cy in centers) <= 1.0


@pytest.mark.parametrize("scene", ["no_trees", "low", "far", "stray"])
def test_detect_row_trees_ignores_non_rows(scene):
    chm, _ = _row_chm()
    if scene == "no_trees":
        chm[:] = 0
    elif scene == "low":
        chm *= 0.3  # pod 3 m
    elif scene == "far":
        chm, _ = _row_chm(offset=12.0)  # dál než ROW_BAND_M
    elif scene == "stray":
        chm[28:32, 40:44] = 8.0  # osamělý strom 20 m od linie
    pts = tr.detect_row_trees(LINE, chm, CHM_GT)
    if scene == "stray":
        assert all(abs(y - 50.0) < 5 for _x, y in pts)
    else:
        assert pts == []


@pytest.mark.parametrize(("scale", "want_all"), [(10000, False), (4000, True)])
def test_thin_points_min_center(scale, want_all):
    cand = [(10.0 + 7.0 * i, 50.0) for i in range(10)]
    kept = tr.thin_points(cand, [], tr.MIN_CENTER_MM * scale / 1000.0)
    assert (len(kept) == len(cand)) == want_all
    d = min(math.hypot(a[0] - b[0], a[1] - b[1]) for i, a in enumerate(kept) for b in kept[:i])
    assert d >= tr.MIN_CENTER_MM * scale / 1000.0 - 1e-9


@pytest.mark.parametrize(
    ("scene", "drawn"),
    [
        ("green", False),  # alej v zelené ploše 406–410
        ("yellow", True),  # v otevřené ploše 401 se zdůrazní
        ("white", False),  # bez plochy
        ("half_green", True),  # 50 % zelená, zbytek 401: pod 75 %
        ("road_over_open", True),  # cesta přes alej se nepočítá, zbytek je 401
        ("road_over_white", False),  # pás cesty se nepočítá, zbytek je bílý
    ],
)
def test_row_support(scene, drawn):
    samples = tr.sample_line(LINE)
    full = box(0, 40, 100, 60)
    left = box(0, 40, 50, 60)
    green = {"green": full, "half_green": left}.get(scene)
    other = {"yellow": full, "half_green": box(50, 40, 100, 60), "road_over_open": full}.get(scene)
    band = LineString([(50.0, 40.0), (50.0, 60.0)]).buffer(20.0) if scene.startswith("road") else None
    g, w = tr.row_support(samples, green, other, band)
    assert (g + w < tr.SUPPORT_SHARE) == drawn


# --- nad hotovou mapou ---------------------------------------------------------------------------------------------


def _area(code, rect):
    idx = symbol_index_for_code(PRESET, 10000, code)
    pts = [projected_to_map_coord(x, y, ref_x=0, ref_y=0, scale=10000, grivation_deg=0.0) for x, y in rect]
    pts = [[x, y, 0] for x, y in pts]
    pts[-1][2] = MAP_COORD_CLOSE_POINT
    return f'<object type="1" symbol="{idx}">\n{_format_coords(pts)}\n</object>'


def _run(tmp_path, under=None, existing=None, kind_geojson=True):
    (tmp_path / "osm_paths").mkdir()
    feat = {"type": "Feature", "properties": {"kind": "tree_row"}, "geometry": {"type": "LineString", "coordinates": [list(p) for p in [(0.0, 0.0), (100.0, 0.0)]]}}
    (tmp_path / "osm_paths" / "features.geojson").write_text(json.dumps({"features": [feat]}), encoding="utf-8")
    parts = []
    if under:
        code, rect = under
        parts.append(OomObjectPart(name="plocha", objects_xml=_area(code, rect), count=1))
    if existing:
        idx = symbol_index_for_code(PRESET, 10000, "417")
        mx, my = projected_to_map_coord(*existing, ref_x=0, ref_y=0, scale=10000, grivation_deg=0.0)
        parts.append(OomObjectPart(name="stromy", objects_xml=_point_object(idx, mx, my), count=1))
    logs = []
    out = tr.add_tree_row_parts(
        parts, tmp_path, preset_id=PRESET, scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0,
        clip_bounds=(-10.0, -50.0, 200.0, 50.0), log=logs.append,
    )
    rows = [p for p in out if p.name == tr.TREE_ROW_PART_NAME]
    return rows[0].count if rows else 0, logs


RECT = [(-5.0, -10.0), (105.0, -10.0), (105.0, 10.0), (-5.0, 10.0)]


@pytest.mark.parametrize(
    ("under", "existing", "want"),
    [
        (("401", RECT), None, "fallback"),  # bez CHM záloha v rozestupu 10 m (1 mm v 1:10 000)
        (("408", RECT), None, 0),  # zelená plocha pod alejí
        (None, None, 0),  # bílá
        (("401", RECT), (45.0, 0.0), "dedup"),  # už nakreslený strom 417 do 1 mm
    ],
)
def test_add_tree_row_parts(tmp_path, under, existing, want):
    n, logs = _run(tmp_path, under, existing)
    assert any(l.startswith("Aleje (418)") for l in logs)
    if want == "fallback":
        assert 8 <= n <= 11
    elif want == "dedup":
        full, _ = _run(tmp_path.parent / "x", under, None) if (tmp_path.parent / "x").mkdir() is None else (0, [])
        assert n == full - 1
    else:
        assert n == want


def test_classify_osm_tree_row():
    assert classify_osm_feature({"natural": "tree_row"}) == ("tree_row", "418")
    assert classify_osm_feature({"natural": "tree_row"}, geom="node") is None
