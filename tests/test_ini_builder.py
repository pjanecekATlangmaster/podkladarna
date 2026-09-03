from __future__ import annotations

from pathlib import Path

from app.pipeline.ini_builder import kp_contour_interval, write_pullauta_ini


def _ini_map(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, val = stripped.split("=", 1)
        out[key.strip()] = val.strip()
    return out


def test_kp_contour_interval_sprint_no_formlines():
    # formline=0 → KP draws every half-interval line as a full contour
    assert kp_contour_interval(2, 0.4, 0) == 10
    assert kp_contour_interval(2.5, 0.4, 0) == 12.5


def test_kp_contour_interval_forest_with_formlines():
    assert kp_contour_interval(5, 1.0, 2) == 5
    assert abs(kp_contour_interval(5, 0.75, 2) - (5 / 0.75)) < 1e-9


def test_write_pullauta_ini_converts_sprint_interval(tmp_path: Path):
    path = write_pullauta_ini(tmp_path, "sprint_2m")
    ini = _ini_map(path.read_text(encoding="utf-8"))
    assert ini["contour_interval"] == "10"
    assert ini["scalefactor"] == "0.4"
    assert ini["formline"] == "0"
    assert ini["indexcontours"] == "10"
    assert ini["buildingcolor"] == "128,128,128"
    assert ini["vectorconf"] == "zabaged.txt"
    assert (path.parent / "zabaged.txt").is_file()


def test_write_pullauta_ini_sprint_2_5m(tmp_path: Path):
    path = write_pullauta_ini(tmp_path, "sprint_2_5m")
    ini = _ini_map(path.read_text(encoding="utf-8"))
    assert ini["contour_interval"] == "12.5"
    assert ini["indexcontours"] == "12.5"
    assert ini["buildingcolor"] == "128,128,128"


def test_write_pullauta_ini_forest_scales_interval(tmp_path: Path):
    path = write_pullauta_ini(tmp_path, "forest_7500")
    ini = _ini_map(path.read_text(encoding="utf-8"))
    assert float(ini["contour_interval"]) == round(5 / 0.75, 6)
    assert ini["indexcontours"] == "25"
    assert ini["buildingcolor"] == "0,0,0"
    assert ini["vectorconf"] == "zabaged_forest.txt"
    assert (path.parent / "zabaged_forest.txt").is_file()
    assert not (path.parent / "zabaged.txt").exists()

    path10000 = write_pullauta_ini(tmp_path / "f10", "forest_10000")
    ini10000 = _ini_map(path10000.read_text(encoding="utf-8"))
    assert ini10000["contour_interval"] == "5"
    assert ini10000["indexcontours"] == "25"
    assert ini10000["buildingcolor"] == "0,0,0"
    assert ini10000["vectorconf"] == "zabaged_forest.txt"


def test_write_pullauta_ini_mtbo_scales(tmp_path: Path):
    path10 = write_pullauta_ini(tmp_path / "m10", "mtbo_10000")
    ini10 = _ini_map(path10.read_text(encoding="utf-8"))
    assert ini10["scalefactor"] == "1.0"
    assert ini10["contour_interval"] == "5"
    assert ini10["indexcontours"] == "25"
    assert ini10["formline"] == "2"
    assert ini10["vectorconf"] == "zabaged_forest.txt"
    assert ini10["buildingcolor"] == "0,0,0"

    path15 = write_pullauta_ini(tmp_path / "m15", "mtbo_15000")
    ini15 = _ini_map(path15.read_text(encoding="utf-8"))
    assert ini15["scalefactor"] == "1.5"
    assert float(ini15["contour_interval"]) == round(5 / 1.5, 6)
    assert ini15["indexcontours"] == "25"
    assert ini15["vectorconf"] == "zabaged_forest.txt"
