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


def test_resolve_formline_mode():
    from app.pipeline.job_options import resolve_formline_mode

    assert resolve_formline_mode({}) == "off"
    assert resolve_formline_mode({"contour_formlines": "sparse"}) == "sparse"
    assert resolve_formline_mode({"contour_formlines": "MORE"}) == "more"
    assert resolve_formline_mode({"contour_formlines": "lots"}) == "off"


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
