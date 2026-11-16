"""Plný disk s daty (ostrá A 2026-11-15): stav jobu, kontrola místa, úklid cache, poškozené LAZ."""

from __future__ import annotations

import errno
import os
import sqlite3
import struct
import subprocess
import time
from pathlib import Path

import pytest

from app import db, disk_space


def _las_header(*, n_vlr: int = 1, vlr_len: int = 34, offset: int | None = None, laz: bool = True) -> bytes:
    """Minimální hlavička LAS 1.2 (227 B) + VLR; ``offset`` = začátek bodů."""
    header = bytearray(227)
    header[0:4] = b"LASF"
    header[24], header[25] = 1, 2
    struct.pack_into("<H", header, 94, 227)
    real = 227 + n_vlr * (54 + vlr_len)
    struct.pack_into("<I", header, 96, real if offset is None else offset)
    struct.pack_into("<I", header, 100, n_vlr)
    header[104] = (3 | 0x80) if laz else 3
    struct.pack_into("<H", header, 105, 34)
    struct.pack_into("<I", header, 107, 10)
    body = bytearray()
    for _ in range(n_vlr):
        vlr = bytearray(54)
        struct.pack_into("<H", vlr, 52, vlr_len)
        body += vlr + bytes(vlr_len)
    return bytes(header + body)


def _write_laz(path: Path, *, broken: bool = False) -> Path:
    head = _las_header(offset=227 + 20 if broken else None)  # broken: VLR „teče“ do bodů
    start = len(head) if not broken else 227 + 20
    data = bytearray(head) + bytes(2000)
    struct.pack_into("<q", data, start, len(data) - 16)  # tabulka chunků uvnitř souboru
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(data))
    return path


@pytest.mark.parametrize(
    ("exc", "full"),
    [
        (OSError(errno.ENOSPC, "No space left on device"), True),
        (sqlite3.OperationalError("database or disk is full"), True),
        (RuntimeError("ERROR 3: Attempt to write line to DXF file failed, disk full?"), True),
        (OSError(errno.ENOENT, "missing"), False),
        (RuntimeError("HTTP Error 503"), False),
    ],
)
def test_is_disk_full(exc, full):
    assert disk_space.is_disk_full(exc) is full


def test_failure_summary_disk():
    from app.pipeline.issue_explain import failure_summary

    assert "místo na disku" in failure_summary("[Errno 28] No space left on device")
    assert "disk space" in failure_summary(disk_space.DISK_FULL_MESSAGE, "en")


def test_laz_looks_truncated(tmp_path):
    from app.download_cache import _laz_usable, laz_looks_truncated

    ok = _write_laz(tmp_path / "ok.laz")
    bad = _write_laz(tmp_path / "bad.laz", broken=True)
    fake = tmp_path / "fake.laz"
    fake.write_bytes(b"x" * 2000)
    assert not laz_looks_truncated(ok) and _laz_usable(ok)
    assert laz_looks_truncated(bad) and not _laz_usable(bad)
    assert not laz_looks_truncated(fake)  # bez LASF se nehodnotí
    # useknutý konec: tabulka chunků za koncem souboru
    cut = tmp_path / "cut.laz"
    cut.write_bytes(ok.read_bytes()[:1200])
    assert laz_looks_truncated(cut)


def test_build_full_sheet_truncated_output_raises_and_is_not_cached(tmp_path, monkeypatch):
    import app.settings as settings
    from app.download_cache import full_sheet_crop_dir, lookup_full_sheet_crop
    from app.pipeline import prepare_lidar

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path / "cache")
    src = _write_laz(tmp_path / "cache" / "lidar" / "sm5" / "JHRA60" / "DMPOK.laz")

    def fake_run(cmd, log=None):
        _write_laz(Path(cmd[3]), broken=True)  # PDAL „uspěl“, ale na plném disku

    monkeypatch.setattr(prepare_lidar, "run_cmd", fake_run)
    monkeypatch.setattr(prepare_lidar, "free_bytes", lambda _p=None: 10)
    with pytest.raises(disk_space.DiskFullError):
        prepare_lidar._build_full_sheet("pdal", src, "veg_cls5_6", [], [], None)
    assert lookup_full_sheet_crop(src, "veg_cls5_6") is None
    assert not list(full_sheet_crop_dir(src, "veg_cls5_6").glob("*.laz"))


def test_corrupted_full_sheet_in_cache_is_dropped(tmp_path, monkeypatch):
    import app.settings as settings
    from app.download_cache import full_sheet_crop_dir, lookup_full_sheet_crop, persist_full_sheet_crop

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path / "cache")
    src = _write_laz(tmp_path / "cache" / "lidar" / "sm5" / "JHRA61" / "DMPOK.laz")
    built = _write_laz(tmp_path / "built.laz")
    cached = persist_full_sheet_crop(src, built, "veg_cls5_6")
    assert lookup_full_sheet_crop(src, "veg_cls5_6") == cached
    _write_laz(cached, broken=True)  # list uložený při plném disku (starší verze)
    assert lookup_full_sheet_crop(src, "veg_cls5_6") is None
    assert not (full_sheet_crop_dir(src, "veg_cls5_6") / "cropped.laz").exists()


def _isolated_job(tmp_path, monkeypatch):
    from app import job_worker

    monkeypatch.setattr("app.db.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("app.db.DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr(job_worker, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(job_worker, "ensure_proj_data", lambda: None)
    (tmp_path / "jobs").mkdir()
    db.init_db()
    job = db.create_job("Lodherov", "forest_10000", {})
    # log.txt jako složka: zápis logu selže stejně jako na plném disku
    (tmp_path / "jobs" / job["id"] / "log.txt").mkdir(parents=True)
    return job_worker, job["id"]


def test_run_job_disk_full_marks_failed_even_if_log_write_fails(tmp_path, monkeypatch):
    job_worker, job_id = _isolated_job(tmp_path, monkeypatch)

    def boom(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(job_worker, "run_job_pipeline", boom)
    assert job_worker.run_job(job_id) == 1
    got = db.get_job(job_id)
    assert got["status"] == "failed"
    assert "místo na disku" in (got["error"] or "")


def test_status_write_retries_after_releasing_reserve(tmp_path, monkeypatch):
    import app.settings as settings

    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path)
    disk_space.reserve_path().write_bytes(b"\0" * 10)
    calls = []

    def write():
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database or disk is full")
        return "ok"

    assert disk_space.write_with_reserve(write) == "ok"
    assert len(calls) == 2 and not disk_space.reserve_path().exists()


def test_worker_marks_crashed_job_failed_on_full_disk(tmp_path, monkeypatch):
    job_worker, job_id = _isolated_job(tmp_path, monkeypatch)
    from app import worker

    db.update_job(job_id, status="running", phase="prepare")
    monkeypatch.setattr(worker.disk_space, "free_bytes", lambda _p=None: 1000)
    monkeypatch.setattr(worker, "_record_failed_usage", lambda _j: None)
    worker._fail_running_job(job_id, "Pipeline skončila s kódem 1")
    got = db.get_job(job_id)
    assert got["status"] == "failed"
    assert "místo na disku" in (got["error"] or "")


def _aoi_entry(root: Path, name: str, size: int, age_days: float) -> Path:
    entry = root / name
    entry.mkdir(parents=True)
    (entry / "a.png").write_bytes(b"x" * size)
    old = time.time() - age_days * 86400
    for p in (entry / "a.png", entry):
        os.utime(p, (old, old))
    return entry


@pytest.mark.parametrize(
    ("folder", "setting", "days", "osm_hours", "old_days", "fresh_days"),
    [
        ("references", "REF_CACHE_MAX_AGE_DAYS", 30, "24", 40, 5),
        # Odpovědi Overpass (revize 2026-11-16): po 2 dnech, nejméně platnost cache + 1 den.
        ("osm", "OSM_CACHE_MAX_AGE_DAYS", 2, "24", 3, 1),
        ("osm", "OSM_CACHE_MAX_AGE_DAYS", 2, "72", 5, 3),
    ],
)
def test_prune_expired_aoi_cache_keeps_valid_entries(
    tmp_path, monkeypatch, folder, setting, days, osm_hours, old_days, fresh_days
):
    import app.settings as settings

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    monkeypatch.setattr(settings, setting, days)
    monkeypatch.setenv("PODKLADARNA_OSM_CACHE_HOURS", osm_hours)
    old = _aoi_entry(tmp_path / folder, "old", 100, old_days)
    fresh = _aoi_entry(tmp_path / folder, "fresh", 100, fresh_days)
    assert disk_space.prune_expired_aoi_cache() == 100
    assert not old.exists() and fresh.exists()


def test_prune_tile_cache_age_cap_and_old_versions(tmp_path, monkeypatch):
    """Cache dlaždic webu (revize 2026-11-16): stará verze pryč, starší než limit pryč, nad stropem nejstarší."""
    import app.settings as settings
    import app.tiles as tiles

    monkeypatch.setattr(tiles, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(settings, "TILE_CACHE_MAX_AGE_DAYS", 30)
    monkeypatch.setattr(settings, "TILE_CACHE_MAX_GB", 250e-9)  # 250 B
    _aoi_entry(tmp_path / "tiles" / "v1" / "5", "17", 100, 1)
    ages = {"old": 40, "mid": 10, "young": 5, "new": 1}
    paths = {name: _aoi_entry(tmp_path / "tiles" / "v2" / "12", name, 100, age) for name, age in ages.items()}
    assert tiles.prune_tile_cache(now=time.time() + 7200) == 300  # v1, old (stáří), mid (strop)
    assert not (tmp_path / "tiles" / "v1").exists()
    assert {n for n, p in paths.items() if (p / "a.png").exists()} == {"young", "new"}
    assert not paths["old"].exists()  # prázdná složka x se smaže


def test_ensure_job_space_prunes_oldest_then_fails(tmp_path, monkeypatch):
    import app.settings as settings

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    monkeypatch.setattr(settings, "DISK_JOB_BASE_GB", 1.0)
    monkeypatch.setattr(settings, "DISK_JOB_GB_PER_KM2", 0.0)
    older = _aoi_entry(tmp_path / "surfaces", "older", 10, 3)
    newer = _aoi_entry(tmp_path / "references", "newer", 10, 1)
    state = {"free": int(0.3e9)}

    def fake_free(_p=None):
        # každá smazaná položka „uvolní“ 0,3 GB
        gone = sum(not p.exists() for p in (older, newer))
        return state["free"] + gone * int(0.3e9)

    monkeypatch.setattr(disk_space, "free_bytes", fake_free)
    monkeypatch.setattr(disk_space, "same_disk", lambda _a, _b: True)
    with pytest.raises(disk_space.DiskFullError):
        disk_space.ensure_job_space(5.0)
    assert not older.exists() and not newer.exists()

    state["free"] = int(0.8e9)  # po smazání první (nejstarší) položky stačí
    older = _aoi_entry(tmp_path / "surfaces", "older", 10, 3)
    newer = _aoi_entry(tmp_path / "references", "newer", 10, 1)
    disk_space.ensure_job_space(5.0)
    assert not older.exists() and newer.exists()


@pytest.mark.parametrize(
    "data_free_gb, cache_free_gb, fails, cache_pruned",
    [
        (0.3, 100.0, True, False),  # málo místa pro joby: cache na jiném disku nemazat, rovnou chyba
        (100.0, 0.3, False, True),  # málo místa v cache: smazat starou cache na jejím disku, job projde
        (100.0, 100.0, False, False),
    ],
)
def test_ensure_job_space_cache_on_other_disk(tmp_path, monkeypatch, data_free_gb, cache_free_gb, fails, cache_pruned):
    import app.settings as settings

    data = tmp_path / "data"
    cache = tmp_path / "cache"
    data.mkdir()
    monkeypatch.setattr(settings, "DATA_ROOT", data)
    monkeypatch.setattr(settings, "DOWNLOADS_DIR", cache)
    monkeypatch.setattr(settings, "DISK_JOB_BASE_GB", 1.0)
    monkeypatch.setattr(settings, "DISK_JOB_GB_PER_KM2", 0.0)
    entry = _aoi_entry(cache / "references", "old", 10, 3)
    monkeypatch.setattr(disk_space, "same_disk", lambda a, b: Path(a) == Path(b))

    def fake_free(p=None):
        if Path(p or data) == cache:
            return int(cache_free_gb * 1e9) + (0 if entry.exists() else int(1e9))
        return int(data_free_gb * 1e9)

    monkeypatch.setattr(disk_space, "free_bytes", fake_free)
    if fails:
        with pytest.raises(disk_space.DiskFullError):
            disk_space.ensure_job_space(1.0)
    else:
        disk_space.ensure_job_space(1.0)
    assert entry.exists() is not cache_pruned


def test_same_disk(tmp_path):
    assert disk_space.same_disk(tmp_path, tmp_path / "neexistuje" / "cache")


def test_lidar_cache_cap_limited_by_disk_share(tmp_path, monkeypatch):
    import app.settings as settings

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    monkeypatch.setattr(settings, "LIDAR_CACHE_MAX_GB", 250.0)
    monkeypatch.setattr(settings, "LIDAR_CACHE_MAX_DISK_SHARE", 0.5)
    monkeypatch.setattr(disk_space, "total_bytes", lambda _p=None: int(100e9))
    assert disk_space.lidar_cache_max_bytes() == int(50e9)
    monkeypatch.setattr(settings, "LIDAR_CACHE_MAX_GB", 20.0)
    assert disk_space.lidar_cache_max_bytes() == int(20e9)
    monkeypatch.setattr(settings, "LIDAR_CACHE_MAX_GB", 0.0)
    assert disk_space.lidar_cache_max_bytes() == int(50e9)
