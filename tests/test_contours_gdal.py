from app.pipeline.contours_gdal import chaikin, contour_oom_code
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


def test_rgb_to_vege_class_kp_palette():
    assert vege_class_to_oom_code(rgb_to_vege_class(255, 219, 166)) == "401"
    assert vege_class_to_oom_code(rgb_to_vege_class(200, 254, 200)) == "406"
    assert vege_class_to_oom_code(rgb_to_vege_class(140, 231, 140)) == "408"
    assert vege_class_to_oom_code(rgb_to_vege_class(80, 209, 80)) == "410"
    assert rgb_to_vege_class(255, 255, 255) == 0
