from app.pipeline.contours_gdal import chaikin, contour_oom_code


def test_contour_oom_code_forest_formline():
    assert contour_oom_code(240, interval_m=5, formline=2, index_m=None) == "101"
    assert contour_oom_code(242.5, interval_m=5, formline=2, index_m=None) == "103"
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
