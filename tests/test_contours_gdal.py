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
