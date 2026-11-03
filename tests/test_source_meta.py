"""Unit tests for LiDAR source / epoch metadata helpers."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.download_cache import write_meta
from app.pipeline.source_meta import (
    INDICATIVE_LABEL_CS,
    citation_line,
    collect_lidar_source_meta,
    dmp_acquired_on,
    format_source_epochs_readme,
    is_leafless_date,
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


@pytest.mark.parametrize(
    "iso,expected",
    [
        ("2024-04-27", True),
        ("2024-05-14", True),
        ("2024-05-15", False),
        ("2024-07-06", False),
        ("2025-11-03", True),
        (None, False),
    ],
)
def test_is_leafless_date(iso, expected):
    assert is_leafless_date(iso) is expected


def test_dmp_acquired_on_reads_gps_time(tmp_path: Path):
    laspy = pytest.importorskip("laspy")
    import datetime as dt

    header = laspy.LasHeader(point_format=6, version="1.4")
    header.global_encoding.gps_time_type = laspy.header.GpsTimeType.STANDARD
    las = laspy.LasData(header)
    n = 20
    las.x = np.arange(n, dtype=float)
    las.y = np.arange(n, dtype=float)
    las.z = np.zeros(n)
    flown = dt.datetime(2024, 4, 27, 12, tzinfo=dt.timezone.utc)
    gps0 = dt.datetime(1980, 1, 6, tzinfo=dt.timezone.utc)
    las.gps_time = np.full(n, (flown - gps0).total_seconds() - 1e9)
    path = tmp_path / "DMPOK.las"
    las.write(str(path))
    assert dmp_acquired_on(path) == "2024-04-27"

    bad = tmp_path / "bad.laz"
    bad.write_bytes(b"x" * 2000)
    assert dmp_acquired_on(bad) is None


def test_collect_and_readme_leafless(tmp_path: Path, monkeypatch):
    from app import settings
    from app.pipeline import source_meta

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    for name in ("PRAH77", "PRAH78"):
        folder = tmp_path / "lidar" / "sm5" / name
        folder.mkdir(parents=True)
        (folder / "DMR5G.laz").write_bytes(b"d" * 2000)
        (folder / "DMPOK.laz").write_bytes(b"x" * 2000)
        write_meta(folder, downloaded_at="2026-03-10T08:00:00+00:00")
    monkeypatch.setattr(
        source_meta,
        "dmp_acquired_on",
        lambda p: "2024-04-27" if "PRAH77" in str(p) else "2024-07-06",
    )
    meta = collect_lidar_source_meta(["PRAH77", "PRAH78"])
    assert meta["dmp_leafless"] is True
    assert meta["dmp_leafless_sheets"] == [
        {"mapnom": "PRAH77", "acquired_on": "2024-04-27"}
    ]
    block = format_source_epochs_readme(meta)
    assert "(nálet 2024-04-27)" in block
    assert "VAROVÁNÍ" in block and "listy PRAH77" in block
