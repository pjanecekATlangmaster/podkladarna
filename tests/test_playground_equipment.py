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


def _feats(ground, *extra, name="", **more):
    props = {"source": "osm", "kind": "playground", "oom_code": "501", **more}
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
    "case, color, height, peak, found",
    [
        ("konstrukce 2,5 m na štěrku", GREY, 2.5, None, True),
        ("lanová pyramida: lana 2,5 m, vrchol 6,5 m", GREY, 2.5, 6.5, True),
        ("zelený keř 2 m", GRASS, 2.0, None, False),
        ("nízký prvek 0,8 m", GREY, 0.8, None, False),
    ],
)
def test_vyska_z_dmpok(case, color, height, peak, found):
    rgb, chm = _scene()
    _paint(rgb, 28, 28, 30, 31, color)  # 2 × 3 m (plný objekt ≥ 8 m² by byl budka)
    _lift(chm, 28, 28, 30, 31, height)
    if peak:
        _lift(chm, 29, 29, 30, 30, peak)
    ortho, ch = _rasters(rgb, chm)
    cand = pe.detect_candidates(box(10, 10, 50, 50), ortho, ch)
    assert any(abs(x - 29.0) < 2 and abs(y - 29.5) < 2 for x, y, _s, _src in cand) is found, case


def test_okraj_koruny_a_budka_nejsou_prvek():
    rgb, chm = _scene()
    # strom: zelená koruna 8 m, na ni přímo navazuje nezelený pás 2 m (okraj koruny posunutý náklonem ortofota)
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


def _fine(size_m: float = 30.0, base=SAND):
    """Ortofoto 0,125 m (písek) a výška 1 m, levý horní roh (0, size)."""
    n = int(size_m / 0.125)
    rgb = np.zeros((n, n, 3), np.uint8)
    rgb[:] = base
    return rgb, np.zeros((int(size_m), int(size_m)), np.float32)


def _fpaint(rgb, x0, y0, x1, y1, color, size_m=30.0):
    rgb[int((size_m - y1) / 0.125) : int((size_m - y0) / 0.125), int(x0 / 0.125) : int(x1 / 0.125)] = color


SHADOW = (105, 95, 85)


@pytest.mark.parametrize(
    "case, lines, crown, found",
    [
        ("stín sloupu na písku (k severu), křížek u paty", [(15.0, 14.0, 15.25, 17.0)], False, (15.1, 14.1)),
        ("trám houpačky-váhy (jedna čára)", [(13.5, 15.0, 16.5, 15.25)], False, (15.0, 15.1)),
        ("rám pískoviště (dvě rovnoběžky)", [(13.5, 14.0, 16.5, 14.25), (13.5, 16.5, 16.5, 16.75)], False, None),
        ("stín koruny", [(15.0, 14.0, 15.25, 17.0)], True, None),
    ],
)
def test_stin_prvku(case, lines, crown, found):
    rgb, chm = _fine()
    for ln in lines:
        _fpaint(rgb, *ln, SHADOW)
    if crown:  # zelená koruna 8 m hned jižně od stínu
        _fpaint(rgb, 13, 10, 18, 14, (40, 90, 30))
        chm[30 - 14 : 30 - 10, 13:18] = 8.0
    ortho, ch = Raster(rgb, 0.0, 30.0, 0.125, 0.125), Raster(chm, 0.0, 30.0, 1.0, 1.0)
    cand = [c for c in pe.detect_candidates(box(2, 2, 28, 28), ortho, ch) if c[3] == "S"]
    if found is None:
        assert cand == [], case
    else:
        assert len(cand) == 1 and np.hypot(cand[0][0] - found[0], cand[0][1] - found[1]) < 0.6, (case, cand)


def test_smer_slunce_z_korun():
    rgb, chm = _fine(40.0, base=GRASS)
    _fpaint(rgb, 17, 17, 23, 23, (40, 90, 30), size_m=40.0)  # koruna 6 × 6 m
    _fpaint(rgb, 17, 23, 23, 28, (35, 40, 30), size_m=40.0)  # stín severně od ní
    chm[40 - 23 : 40 - 17, 17:23] = 10.0
    nd = pe._sample_chm(Raster(chm, 0.0, 40.0, 1.0, 1.0), 0.0, 40.0, 0.125, rgb.shape[:2])
    sx, sy = pe.sun_shadow_dir(rgb, nd, 0.125)
    assert sy > 0.9 and abs(sx) < 0.35


@pytest.mark.parametrize(
    "case, height, expected",
    [
        ("výška z DMP OK", "raster", ["detect"]),
        ("DMP OK chybí → chm.tif", "none", ["detect"]),
        ("PDAL selže → chm.tif", "error", ["detect"]),
    ],
)
def test_vyska_dmpok_a_zaloha_chm(case, height, expected):
    rgb, chm = _scene()
    _paint(rgb, 28, 28, 30, 31, GREY)
    _lift(chm, 28, 28, 30, 31, 2.5)
    ortho, ch = _rasters(rgb, chm)

    def _height(_grounds):
        if height == "error":
            raise RuntimeError("pdal")
        return ch if height == "raster" else None

    logs = []
    out = pe.playground_points(_feats(box(20, 20, 40, 40)), ortho, None if height == "raster" else ch, 4000,
                               log=logs.append, height=_height)
    assert [src for _p, src in _crosses(out)] == expected, case
    assert ("DMP OK 0,5 m" in logs[-1]) is (height == "raster"), logs


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
    "case, name, double, more, expected",
    [
        ("hřiště bez nálezu = záložní křížek v otevřené části", "", False, {}, ["fallback"]),
        ("dvojitý obrys v OSM = jedno hřiště, jeden křížek", "", True, {}, ["fallback"]),
        ("dopravní hřiště se přeskakuje", "Dětské dopravní hřiště", False, {}, []),
        ("access=private se přeskakuje, OSM bod zůstane", "", False, {"access": "private"}, ["osm"]),
        ("hřiště v soukromé zahradě se přeskakuje", "", False, {"private_garden": True}, ["osm"]),
    ],
)
def test_zaloha_a_dopravni_hriste(case, name, double, more, expected):
    rgb, chm = _scene()
    _paint(rgb, 20, 20, 40, 40, GREY)  # zpevněná plocha bez prvků
    ortho, ch = _rasters(rgb, chm)
    feats = _feats(box(20, 20, 40, 40), name=name, **more)
    if double:
        feats += _feats(box(21, 21, 39, 39))
    if more:  # soukromé hřiště: plochý OSM prvek zůstane bez generalizace (jinde by šel jen jako záloha)
        feats.append(_osm_point(25, 25, "sandpit"))
    out = pe.playground_points(feats, ortho, ch, 4000)
    srcs = [src for _p, src in _crosses(out)]
    assert srcs == expected, case
    if expected:
        (x, y), _src = _crosses(out)[0]
        assert 20 < x < 40 and 20 < y < 40


@pytest.mark.parametrize(
    "case, gardens, pools, private",
    [
        ("hřiště celé v soukromé zahradě", [box(0, 0, 50, 50)], [], True),
        ("hřiště jen z poloviny v zahradě", [box(0, 0, 30, 50)], [], False),
        ("bazén do 10 m", [], [box(45, 20, 50, 25)], True),
        ("bazén dál než 10 m", [], [box(55, 20, 60, 25)], False),
    ],
)
def test_oznaceni_soukromeho_hriste(case, gardens, pools, private):
    feats = _feats(box(20, 20, 40, 40))
    rings = lambda gs: [[list(g.exterior.coords)] for g in gs]  # noqa: E731
    pe.mark_private_playgrounds(feats, rings(gardens), rings(pools))
    assert bool(feats[0]["properties"].get("private_garden")) is private, case


def test_osm_bod_mimo_hriste_zustava():
    rgb, chm = _scene()
    ortho, ch = _rasters(rgb, chm)
    out = pe.playground_points(_feats(box(20, 20, 40, 40), _osm_point(5, 5, "swing")), ortho, ch, 4000)
    assert ([5, 5], "osm") in [(f["geometry"]["coordinates"], f["properties"]["source"]) for f in out if f["properties"]["kind"] == pe.KIND]


@pytest.mark.parametrize(
    "case, preset, flag, called",
    [
        ("sprint s klíčem", "sprint_4000", True, True),
        ("starší job bez klíče beze změny", "sprint_4000", None, False),
        ("les nedetekuje", "forest_10000", True, False),
    ],
)
def test_jen_sprint_s_klicem(tmp_path, monkeypatch, case, preset, flag, called):
    import json

    from app.pipeline import osm_paths as op

    data = {"type": "FeatureCollection", "features": _feats(box(20, 20, 40, 40))}
    if flag is not None:
        data["playground_equipment"] = flag
    (tmp_path / "osm_paths").mkdir()
    (tmp_path / "osm_paths" / "features.geojson").write_text(json.dumps(data), encoding="utf-8")
    calls = []
    monkeypatch.setattr(op, "_sprint_playground_points", lambda wd, feats, *a, **k: calls.append(1) or feats)
    op.build_osm_feature_parts(tmp_path, preset_id=preset, scale=4000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    assert bool(calls) is called, case
