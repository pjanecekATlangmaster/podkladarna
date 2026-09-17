from app.pipeline.contours_gdal import (
    chaikin,
    contour_dem_params,
    contour_line_params,
    contour_oom_code,
    filter_short_polylines,
    refine_contour_polylines,
    stitch_open_polylines,
)
from app.pipeline.vegetation_gdal import rgb_to_vege_class, vege_class_to_oom_code


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
    assert iters == 2
    _cell25, window25, _ = contour_dem_params(0.4, 2.5)
    assert window25 >= 5.0


def test_contour_dem_params_forest_e_plus():
    cell, window, iters = contour_dem_params(1.0, 5.0)
    assert cell == 1.0
    assert window == 6.0
    assert iters == 2
    min_len, gap = contour_line_params(1.0, 5.0)
    assert min_len >= 15.0
    assert gap >= 6.0
    cell75, window75, iters75 = contour_dem_params(0.75, 5.0)
    assert abs(cell75 - 1.0) < 1e-9  # floor 1 m
    assert abs(window75 - 4.5) < 1e-9
    assert iters75 == 2


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


def test_rgb_to_vege_class_kp_palette():
    assert vege_class_to_oom_code(rgb_to_vege_class(255, 219, 166)) == "401"
    assert vege_class_to_oom_code(rgb_to_vege_class(200, 254, 200)) == "406"
    assert vege_class_to_oom_code(rgb_to_vege_class(140, 231, 140)) == "408"
    assert vege_class_to_oom_code(rgb_to_vege_class(80, 209, 80)) == "410"
    assert rgb_to_vege_class(255, 255, 255) == 0
