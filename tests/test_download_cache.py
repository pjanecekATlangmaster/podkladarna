from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.download_cache import (
    bbox_cache_key,
    is_fresh,
    lidar_sheet_dir,
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
