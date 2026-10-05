"""Unit tests for LiDAR source / epoch metadata helpers."""

from __future__ import annotations

from pathlib import Path

from app.download_cache import write_meta
from app.pipeline.source_meta import (
    INDICATIVE_LABEL_CS,
    citation_line,
    collect_lidar_source_meta,
    format_source_epochs_readme,
    resolve_dmp_product,
)


def test_citation_line_has_no_karttapullautin():
    line = citation_line()
    assert "Karttapullautin" not in line
    assert "Podkladárna" in line
    assert INDICATIVE_LABEL_CS in line


def test_resolve_dmp_product_prefers_ok(tmp_path: Path, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    folder = tmp_path / "lidar" / "sm5" / "PRAH77"
    folder.mkdir(parents=True)
    (folder / "DMPOK.laz").write_bytes(b"x" * 2000)
    (folder / "DMP1G.laz").write_bytes(b"y" * 2000)
    write_meta(folder, downloaded_at="2026-01-15T12:00:00+00:00", kind="DMPOK")
    info = resolve_dmp_product(folder)
    assert info["mode"] == "ok"
    assert info["degraded"] is False
    assert info["product"] == "DMPOK"


def test_resolve_dmp_product_1g_degraded(tmp_path: Path, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    folder = tmp_path / "lidar" / "sm5" / "PRAH77"
    folder.mkdir(parents=True)
    (folder / "DMP1G.laz").write_bytes(b"y" * 2000)
    write_meta(folder, downloaded_at="2026-02-01T00:00:00+00:00")
    info = resolve_dmp_product(folder)
    assert info["mode"] == "1g"
    assert info["degraded"] is True


def test_collect_and_format_epochs(tmp_path: Path, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    folder = tmp_path / "lidar" / "sm5" / "PRAH77"
    folder.mkdir(parents=True)
    (folder / "DMR5G.laz").write_bytes(b"d" * 2000)
    (folder / "DMP1G.laz").write_bytes(b"y" * 2000)
    write_meta(folder, downloaded_at="2026-03-10T08:00:00+00:00")

    meta = collect_lidar_source_meta(["prah77"])
    assert meta["sheet_count"] == 1
    assert meta["dmp_mode"] == "1g"
    assert meta["dmp_degraded"] is True
    assert meta["sheets"][0]["mapnom"] == "PRAH77"

    block = format_source_epochs_readme(meta)
    assert "PRAH77" in block
    assert "DMP 1G" in block
    assert "VAROVÁNÍ" in block
    assert INDICATIVE_LABEL_CS in block
