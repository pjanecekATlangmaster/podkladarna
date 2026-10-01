from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.download_cache import (
    bbox_cache_key,
    is_fresh,
    lidar_sheet_dir,
    references_cache_dir,
    write_meta,
    zabaged_cache_dir,
)


def test_bbox_cache_key_stable():
    bbox = (14.4, 50.08, 14.42, 50.09)
    assert bbox_cache_key(bbox) == "14.4000_50.0800_14.4200_50.0900"


def test_is_fresh_respects_max_age(tmp_path: Path):
    folder = tmp_path / "sheet"
    folder.mkdir()
    artifact = folder / "DMR5G.laz"
    artifact.write_bytes(b"x" * 2000)
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    write_meta(folder, downloaded_at=old)
    assert not is_fresh(folder, artifact, max_age_days=180)
    write_meta(folder, downloaded_at=datetime.now(timezone.utc).isoformat())
    assert is_fresh(folder, artifact, max_age_days=180)


def test_lidar_sheet_dir_prefers_new_layout(tmp_path: Path, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    legacy = tmp_path / "sm5" / "PRAH77"
    legacy.mkdir(parents=True)
    assert lidar_sheet_dir("PRAH77") == legacy

    new = tmp_path / "lidar" / "sm5" / "PRAH78"
    new.mkdir(parents=True)
    assert lidar_sheet_dir("PRAH78") == new


def test_zabaged_cache_dir_includes_config_version(tmp_path: Path, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    cfg = tmp_path / "zabaged_ags.yaml"
    cfg.write_text("layers: {}\n", encoding="utf-8")
    bbox = (14.4, 50.08, 14.42, 50.09)
    path = zabaged_cache_dir(bbox, cfg)
    assert path.parent.name == "zabaged"
    assert path.name.startswith("14.4000_50.0800_14.4200_50.0900_")


def test_references_cache_dir_includes_sizes(tmp_path: Path, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    bbox = (14.4, 50.08, 14.42, 50.09)
    path = references_cache_dir(bbox, ref_wh=(2048, 1536), osm_wh=(4096, 3072))
    assert path.parent.name == "references"
    assert "r2048x1536" in path.name
    assert "o4096x3072" in path.name
    assert path.name.startswith("14.4000_50.0800_14.4200_50.0900_")


def test_aoi_cache_key_not_job_id():
    from app.download_cache import aoi_cache_key

    bbox = (14.4, 50.08, 14.42, 50.09)
    key = aoi_cache_key(bbox, resolution_m=1.0, sheet_ids=["prah77", "PRAH78"])
    assert "prah77" not in key  # normalized upper
    assert "PRAH77" in key and "PRAH78" in key
    assert key.startswith("14.4000_50.0800_14.4200_50.0900_r1")
    # Must not look like a 12-char job id alone.
    assert key != "abc123def456"
    assert len(key) > 12


def test_surfaces_cache_dir_uses_aoi_key(tmp_path: Path, monkeypatch):
    from app import settings
    from app.download_cache import aoi_cache_key, surfaces_cache_dir

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    bbox = (14.4, 50.08, 14.42, 50.09)
    path = surfaces_cache_dir(bbox, resolution_m=1.0, sheet_ids=["PRAH77"])
    assert path.parent.name == "surfaces"
    assert path.name == aoi_cache_key(bbox, resolution_m=1.0, sheet_ids=["PRAH77"])
    assert path.name != "job123"
    with_sf = surfaces_cache_dir(
        bbox, resolution_m=1.0, sheet_ids=["PRAH77"], scalefactor=0.75
    )
    assert "s0.75" in with_sf.name


def test_force_refresh_enabled_default_false(monkeypatch):
    from app.download_cache import force_refresh_enabled

    monkeypatch.setattr("app.settings.FORCE_REFRESH_DEFAULT", False)
    assert force_refresh_enabled({"contour_interval": 2.5}) is False
    assert force_refresh_enabled({"force_refresh": 1}) is True
def test_sheet_crop_bounds_key_stable():
    from app.download_cache import (
        bounds_contain,
        bounds_from_sheet_crop_key,
        sheet_crop_bounds_key,
    )

    bounds = (-660641.4480863165, -983272.7562890819, -659243.7255616218, -982243.5440805227)
    key = sheet_crop_bounds_key(bounds)
    assert key == sheet_crop_bounds_key(bounds)
    parsed = bounds_from_sheet_crop_key(key)
    assert parsed is not None
    assert bounds_contain(parsed, bounds, eps=0.002)
    assert bounds_contain(
        (-661000.0, -984000.0, -659000.0, -982000.0),
        bounds,
    )
    assert not bounds_contain(bounds, (-661000.0, -984000.0, -659000.0, -982000.0))


def test_sheet_crop_cache_exact_and_superset(tmp_path: Path, monkeypatch):
    from app import settings
    from app.download_cache import (
        SHEET_CROP_VEG,
        persist_sheet_crop,
        sheet_id_for_laz,
        try_lookup_sheet_crop,
    )

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    sheet = tmp_path / "lidar" / "sm5" / "VRCH31"
    sheet.mkdir(parents=True)
    src = sheet / "DMPOK.laz"
    src.write_bytes(b"S" * 5000)
    assert sheet_id_for_laz(src) == "VRCH31"

    outer = (0.0, 0.0, 100.0, 80.0)
    cropped = tmp_path / "outer.laz"
    cropped.write_bytes(b"C" * 4000)
    persist_sheet_crop(src, cropped, outer, SHEET_CROP_VEG)

    exact = try_lookup_sheet_crop(src, outer, SHEET_CROP_VEG)
    assert exact is not None
    assert exact[0] == "exact"

    inner = (10.0, 10.0, 50.0, 40.0)
    super_hit = try_lookup_sheet_crop(src, inner, SHEET_CROP_VEG)
    assert super_hit is not None
    assert super_hit[0] == "superset"
    assert super_hit[1].is_file()

    assert try_lookup_sheet_crop(src, inner, SHEET_CROP_VEG, force=True) is None

    # Jiny fingerprint zdroje -> miss
    src.write_bytes(b"T" * 6000)
    assert try_lookup_sheet_crop(src, outer, SHEET_CROP_VEG) is None
