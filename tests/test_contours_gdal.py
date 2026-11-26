import pytest

from app.pipeline.contours_gdal import (
    chaikin,
    contour_dem_params,
    contour_line_params,
    contour_oom_code,
    contour_simplify_tol_m,
    filter_short_polylines,
    refine_contour_polylines,
    simplify_contour_polyline,
    stitch_open_polylines,
)
from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.vegetation_gdal import vege_class_to_oom_code


def test_contour_oom_code_forest_index_no_formline():
    assert contour_oom_code(240, interval_m=5, formline=2, index_m=25) == "101"
    assert contour_oom_code(242.5, interval_m=5, formline=2, index_m=25) == "101"
    assert contour_oom_code(250, interval_m=5, formline=2, index_m=25) == "102"


def test_contour_oom_code_sprint_no_formline():
    assert contour_oom_code(202, interval_m=2, formline=0, index_m=10) == "101"
    assert contour_oom_code(200, interval_m=2, formline=0, index_m=10) == "102"


def test_chaikin_keeps_ends():
    pts = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    out = chaikin(pts, iterations=1)
    assert out[0] == (0.0, 0.0)
    assert out[-1] == (10.0, 10.0)
    assert len(out) > len(pts)


def test_contour_dem_params_sprint_smoother_than_scaled():
    cell, window, iters = contour_dem_params(0.4, 2.0)
    assert cell == 1.0
    assert window >= 4.0
    assert iters == 1
    _cell25, window25, _ = contour_dem_params(0.4, 2.5)
    assert window25 >= 5.0


def test_contour_dem_params_forest_e_plus():
    cell, window, iters = contour_dem_params(1.0, 5.0)
    assert cell == 1.0
    assert window == 6.0
    assert iters == 1
    min_len, gap = contour_line_params(1.0, 5.0)
    assert min_len >= 15.0
    assert gap >= 6.0
    cell75, window75, iters75 = contour_dem_params(0.75, 5.0)
    assert abs(cell75 - 1.0) < 1e-9  # floor 1 m
    assert abs(window75 - 4.5) < 1e-9
    assert iters75 == 1


def test_contour_simplify_tol_paper_mm():
    # 0,08 mm @ 1:10 000 ≈ 0,8 m; @ 1:15 000 ≈ 1,2 m
    assert abs(contour_simplify_tol_m(10000) - 0.8) < 1e-9
    assert abs(contour_simplify_tol_m(15000) - 1.2) < 1e-9


def test_simplify_then_curves_not_curve_dense():
    """Chaikin densita → DP → křivky: málo CurveStart, ne 3× Chaikin chaos."""
    from app.pipeline.oom_import import (
        MAP_COORD_CURVE_START,
        convert_polyline_to_curves,
    )

    # Hustá „vrstevnice“ se šumem (~0,2 m) – Chaikin ji zhoustí, DP seřízne.
    pts = [(float(i), 0.2 * ((-1) ** i)) for i in range(0, 81)]
    chaikin_dense = chaikin(pts, iterations=1)
    assert len(chaikin_dense) > len(pts)

    tol = contour_simplify_tol_m(10000)  # 0,8 m
    simplified = simplify_contour_polyline(chaikin_dense, tol)
    assert len(simplified) < len(chaikin_dense) / 2
    assert len(simplified) <= 12  # rovná-ish linie → pár uzlů

    coords = [
        projected_to_map_coord(x, y, ref_x=0, ref_y=0, scale=10000, grivation_deg=0)
        for x, y in simplified
    ]
    curved = convert_polyline_to_curves(coords)
    curve_starts = sum(
        1 for c in curved if len(c) == 3 and c[2] == MAP_COORD_CURVE_START
    )
    # Bez simplify by CurveStart ≈ len(chaikin)-1; po DP jen ≈ len(simplified)-1.
    assert curve_starts == len(simplified) - 1
    assert curve_starts < len(chaikin_dense) / 3
    # Coord slots ≈ 3N-2, ne 3× Chaikin densita.
    assert len(curved) == 3 * len(simplified) - 2
    assert len(curved) < len(chaikin_dense)


def test_simplify_closed_ring_keeps_close():
    ring = [(0.0, 0.0), (10.0, 0.05), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
    out = simplify_contour_polyline(ring, tol_m=0.5)
    assert out[0] == out[-1]
    assert len(out) >= 4


def test_stitch_open_polylines_joins_gap():
    a = [(0.0, 0.0), (10.0, 0.0)]
    b = [(14.0, 0.0), (30.0, 0.0)]
    out = stitch_open_polylines([a, b], gap_m=5.0)
    assert len(out) == 1
    assert out[0][0] == (0.0, 0.0)
    assert out[0][-1] == (30.0, 0.0)


def test_filter_short_and_refine():
    long_line = [(0.0, 0.0), (40.0, 0.0)]
    short = [(0.0, 1.0), (5.0, 1.0)]
    kept = filter_short_polylines([long_line, short], min_length_m=15.0)
    assert kept == [long_line]
    # Dva krátké úseky se spojí a projdou min délkou.
    a = [(0.0, 0.0), (10.0, 0.0)]
    b = [(12.0, 0.0), (25.0, 0.0)]
    refined = refine_contour_polylines([a, b], min_length_m=15.0, stitch_gap_m=3.0)
    assert len(refined) == 1
    assert refined[0][0] == (0.0, 0.0)
    assert refined[0][-1] == (25.0, 0.0)


def test_vege_class_to_oom_code():
    assert vege_class_to_oom_code(1) == "401"
    assert vege_class_to_oom_code(2) == "406"
    assert vege_class_to_oom_code(3) == "408"
    assert vege_class_to_oom_code(4) == "410"
    assert vege_class_to_oom_code(0) is None

def test_build_gdal_contour_parts_simplify_then_curves(tmp_path, monkeypatch):
    """Vrstevnice: Chaikin → DP → as_curves=True (ne surový Chaikin×Bézier)."""
    from app.pipeline import contours_gdal as cg

    shp_dir = tmp_path / "contours"
    shp_dir.mkdir()
    (shp_dir / "contours.shp").write_bytes(b"placeholder")

    captured: dict = {}
    seen_parts: list = []

    def fake_geom_parts(parts, *_args, **kwargs):
        captured.clear()
        captured.update(kwargs)
        seen_parts.clear()
        seen_parts.extend(parts)
        return ['<object type="Path" />']

    # Hustá linie se šumem – bez DP by šla do křivek s desítkami uzlů.
    noisy = [(float(i), 0.15 * ((-1) ** i)) for i in range(0, 61)]
    monkeypatch.setattr(
        cg,
        "_iter_contour_rows",
        lambda _shp: [({"elev": 250.0}, b"dummy")],
    )
    monkeypatch.setattr(
        cg,
        "_wkb_parts",
        lambda _wkb: ([("line", noisy, False)], None),
    )
    monkeypatch.setattr(cg, "_geom_parts_to_objects", fake_geom_parts)
    monkeypatch.setattr(cg, "symbol_index_for_code", lambda *_a, **_k: 1)

    parts = cg.build_gdal_contour_parts(
        tmp_path,
        preset_id="forest_10000",
        scale=10000,
        ref_x=0.0,
        ref_y=0.0,
        grivation_deg=0.0,
        interval_m=5.0,
        index_m=25.0,
    )
    assert captured.get("as_curves") is True
    assert parts and parts[0].count >= 1
    assert seen_parts
    verts = seen_parts[0][1]
    assert len(verts) < len(noisy) / 2
    assert len(verts) <= 10


def test_numpy_dp_matches_recursive_reference():
    import math
    import random

    from app.pipeline.contours_gdal import _simplify_polyline_dp_py, simplify_polyline_dp

    rnd = random.Random(3)
    for n in (40, 200, 1500):
        pts = [(i * 0.7, 5 * math.sin(i / 7.0) + rnd.uniform(-0.6, 0.6)) for i in range(n)]
        pts[n // 2] = pts[n // 2 - 1]  # duplicitní bod
        for tol in (0.05, 0.8, 3.0):
            assert simplify_polyline_dp(pts, tol) == _simplify_polyline_dp_py(pts, tol)


def _line(x0, x1, y_of_x, step=1.0):
    n = int(round((x1 - x0) / step))
    return [(x0 + i * step, y_of_x(x0 + i * step)) for i in range(n + 1)]


def _circle(r, n=360):
    import math

    pts = [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    return pts + [pts[0]]


def test_formline_uniform_slope_is_dropped():
    """Půlová linie přesně uprostřed mezi vrstevnicemi nic nepřidá."""
    from app.pipeline.contours_gdal import select_formlines

    contours = [
        (0.0, _line(-300, 300, lambda _x: 0.0)),
        (5.0, _line(-300, 300, lambda _x: 40.0)),
    ]
    formlines = [(2.5, _line(-300, 300, lambda _x: 20.0))]
    for mode in ("sparse", "more"):
        assert select_formlines(formlines, contours, interval_m=5, scale=10000, mode=mode) == []


def test_formline_spur_keeps_only_the_bend():
    """Výběžek mezi vrstevnicemi: půlová se přimkne k dolní → kus kolem výběžku."""
    import math

    from app.pipeline.contours_gdal import polyline_length_m, select_formlines

    # Krajní úrovně jen kvůli obálce dat (u okraje výřezu se 103 nevybírá).
    contours = [
        (-5.0, _line(-300, 300, lambda _x: -40.0)),
        (0.0, _line(-300, 300, lambda _x: 0.0)),
        (5.0, _line(-300, 300, lambda _x: 40.0)),
        (10.0, _line(-300, 300, lambda _x: 80.0)),
    ]
    spur = lambda x: 20.0 - 16.0 * math.exp(-((x / 25.0) ** 2))  # noqa: E731
    formlines = [(2.5, _line(-300, 300, spur))]
    got = select_formlines(formlines, contours, interval_m=5, scale=10000, mode="sparse")
    assert len(got) == 1
    elev, pts, closed = got[0]
    assert elev == 2.5 and not closed
    xs = [p[0] for p in pts]
    assert min(xs) < 0 < max(xs)
    # Jen okolí výběžku, ne celá 600m linie.
    assert 30.0 <= polyline_length_m(pts) <= 200.0


def test_formline_summit_between_levels_keeps_whole_ring():
    """Vrchol nad poslední vrstevnicí (horní úroveň chybí) → celý kroužek 103."""
    from app.pipeline.contours_gdal import select_formlines

    contours = [(5.0, _circle(90.0)), (0.0, _circle(140.0))]
    formlines = [(7.5, _circle(35.0)), (2.5, _circle(115.0))]
    got = select_formlines(formlines, contours, interval_m=5, scale=10000, mode="sparse")
    assert [(e, c) for e, _p, c in got] == [(7.5, True)]


def test_formline_mode_off_selects_nothing():
    from app.pipeline.contours_gdal import select_formlines

    contours = [(5.0, _circle(90.0))]
    formlines = [(7.5, _circle(35.0))]
    assert select_formlines(formlines, contours, interval_m=5, scale=10000, mode="off") == []


@pytest.mark.parametrize(
    "opts, expected",
    [({}, "off"), ({"contour_formlines": "sparse"}, "sparse"), ({"contour_formlines": "MORE"}, "more"),
     ({"contour_formlines": "lots"}, "off"), ({"contour_formlines": "auto"}, "off"),
     # podle země: sever „víc“ (reálné mapy 5 m + pomocné), Česko beze změny, ruční volba má přednost
     ({"country": "FI"}, "more"), ({"country": "NO", "contour_formlines": "auto"}, "more"),
     ({"country": "FI", "contour_formlines": "off"}, "off"), ({"country": "CZ"}, "off")],
)
def test_resolve_formline_mode(opts, expected):
    from app.pipeline.job_options import resolve_formline_mode

    assert resolve_formline_mode(opts) == expected


def test_build_gdal_contour_parts_adds_formline_part(tmp_path, monkeypatch):
    """S volbou se z formlines_all.shp vybrané kusy přidají jako 103."""
    from app.pipeline import contours_gdal as cg

    shp_dir = tmp_path / "contours"
    shp_dir.mkdir()
    (shp_dir / "contours.shp").write_bytes(b"placeholder")
    (shp_dir / cg.FORMLINES_ALL_NAME).write_bytes(b"placeholder")
    rows = {
        "contours.shp": [({"elev": 5.0}, _circle(90.0)), ({"elev": 0.0}, _circle(140.0))],
        cg.FORMLINES_ALL_NAME: [({"elev": 7.5}, _circle(35.0)), ({"elev": 2.5}, _circle(115.0))],
    }
    monkeypatch.setattr(cg, "_iter_contour_rows", lambda shp: rows[shp.name])
    monkeypatch.setattr(cg, "_wkb_parts", lambda pts: ([("line", pts, True)], None))
    codes = {"101": 1, "102": 2, "103": 3}
    monkeypatch.setattr(cg, "symbol_index_for_code", lambda _p, _s, code: codes.get(code))
    symbols: list[int] = []

    def fake_objects(parts, symbol_index, **_kw):
        symbols.append(symbol_index)
        return ["<object />" for _ in parts]

    monkeypatch.setattr(cg, "_geom_parts_to_objects", fake_objects)
    kw = dict(
        preset_id="forest_10000", scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0,
        interval_m=5.0, index_m=25.0,
    )
    off = cg.build_gdal_contour_parts(tmp_path, **kw)
    assert "Pomocné vrstevnice (GDAL)" not in [p.name for p in off]
    on = {p.name: p for p in cg.build_gdal_contour_parts(tmp_path, formlines="sparse", **kw)}
    assert on["Pomocné vrstevnice (GDAL)"].count == 1
    assert symbols[-1] == 3


def test_formline_mode_all_keeps_every_half_line():
    """„Všude“: celé půlové linie jako vodítko (jako OCAD), jen bez útržků."""
    from app.pipeline.contours_gdal import select_formlines

    contours = [
        (0.0, _line(-300, 300, lambda _x: 0.0)),
        (5.0, _line(-300, 300, lambda _x: 40.0)),
    ]
    formlines = [
        (2.5, _line(-300, 300, lambda _x: 20.0)),  # rovnoměrný svah – i tak
        (2.5, _line(0, 8, lambda _x: 25.0)),  # 8 m útržek < 1,5 mm @ 1:10000
    ]
    got = select_formlines(formlines, contours, interval_m=5, scale=10000, mode="all")
    assert len(got) == 1
    assert got[0][0] == 2.5 and len(got[0][1]) == 601
    from app.pipeline.job_options import resolve_formline_mode

    assert resolve_formline_mode({"contour_formlines": "all"}) == "all"


def test_build_gdal_contour_parts_reuses_geometry_between_maps(tmp_path, monkeypatch):
    """Les + MTBO (plná i „čistá“) z jednoho contours.shp: geometrie jednou, symboly zvlášť."""
    import os

    from app.pipeline import contours_gdal as cg

    shp_dir = tmp_path / "contours"
    shp_dir.mkdir()
    shp = shp_dir / "contours.shp"
    shp.write_bytes(b"placeholder")
    reads: list[str] = []

    def fake_rows(path):
        reads.append(path.name)
        return [({"elev": 5.0}, _circle(90.0)), ({"elev": 25.0}, _circle(140.0))]

    monkeypatch.setattr(cg, "_iter_contour_rows", fake_rows)
    monkeypatch.setattr(cg, "_wkb_parts", lambda pts: ([("line", pts, True)], None))
    symbols = {"forest_10000": {"101": 1, "102": 2}, "mtbo_10000": {"101": 11, "102": 12}}
    monkeypatch.setattr(
        cg, "symbol_index_for_code", lambda preset, _s, code: symbols[preset].get(code)
    )
    monkeypatch.setattr(
        cg,
        "_geom_parts_to_objects",
        lambda parts, symbol_index, **_kw: [f"<object sym='{symbol_index}' n='{len(p[1])}'/>" for p in parts],
    )
    kw = dict(scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0, interval_m=5.0, index_m=25.0)
    forest = cg.build_gdal_contour_parts(tmp_path, preset_id="forest_10000", **kw)
    forest_again = cg.build_gdal_contour_parts(tmp_path, preset_id="forest_10000", **kw)
    mtbo = cg.build_gdal_contour_parts(tmp_path, preset_id="mtbo_10000", **kw)
    assert reads == ["contours.shp"]
    assert [(p.name, p.objects_xml) for p in forest_again] == [(p.name, p.objects_xml) for p in forest]
    assert "sym='11'" in mtbo[0].objects_xml and "sym='12'" in mtbo[1].objects_xml
    assert [p.objects_xml.replace("sym='1", "sym='") for p in mtbo] == [
        p.objects_xml for p in forest
    ]

    # Přegenerované vrstevnice (jiný soubor) se načtou znovu.
    shp.write_bytes(b"placeholder-new")
    os.utime(shp, ns=(shp.stat().st_atime_ns, shp.stat().st_mtime_ns + 10**9))
    cg.build_gdal_contour_parts(tmp_path, preset_id="forest_10000", **kw)
    assert reads == ["contours.shp", "contours.shp"]


def _ring_at(cx, cy, r, n=72):
    return [(cx + x, cy + y) for x, y in _circle(r, n)]


@pytest.mark.parametrize(
    "candidates, inner, expect",
    [
        # kopeček s vrcholem mezi ¼ a ½: kroužek ve ¼
        ([(6.25, _ring_at(0, 0, 40.0))], [], [6.25]),
        # dva soustředné kroužky téhož tvaru (¼ a ¾): jen jeden (stejně blízko půlce → větší)
        ([(6.25, _ring_at(0, 0, 40.0)), (8.75, _ring_at(0, 0, 25.0))], [], [6.25]),
        # malý kroužek (pod 3 mm² na papíře) nic
        ([(6.25, _ring_at(0, 0, 8.0))], [], []),
        # vrstevnice uvnitř kroužku → tvar už ukazuje základní
        ([(6.25, _ring_at(0, 0, 60.0))], [(10.0, _ring_at(0, 0, 20.0))], []),
        # těsně u vrstevnice (pod 0,35 mm) se nevejde
        ([(6.25, _ring_at(0, 0, 87.0))], [], []),
        # už vybraná půlová 103 v témže tvaru → druhá ne
        ([(6.25, _ring_at(0, 0, 40.0))], "half", []),
    ],
)
def test_closed_formline_between_levels(candidates, inner, expect):
    """Uzavřená 103 ve ¼ / ¾ jen kde tvar minou vrstevnice i půlová 103, nejvýš jedna na tvar."""
    from app.pipeline.contour_shapes import select_closed_formlines

    contours = [(5.0, _circle(90.0)), (0.0, _circle(300.0))]
    selected = []
    if inner == "half":
        selected = [_ring_at(0, 0, 30.0)]
    else:
        contours += inner
    got = select_closed_formlines(candidates, contours, selected, interval_m=5, scale=10000, mode="sparse")
    assert [e for e, _p, closed in got if closed] == expect


def test_slope_lines_on_depression_ring(tmp_path):
    """Spádnice na uzavřené vrstevnici prohlubně, směr dovnitř; na kopečku žádná."""
    import math

    import numpy as np

    from app.pipeline import contour_shapes as cs

    ring = _ring_at(100.0, 100.0, 30.0)
    ticks = cs.slope_ticks(ring, scale=10000)
    assert len(ticks) == 1
    x, y, dx, dy = ticks[0]
    # směr k středu kroužku
    assert (100.0 - x) * dx + (100.0 - y) * dy > 0
    # OOM: 0 = čárka k severu, kladně proti směru hodinových ručiček (mapa y dolů)
    to_map = lambda px, py: (int(px * 100), int(-py * 100))  # noqa: E731
    assert cs.ocad_rotation(to_map, 0, 0, 0.0, 1.0) == pytest.approx(0.0)
    assert cs.ocad_rotation(to_map, 0, 0, -1.0, 0.0) == pytest.approx(math.pi / 2)
    # opačný směr obíhání dá stejný směr spádnice
    x2, y2, dx2, dy2 = cs.slope_ticks(list(reversed(ring)), scale=10000)[0]
    assert (100.0 - x2) * dx2 + (100.0 - y2) * dy2 > 0

    gdal = pytest.importorskip("osgeo.gdal")
    yy, xx = np.mgrid[0:200, 0:200]
    r = np.hypot(xx + 0.5 - 100.0, (200 - yy - 0.5) - 100.0)
    for sign, depression in ((1.0, True), (-1.0, False)):
        path = tmp_path / f"dem{sign}.tif"
        ds = gdal.GetDriverByName("GTiff").Create(str(path), 200, 200, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((0.0, 1.0, 0.0, 200.0, 0.0, -1.0))
        ds.GetRasterBand(1).WriteArray((100.0 + sign * 0.1 * r).astype("float32"))
        ds = None
        assert cs.is_depression(cs._Dem(path), ring, 100.0 + sign * 3.0) is depression


# Mimo Česko: vrstevnice ve vodě (dno nádrže z DMR) se nekreslí, přes břeh končí na břehu, ostrov zůstává.
@pytest.mark.parametrize(
    "pts, expect_pieces, expect_len",
    [
        ([(10.0, 10.0), (90.0, 10.0), (90.0, 90.0), (10.0, 90.0), (10.0, 10.0)], 0, 0.0),  # celá ve vodě (dno)
        ([(-50.0, 20.0), (150.0, 20.0)], 2, 100.0),  # přes nádrž: dva kusy, konec na břehu
        ([(-50.0, 20.0), (-5.0, 20.0)], 1, 45.0),  # mimo vodu beze změny
        ([(42.0, 42.0), (58.0, 42.0), (58.0, 58.0), (42.0, 58.0), (42.0, 42.0)], 1, 64.0),  # na ostrově
        ([(-10.0, 20.0), (5.0, 20.0)], 0, 0.0),  # zbytek na břehu kratší než min. délka
    ],
)
def test_contours_clipped_by_water(pts, expect_pieces, expect_len):
    from shapely.geometry import LineString, Polygon

    from app.pipeline.contours_gdal import clip_lines_outside_water, is_closed_polyline

    lake = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)], [[(40, 40), (60, 40), (60, 60), (40, 60)]])
    out, _ = clip_lines_outside_water([("line", pts, is_closed_polyline(pts))], lake, min_length_m=15.0)
    assert len(out) == expect_pieces
    assert sum(LineString(p[1]).length for p in out) == pytest.approx(expect_len, abs=0.01)
    assert all(LineString(p[1]).intersection(lake).length < 1e-6 for p in out)


def test_contour_water_mask_off_in_czechia(tmp_path, monkeypatch):
    import json

    from app.pipeline import contours_gdal, country_profile

    (tmp_path / "osm_paths").mkdir()
    lake = {"type": "Polygon", "coordinates": [[[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]}
    feat = {"type": "Feature", "properties": {"kind": "water_body"}, "geometry": lake}
    (tmp_path / "osm_paths" / "features.geojson").write_text(json.dumps({"features": [feat]}))
    monkeypatch.setenv(country_profile.JOB_COUNTRY_ENV, "CZ")
    assert contours_gdal.contour_water_mask(tmp_path) is None
    monkeypatch.setenv(country_profile.JOB_COUNTRY_ENV, "FR")
    assert contours_gdal.contour_water_mask(tmp_path).area == pytest.approx(10000.0)


# Malý kroužek (spádnice se nevejde) → značka minimální velikosti: kupa 101.2 / prohlubeň 101.3;
# větší kroužek a sada bez značek (sprint) zůstanou čárou (Petr 2026-11-26, Písková ohrada).
@pytest.mark.parametrize(
    "radius, inside, codes, expect_symbol",
    [
        (2.0, 99.0, {"101.2": 12, "101.3": 13}, 13),  # malá prohlubeň
        (2.0, 101.0, {"101.2": 12, "101.3": 13}, 12),  # malá kupa
        (30.0, 99.0, {"101.2": 12, "101.3": 13}, None),  # velký kroužek – čárou se spádnicí
        (2.0, 99.0, {}, None),  # sprint bez značek minimální velikosti
        ("short", 99.0, {"101.2": 12, "101.3": 13}, 13),  # kroužek pod min. délkou, 1 m hluboký – vrátí se
        ("short", 99.8, {"101.2": 12, "101.3": 13}, None),  # pod min. délkou a mělký (0,2 m) – ne
    ],
)
def test_min_size_rings_as_points(monkeypatch, tmp_path, radius, inside, codes, expect_symbol):
    from app.pipeline import contour_shapes as cs
    from app.pipeline import contours_gdal as cg

    if radius == "short":
        ring = _ring_at(100.0, 100.0, 1.5)
        entry = {"smoothed": [("101", [])], "rings": [], "short_rings": [("101", 100.0, ring)]}
    else:
        ring = _ring_at(100.0, 100.0, radius)
        entry = {"smoothed": [("101", [("line", ring, True)])], "rings": [("101", 100.0, ring)]}

    class _FakeDem:
        def inside_median(self, _poly):
            return inside

        def inside_relief(self, _poly, elev):
            return abs(inside - elev)

    monkeypatch.setattr(cs, "load_dem", lambda _d: _FakeDem())
    monkeypatch.setattr(cg, "_has_symbol", lambda _p, _s, code: code in codes)
    monkeypatch.setattr(cg, "symbol_index_for_code", lambda _p, _s, code: codes.get(code))
    lines, objects = cg._min_size_ring_objects(
        tmp_path, entry, None, preset_id="forest_10000", scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0
    )
    drawn = len(entry["smoothed"][0][1])
    if expect_symbol is None:
        assert objects == [] and len(lines[0][1]) == drawn
    else:
        assert len(objects) == 1 and f'symbol="{expect_symbol}"' in objects[0]
        assert lines[0][1] == []
    assert len(entry["smoothed"][0][1]) == drawn  # cache geometrie beze změny


# Značka malého kroužku a kupka / ďolík z DEM na témže místě: zůstane jen bod z DEM
# (Petr 2026-11-26, job b4465cdf3698: 101.3 přes 111).
@pytest.mark.parametrize(
    "dem_xy, dem_symbol, expect_kept",
    [
        ((300, -200), 11, 0),  # ďolík 111 uvnitř oválku 101.3 – značka kroužku pryč
        ((900, 0), 11, 0),  # do 1 mm
        ((1600, 0), 11, 1),  # dál než 1 mm – obojí
        ((0, 0), 99, 1),  # jiný bod (např. balvan) – beze změny
    ],
)
def test_min_size_symbol_yields_to_dem_point(monkeypatch, dem_xy, dem_symbol, expect_kept):
    from app.pipeline import contours_gdal as cg
    from app.pipeline.oom_import import OomObjectPart, _geom_parts_to_objects

    monkeypatch.setattr(cg, "_has_symbol", lambda _p, _s, code: code in ("109", "111", "112"))
    monkeypatch.setattr(cg, "symbol_index_for_code", lambda _p, _s, code: {"109": 10, "111": 11, "112": 12}.get(code))
    ring = (
        '            <object type="0" symbol="3" rotation="0.5">\n'
        '                <coords count="1">0 0;</coords>\n            </object>'
    )
    dem = (
        f'            <object type="0" symbol="{dem_symbol}">\n'
        f'                <coords count="1">{dem_xy[0]} {dem_xy[1]};</coords>\n            </object>'
    )
    parts = [
        OomObjectPart(name=cg.MIN_SIZE_PART_NAME, objects_xml=ring, count=1),
        OomObjectPart(name="DXF", objects_xml=dem, count=1),
    ]
    cg.drop_min_size_near_dem_points(parts, preset_id="forest_10000", scale=10000)
    kept = [p for p in parts if p.name == cg.MIN_SIZE_PART_NAME]
    assert sum(p.count for p in kept) == expect_kept
    assert parts[-1].objects_xml == dem
