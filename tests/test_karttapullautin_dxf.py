from __future__ import annotations

import zipfile
from pathlib import Path

from app.pipeline.karttapullautin_dxf import (
    collect_dxf_for_zip,
    prune_heavy_intermediate_dxf,
)
from app.pipeline.package_oom import build_oom_zip, oom_metadata


def test_collect_dxf_prefers_out2_as_contours(tmp_path: Path):
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "out2.dxf").write_text("contour lines", encoding="utf-8")
    (temp / "contours03.dxf").write_text("x" * 5000, encoding="utf-8")
    (temp / "out.dxf").write_text("intermediate", encoding="utf-8")

    got = collect_dxf_for_zip(temp)
    assert "contours.dxf" in got
    assert got["contours.dxf"].name == "out2.dxf"
    assert "contours03.dxf" not in got


def test_prune_removes_contours03(tmp_path: Path):
    temp = tmp_path / "temp"
    temp.mkdir()
    huge = temp / "contours03.dxf"
    huge.write_text("x" * 2000, encoding="utf-8")

    prune_heavy_intermediate_dxf(temp)
    assert not huge.exists()


def test_build_oom_zip_skips_contours03(tmp_path: Path):
    kp = tmp_path / "work"
    kp.mkdir()
    (kp / "pullautus.png").write_bytes(b"png")
    (kp / "pullautus.pgw").write_text("1\n0\n0\n-1\n0\n0\n", encoding="utf-8")
    temp = kp / "temp"
    temp.mkdir()
    (temp / "out2.dxf").write_text("contours", encoding="utf-8")
    (temp / "contours03.dxf").write_text("huge", encoding="utf-8")

    dest = tmp_path / "out.zip"
    meta = oom_metadata("sprint_2m", {"scalefactor": 0.4}, {"scalefactor": 0.4})
    build_oom_zip(kp, dest, zabaged_clean=None, metadata=meta)

    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
    assert "karttapullautin/contours.dxf" in names
    assert "karttapullautin/contours03.dxf" not in names
    assert "karttapullautin/out.dxf" not in names
