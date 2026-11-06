"""Herní prvky na hřištích ve sprintu: detekce z CHM a ortofota, generalizace, OSM přednost, záložní křížek."""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import box, mapping

from app.pipeline import playground_equipment as pe
from app.pipeline.residual_verify import Raster

GRASS = (70, 120, 50)
SAND = (200, 180, 150)
GREY = (120, 120, 125)


def _scene(size_m: float = 60.0, base=GRASS):
    """Ortofoto 0,5 m a CHM 1 m, levý horní roh (0, size)."""
    n = int(size_m / 0.5)
    rgb = np.zeros((n, n, 3), np.uint8)
    rgb[:] = base
    chm = np.zeros((int(size_m), int(size_m)), np.float32)
    return rgb, chm


def _paint(rgb, x0, y0, x1, y1, color, size_m=60.0):
    rgb[int((size_m - y1) / 0.5) : int((size_m - y0) / 0.5), int(x0 / 0.5) : int(x1 / 0.5)] = color


def _lift(chm, x0, y0, x1, y1, h, size_m=60.0):
    chm[int(size_m - y1) : int(size_m - y0), int(x0) : int(x1)] = h


def _rasters(rgb, chm, size_m=60.0):
    return Raster(rgb, 0.0, size_m, 0.5, 0.5), Raster(chm, 0.0, size_m, 1.0, 1.0)


def _feats(ground, *extra, name=""):
    props = {"source": "osm", "kind": "playground", "oom_code": "501"}
    if name:
        props["name"] = name
    return [{"type": "Feature", "properties": props, "geometry": mapping(ground)}, *extra]


def _osm_point(x, y, ptype):
    return {
        "type": "Feature",
        "properties": {"source": "osm", "kind": pe.KIND, "oom_code": "531", "playground": ptype},
        "geometry": {"type": "Point", "coordinates": [x, y]},
    }


def _crosses(out):
    return [
        (f["geometry"]["coordinates"], f["properties"]["source"])
        for f in out
        if f["properties"].get("kind") == pe.KIND
    ]


@pytest.mark.parametrize(
    "case, color, height, found",
    [
        ("konstrukce 2,5 m na štěrku", GREY, 2.5, True),
        ("lanová pyramida 6,5 m", GREY, 6.5, True),
        ("zelený keř 2 m", GRASS, 2.0, False),
        ("nízký prvek 1 m", GREY, 1.0, False),
    ],
)
def test_vyska_z_chm(case, color, height, found):
    rgb, chm = _scene()
    _paint(rgb, 28, 28, 30, 31, color)  # 2 × 3 m (plný objekt ≥ 8 m² by byl budka)
    _lift(chm, 28, 28, 30, 31, height)
    ortho, ch = _rasters(rgb, chm)
    cand = pe.detect_candidates(box(10, 10, 50, 50), ortho, ch)
    assert any(abs(x - 29.0) < 2 and abs(y - 29.5) < 2 for x, y, _s, _src in cand) is found, case


def test_okraj_koruny_a_budka_nejsou_prvek():
    rgb, chm = _scene()
    # strom: zelená koruna 8 m, kolem nezelený pás 2 m (stín / okraj z maxima DMP v 1 m)
    _paint(rgb, 20, 20, 26, 26, (40, 90, 30))
    _lift(chm, 20, 20, 26, 26, 8.0)
    _paint(rgb, 26, 20, 27, 26, GREY)
    _lift(chm, 26, 20, 27, 26, 2.0)
    # budka 4 × 4 m s plnou střechou 2,5 m
    _paint(rgb, 36, 36, 40, 40, (150, 60, 50))
    _lift(chm, 36, 36, 40, 40, 2.5)
    ortho, ch = _rasters(rgb, chm)
    assert pe.detect_candidates(box(10, 10, 50, 50), ortho, ch) == []


@pytest.mark.parametrize(
    "case, patch, item, found",
    [
        ("písková plocha s houpačkou", (24, 24, 32, 31), True, True),
        ("prázdná pravoúhlá skvrna = pískoviště", (24, 24, 28, 28), False, False),
    ],
)
def test_dopadova_plocha(case, patch, item, found):
    rgb, chm = _scene()
    _paint(rgb, *patch, SAND)
    if item:
        _paint(rgb, 27, 27, 28, 28.5, (60, 55, 50))  # stín konstrukce
    ortho, ch = _rasters(rgb, chm)
    cand = pe.detect_candidates(box(10, 10, 50, 50), ortho, ch)
    assert bool([c for c in cand if c[3] == "B"]) is found, case


@pytest.mark.parametrize(
    "case, size, n_items, expected",
    [
        ("malé hřiště 20 × 20 m = 1 křížek", 20.0, 4, 1),
        ("velké hřiště 50 × 50 m, prvky 6 m od sebe = odstup 16 m", 50.0, 8, 4),
    ],
)
def test_generalizace(case, size, n_items, expected):
    rgb, chm = _scene()
    x0 = 5.0
    for i in range(n_items):
        x = x0 + 2 + (i % 4) * 12
        y = x0 + 2 + (i // 4) * 16 if size > 30 else x0 + 2 + (i // 4) * 6
        x = x if size > 30 else x0 + 2 + (i % 4) * 4.5
        _paint(rgb, x, y, x + 2, y + 2, GREY)
        _lift(chm, x, y, x + 2, y + 2, 2.5)
    ortho, ch = _rasters(rgb, chm)
    out = pe.playground_points(_feats(box(x0, x0, x0 + size, x0 + size)), ortho, ch, 4000)
    pts = [p for p, src in _crosses(out) if src == "detect"]
    assert len(pts) == expected, case
    gap = pe.GAP_MM * 4000 / 1000.0
    assert all(np.hypot(a[0] - b[0], a[1] - b[1]) >= gap for i, a in enumerate(pts) for b in pts[i + 1 :])


def test_osm_ma_prednost_a_plochy_prvek_jen_jako_zaloha():
    rgb, chm = _scene()
    _paint(rgb, 28, 28, 31, 31, GREY)
    _lift(chm, 28, 28, 31, 31, 2.5)
    ortho, ch = _rasters(rgb, chm)
    ground = box(20, 20, 40, 40)  # 400 m² = jeden křížek
    out = pe.playground_points(
        _feats(ground, _osm_point(25, 25, "climbingframe"), _osm_point(35, 35, "trampoline")), ortho, ch, 4000
    )
    assert _crosses(out) == [([25.0, 25.0], "osm")]
    # jen plochý OSM prvek a nic dalšího → zůstane on
    rgb2, chm2 = _scene()
    ortho2, ch2 = _rasters(rgb2, chm2)
    out2 = pe.playground_points(_feats(ground, _osm_point(35, 35, "trampoline")), ortho2, ch2, 4000)
    assert _crosses(out2) == [([35.0, 35.0], "osm")]


@pytest.mark.parametrize(
    "case, name, expected",
    [
        ("hřiště bez nálezu = záložní křížek v otevřené části", "", ["fallback"]),
        ("dopravní hřiště se přeskakuje", "Dětské dopravní hřiště", []),
    ],
)
def test_zaloha_a_dopravni_hriste(case, name, expected):
    rgb, chm = _scene()
    _paint(rgb, 20, 20, 40, 40, GREY)  # zpevněná plocha bez prvků
    ortho, ch = _rasters(rgb, chm)
    out = pe.playground_points(_feats(box(20, 20, 40, 40), name=name), ortho, ch, 4000)
    srcs = [src for _p, src in _crosses(out)]
    assert srcs == expected, case
    if expected:
        (x, y), _src = _crosses(out)[0]
        assert 20 < x < 40 and 20 < y < 40


def test_osm_bod_mimo_hriste_zustava():
    rgb, chm = _scene()
    ortho, ch = _rasters(rgb, chm)
    out = pe.playground_points(_feats(box(20, 20, 40, 40), _osm_point(5, 5, "swing")), ortho, ch, 4000)
    assert ([5, 5], "osm") in [(f["geometry"]["coordinates"], f["properties"]["source"]) for f in out if f["properties"]["kind"] == pe.KIND]
