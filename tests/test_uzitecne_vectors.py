"""Testy složky uzitecne/ (vegetace / srázy / skály pouzite vs vyhozene)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.pipeline.package_oom import build_oom_zip, oom_metadata
from app.pipeline.uzitecne_vectors import (
    copy_uzitecne_to_output,
    finalize_uzitecne_vectors,
    write_cliff_inspection_vectors,
    write_line_shapefile,
    write_polygon_shapefile,
    write_vegetation_discarded_shp,
)


def test_write_line_and_polygon_shp(tmp_path: Path):
    line = write_line_shapefile(
        tmp_path / "srazy_104.shp",
        [[(0.0, 0.0), (10.0, 0.0), (20.0, 5.0)]],
    )
    assert line is not None and line.is_file()
    assert line.with_suffix(".prj").is_file()

    poly = write_polygon_shapefile(
        tmp_path / "skaly_201.shp",
        [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]],
    )
    assert poly is not None and poly.is_file()


def test_write_discarded_with_reason(tmp_path: Path):
    out = write_line_shapefile(
        tmp_path / "srazy_104.shp",
        [
            [(0.0, 0.0), (5.0, 0.0)],
            [(10.0, 0.0), (15.0, 0.0)],
        ],
        reasons=["budova", "huste_vrstevnice"],
    )
    assert out is not None
    import shapefile

    with shapefile.Reader(str(out.with_suffix(""))) as r:
        assert r.fields[1][0] == "oom_code"
        assert r.fields[2][0] == "duvod"
        recs = list(r.records())
        assert len(recs) == 2
        assert recs[0][1] == "budova"
        assert recs[1][1] == "huste_vrstevnice"


def test_cliff_inspection_and_finalize(tmp_path: Path):
    kp = tmp_path / "work"
    vege = kp / "vegetation"
    vege.mkdir(parents=True)
    # Minimální „vegetation.shp“ – empty-ish via write helper.
    write_polygon_shapefile(
        vege / "vegetation.shp",
        [[(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]],
        oom_code="401",
    )
    # Rename fields aren't required for copy; just need sidecars.
    write_cliff_inspection_vectors(
        kp,
        used_earth=[[(0.0, 0.0), (30.0, 0.0)]],
        used_rocks=[[(0.0, 0.0), (8.0, 0.0), (8.0, 8.0), (0.0, 8.0)]],
        discarded_earth=[([(0.0, 0.0), (2.0, 0.0)], "nizky_schod")],
        discarded_rocks=[
            ([(100.0, 0.0), (110.0, 0.0), (110.0, 10.0), (100.0, 10.0)], "occupancy")
        ],
    )
    root = finalize_uzitecne_vectors(kp)
    assert (root / "README.txt").is_file()
    assert (root / "pouzite" / "srazy_104.shp").is_file()
    assert (root / "pouzite" / "skaly_201.shp").is_file()
    assert (root / "pouzite" / "vegetace.shp").is_file()
    assert (root / "vyhozene" / "srazy_104.shp").is_file()
    assert (root / "vyhozene" / "skaly_201.shp").is_file()

    out = tmp_path / "output"
    out.mkdir()
    n = copy_uzitecne_to_output(kp, out)
    assert n >= 5
    assert (out / "uzitecne" / "pouzite" / "vegetace.shp").is_file()


def test_build_oom_zip_includes_uzitecne(tmp_path: Path):
    kp = tmp_path / "work"
    kp.mkdir()
    (kp / "pullautus.png").write_bytes(b"png")
    (kp / "pullautus.pgw").write_text("1\n0\n0\n-1\n0\n0\n", encoding="utf-8")
    vege = kp / "vegetation"
    vege.mkdir()
    write_polygon_shapefile(
        vege / "vegetation.shp",
        [[(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)]],
        oom_code="401",
    )
    write_cliff_inspection_vectors(
        kp,
        used_earth=[[(0.0, 0.0), (12.0, 0.0)]],
        used_rocks=[],
        discarded_earth=[],
        discarded_rocks=[],
    )

    dest = tmp_path / "out.zip"
    meta = oom_metadata("sprint_2m", {"scalefactor": 0.4}, {"scalefactor": 0.4})
    build_oom_zip(kp, dest, zabaged_clean=None, metadata=meta)

    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
    assert "uzitecne/README.txt" in names
    assert "uzitecne/pouzite/vegetace.shp" in names
    assert "uzitecne/pouzite/srazy_104.shp" in names
    readme = zipfile.ZipFile(dest).read("README_OOM.txt").decode("utf-8")
    assert "uzitecne" in readme


def test_vegetation_discarded_shp(tmp_path: Path):
    try:
        from shapely.geometry import box
    except ImportError:
        return
    out = write_vegetation_discarded_shp(
        tmp_path / "vyhozene_vegetace.shp",
        [(2, "406", box(0, 0, 2, 2))],
        reason="min_plocha",
    )
    assert out is not None and out.is_file()
