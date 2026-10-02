"""§9.4: vrstevnice z sdíleného DMR dem_filled – jedna pravda, ne DMP."""

from __future__ import annotations

from pathlib import Path

from app.pipeline.contours_gdal import (
    generate_job_contours,
    load_contour_meta,
    qa_contours_vs_shared_dem,
    shared_dem_filled,
)


def test_shared_dem_filled_detects_dem_prep(tmp_path: Path):
    dem_dir = tmp_path / "dem"
    dem_dir.mkdir()
    filled = dem_dir / "dem_filled.tif"
    filled.write_bytes(b"x" * 600)
    assert shared_dem_filled(tmp_path) == filled
    assert shared_dem_filled(tmp_path / "missing") is None


def test_generate_job_contours_prefers_shared_dem(tmp_path: Path, monkeypatch):
    dem_dir = tmp_path / "dem"
    dem_dir.mkdir()
    filled = dem_dir / "dem_filled.tif"
    filled.write_bytes(b"x" * 600)
    laz = tmp_path / "lidar" / "merged.laz"
    laz.parent.mkdir()
    laz.write_bytes(b"laz")

    calls: list[Path] = []

    def fake_from_dem(dem, dest, *, interval_m, scalefactor, log=None):
        calls.append(dem)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"shp")
        return dest

    def boom_shapefile(*_a, **_k):
        raise AssertionError("LAZ path must not run when shared DEM exists")

    monkeypatch.setattr(
        "app.pipeline.contours_gdal.generate_contours_from_dem", fake_from_dem
    )
    monkeypatch.setattr(
        "app.pipeline.contours_gdal.generate_contours_shapefile", boom_shapefile
    )
    monkeypatch.setattr(
        "app.pipeline.contours_gdal.resolve_job_extent",
        lambda *_a, **_k: (0.0, 0.0, 100.0, 100.0),
    )

    out = generate_job_contours(
        tmp_path,
        laz,
        interval_m=5.0,
        formline=0,
        scalefactor=1.0,
        crop_bounds=(0.0, 0.0, 100.0, 100.0),
    )
    assert out.name == "contours.shp"
    assert calls == [filled]
    meta = load_contour_meta(tmp_path)
    assert meta is not None
    assert meta["dem_source"] == "shared_dem"
    assert meta["surface"] == "DMR"
    assert meta["single_truth"] is True
    assert qa_contours_vs_shared_dem(tmp_path) is True


def test_generate_job_contours_fallback_ground_laz(tmp_path: Path, monkeypatch):
    laz = tmp_path / "lidar" / "merged.laz"
    laz.parent.mkdir()
    laz.write_bytes(b"x" * 1200)
    ground = tmp_path / "lidar" / "ground_merged.laz"
    ground.write_bytes(b"g" * 1200)

    used: list[Path] = []

    def fake_shp(src_laz, bounds, dest, *, interval_m, formline, scalefactor, log=None):
        used.append(src_laz)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"shp")
        return dest

    monkeypatch.setattr(
        "app.pipeline.contours_gdal.generate_contours_shapefile", fake_shp
    )
    monkeypatch.setattr(
        "app.pipeline.contours_gdal.resolve_job_extent",
        lambda *_a, **_k: (0.0, 0.0, 10.0, 10.0),
    )

    generate_job_contours(
        tmp_path,
        laz,
        interval_m=2.5,
        formline=0,
        scalefactor=0.4,
        crop_bounds=None,
    )
    assert used == [ground]
    meta = load_contour_meta(tmp_path)
    assert meta["dem_source"] == "laz_ground_fallback"
    assert qa_contours_vs_shared_dem(tmp_path) is False
