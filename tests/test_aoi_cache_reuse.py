"""§10 AOI cache: param-only change skips download/DEM prep/shade."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.download_cache import (
    aoi_cache_key,
    file_fingerprint,
    force_refresh_enabled,
    lidar_crop_cache_dir,
    persist_lidar_crop,
    persist_shade,
    persist_surfaces,
    surfaces_cache_dir,
    try_restore_lidar_crop,
    write_meta,
)


@pytest.fixture()
def bbox():
    return (14.4, 50.08, 14.42, 50.09)


def test_force_refresh_option_and_env(monkeypatch):
    monkeypatch.setattr("app.settings.FORCE_REFRESH_DEFAULT", False)
    assert force_refresh_enabled(None) is False
    assert force_refresh_enabled({}) is False
    assert force_refresh_enabled({"force_refresh": True}) is True
    monkeypatch.setattr("app.settings.FORCE_REFRESH_DEFAULT", True)
    assert force_refresh_enabled({}) is True


def test_aoi_key_ignores_contour_and_benches(bbox):
    """Ekvidistance / lavičky nesmí měnit AOI klíč (jen závislé větve)."""
    base = aoi_cache_key(bbox, resolution_m=1.0, sheet_ids=["PRAH77"], scalefactor=1.0)
    # Klíč se nestaví z options — stejný AOI = stejný klíč nezávisle na params.
    again = aoi_cache_key(bbox, resolution_m=1.0, sheet_ids=["PRAH77"], scalefactor=1.0)
    assert base == again
    different_sf = aoi_cache_key(
        bbox, resolution_m=1.0, sheet_ids=["PRAH77"], scalefactor=0.75
    )
    assert different_sf != base


def test_lidar_crop_restore_skips_merge(tmp_path: Path, monkeypatch, bbox):
    from app import settings

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    cache = lidar_crop_cache_dir(bbox, scalefactor=1.0, sheet_ids=["PRAH77"])
    src = tmp_path / "job1" / "lidar"
    src.mkdir(parents=True)
    for name in ("ground_merged.laz", "veg_merged.laz", "merged_crop.laz"):
        (src / name).write_bytes(b"x" * 2000)
    persist_lidar_crop(cache, src, bbox_wgs84=bbox, sheet_ids=["PRAH77"], scalefactor=1.0)

    dest = tmp_path / "job2" / "lidar"
    hit = try_restore_lidar_crop(cache, dest)
    assert hit is not None
    assert hit.is_file()
    assert (dest / "ground_merged.laz").is_file()

    # force → miss
    assert try_restore_lidar_crop(cache, tmp_path / "job3" / "lidar", force=True) is None


def test_surfaces_reuse_skips_pdal(tmp_path: Path, monkeypatch, bbox):
    from app import settings
    from app.pipeline.dem_prep import prepare_job_surfaces

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    cache = surfaces_cache_dir(
        bbox, resolution_m=1.0, sheet_ids=["PRAH77"], scalefactor=1.0
    )

    work1 = tmp_path / "j1" / "work"
    lidar1 = work1 / "lidar"
    lidar1.mkdir(parents=True)
    ground = lidar1 / "ground_merged.laz"
    ground.write_bytes(b"laz" * 400)
    bounds = (-700010.0, -1050010.0, -700000.0, -1050000.0)

    pdal_calls: list[str] = []

    def fake_pdal(laz, b, dest, *, resolution_m=1.0, log=None):
        pdal_calls.append(laz.name)
        dest.write_bytes(b"dem" * 200)

    def fake_fill(src, dest, *, log=None):
        dest.write_bytes(src.read_bytes() + b"fill")

    with (
        patch("app.pipeline.dem_prep._pdal_dem_from_laz", side_effect=fake_pdal),
        patch("app.pipeline.dem_prep._fill_dem_nodata", side_effect=fake_fill),
    ):
        prepare_job_surfaces(
            work1, bounds, resolution_m=1.0, cache_dir=cache, log=None
        )
    assert len(pdal_calls) == 1

    # Stejný AOI, jiný job — jen změna „parametrů“ (cache_dir stejný).
    work2 = tmp_path / "j2" / "work"
    lidar2 = work2 / "lidar"
    lidar2.mkdir(parents=True)
    # Stejný obsah + copy2 mtime → fingerprint match po persist z job1.
    import shutil

    shutil.copy2(ground, lidar2 / "ground_merged.laz")

    with (
        patch(
            "app.pipeline.dem_prep._pdal_dem_from_laz",
            side_effect=fake_pdal,
        ) as mock_pdal,
        patch("app.pipeline.dem_prep._fill_dem_nodata", side_effect=fake_fill),
    ):
        result = prepare_job_surfaces(
            work2, bounds, resolution_m=1.0, cache_dir=cache, log=None
        )
    mock_pdal.assert_not_called()
    assert result.dem_filled.is_file()
    assert (work2 / "dem" / "dem_filled.tif").is_file()


def test_surfaces_force_refresh_reruns_pdal(tmp_path: Path, monkeypatch, bbox):
    from app import settings
    from app.pipeline.dem_prep import prepare_job_surfaces

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    cache = surfaces_cache_dir(bbox, resolution_m=1.0, sheet_ids=["PRAH77"])
    work = tmp_path / "work"
    lidar = work / "lidar"
    lidar.mkdir(parents=True)
    (lidar / "ground_merged.laz").write_bytes(b"laz" * 400)
    bounds = (0.0, 0.0, 10.0, 10.0)

    dem_dir = work / "dem"
    dem_dir.mkdir(parents=True)
    (dem_dir / "dem_filled.tif").write_bytes(b"old" * 200)
    (dem_dir / "dem_raw.tif").write_bytes(b"old" * 200)
    persist_surfaces(
        cache,
        dem_dir,
        bounds=bounds,
        resolution_m=1.0,
        ground_fp=file_fingerprint(lidar / "ground_merged.laz"),
        surface_fp=None,
    )

    calls = {"n": 0}

    def fake_pdal(laz, b, dest, *, resolution_m=1.0, log=None):
        calls["n"] += 1
        dest.write_bytes(b"new" * 200)

    def fake_fill(src, dest, *, log=None):
        dest.write_bytes(b"filled" * 40)

    with (
        patch("app.pipeline.dem_prep._pdal_dem_from_laz", side_effect=fake_pdal),
        patch("app.pipeline.dem_prep._fill_dem_nodata", side_effect=fake_fill),
    ):
        prepare_job_surfaces(
            work,
            bounds,
            cache_dir=cache,
            force_refresh=True,
        )
    assert calls["n"] == 1


def test_shade_reuse_when_dem_unchanged(tmp_path: Path, monkeypatch, bbox):
    from app import settings
    from app.pipeline.job_grid import JobGrid, write_job_grid
    from app.pipeline.shade import build_job_shade

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    cache = surfaces_cache_dir(bbox, resolution_m=1.0)
    work = tmp_path / "work"
    write_job_grid(work, (0.0, 0.0, 100.0, 100.0), resolution_m=1.0)
    dem = work / "dem"
    dem.mkdir(parents=True)
    dem_tif = dem / "dem_filled.tif"
    dem_tif.write_bytes(b"dem" * 200)

    shade = work / "shade"
    shade.mkdir(parents=True)
    (shade / "hillshade.png").write_bytes(b"png" * 30)
    JobGrid.load(work).to_pgw().write(shade / "hillshade.pgw")
    persist_shade(cache, shade, dem_fp=file_fingerprint(dem_tif))

    # Nový work dir se stejným DEM fingerprintem (copy2).
    work2 = tmp_path / "work2"
    write_job_grid(work2, (0.0, 0.0, 100.0, 100.0), resolution_m=1.0)
    dem2 = work2 / "dem"
    dem2.mkdir(parents=True)
    import shutil

    shutil.copy2(dem_tif, dem2 / "dem_filled.tif")

    with (
        patch("app.pipeline.shade.build_hillshade_from_dem") as mock_local,
        patch("app.pipeline.shade.fetch_hillshade_wms_for_grid") as mock_wms,
    ):
        out = build_job_shade(work2, cache_dir=cache, prefer_local=True)
    mock_local.assert_not_called()
    mock_wms.assert_not_called()
    assert out is not None
    assert out.is_file()


def test_fetch_lidar_force_refresh_bypasses_fresh(tmp_path: Path, monkeypatch):
    from app import settings
    from app.pipeline import fetch_openzu as fo

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    folder = tmp_path / "lidar" / "sm5" / "PRAH77"
    folder.mkdir(parents=True)
    laz = folder / "DMR5G.laz"
    laz.write_bytes(b"x" * 2000)
    from datetime import datetime, timezone

    write_meta(folder, downloaded_at=datetime.now(timezone.utc).isoformat())

    hits: list[bool] = []

    def fake_download(url, dest):
        hits.append(True)
        dest.write_bytes(b"zip" * 400)

    def fake_extract(zpath, dest_laz):
        dest_laz.write_bytes(b"y" * 2000)

    with (
        patch.object(fo, "_http_download", side_effect=fake_download),
        patch.object(fo, "_extract_laz", side_effect=fake_extract),
    ):
        # cache hit
        out = fo._cached_laz("PRAH77", "DMR5G", "http://example/x.zip", None)
        assert out == laz
        assert hits == []
        # force
        out2 = fo._cached_laz(
            "PRAH77",
            "DMR5G",
            "http://example/x.zip",
            None,
            force_refresh=True,
        )
        assert out2 == laz
        assert len(hits) == 1


def test_param_only_pipeline_skips_merge_and_dem(tmp_path: Path, monkeypatch, bbox):
    """Simulace: 2. job se stejnou AOI — bez merge/PDAL (jen param-only větve by běžely)."""
    from app import settings
    from app.pipeline.dem_prep import prepare_job_surfaces

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)

    sheets = ["PRAH77"]
    crop_c = lidar_crop_cache_dir(bbox, scalefactor=1.0, sheet_ids=sheets)
    surf_c = surfaces_cache_dir(
        bbox, resolution_m=1.0, sheet_ids=sheets, scalefactor=1.0
    )
    seed_lidar = tmp_path / "_seed" / "lidar"
    seed_lidar.mkdir(parents=True)
    for name in ("ground_merged.laz", "veg_merged.laz", "merged_crop.laz"):
        (seed_lidar / name).write_bytes(b"laz" * 500)
    persist_lidar_crop(
        crop_c, seed_lidar, bbox_wgs84=bbox, sheet_ids=sheets, scalefactor=1.0
    )
    seed_dem = tmp_path / "_seed" / "dem"
    seed_dem.mkdir(parents=True)
    (seed_dem / "dem_filled.tif").write_bytes(b"dem" * 200)
    (seed_dem / "dem_raw.tif").write_bytes(b"dem" * 200)
    persist_surfaces(
        surf_c,
        seed_dem,
        bounds=(-700010.0, -1050010.0, -700000.0, -1050000.0),
        resolution_m=1.0,
        ground_fp=file_fingerprint(seed_lidar / "ground_merged.laz"),
        surface_fp=file_fingerprint(seed_lidar / "veg_merged.laz"),
    )

    work = tmp_path / "job_param" / "work"
    work.mkdir(parents=True)
    restored = try_restore_lidar_crop(crop_c, work / "lidar")
    assert restored is not None

    pdal_mock = MagicMock()
    with (
        patch("app.pipeline.dem_prep._pdal_dem_from_laz", pdal_mock),
        patch("app.pipeline.dem_prep._fill_dem_nodata"),
    ):
        prepare_job_surfaces(
            work,
            (-700010.0, -1050010.0, -700000.0, -1050000.0),
            resolution_m=1.0,
            cache_dir=surf_c,
        )
    pdal_mock.assert_not_called()
    assert (work / "dem" / "dem_filled.tif").is_file()
