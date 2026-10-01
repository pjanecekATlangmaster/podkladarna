from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app import settings

META_FILENAME = "meta.json"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_meta(folder: Path) -> dict | None:
    meta_path = folder / META_FILENAME
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_meta(folder: Path, **fields) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    meta = read_meta(folder) or {}
    meta.update(fields)
    meta.setdefault("downloaded_at", utcnow_iso())
    (folder / META_FILENAME).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def age_days(downloaded_at: str | None) -> float | None:
    if not downloaded_at:
        return None
    try:
        dt = datetime.fromisoformat(downloaded_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def is_fresh(
    folder: Path,
    artifact: Path,
    max_age_days: int,
    *,
    min_size: int = 1000,
) -> bool:
    if not artifact.is_file() or artifact.stat().st_size < min_size:
        return False
    if max_age_days <= 0:
        return True
    meta = read_meta(folder)
    if not meta:
        return True
    age = age_days(meta.get("downloaded_at"))
    if age is None:
        return True
    return age <= max_age_days


def bbox_cache_key(bbox: tuple[float, float, float, float], precision: int = 4) -> str:
    west, south, east, north = bbox
    return "_".join(f"{v:.{precision}f}" for v in (west, south, east, north))


def aoi_cache_key(
    bbox: tuple[float, float, float, float],
    *,
    resolution_m: float | None = None,
    sheet_ids: list[str] | tuple[str, ...] | None = None,
    precision: int = 4,
) -> str:
    """Klíč cache podle AOI (+ volitelně buňka / listy SM5) – ne podle job ID.

    Seam §10: stejný výřez sdílí underlaye/meziprodukty napříč joby.
    Plná invalidace podle ekvidistance/laviček sem ještě nepatří.
    """
    parts = [bbox_cache_key(bbox, precision=precision)]
    if resolution_m is not None:
        parts.append(f"r{float(resolution_m):g}")
    if sheet_ids:
        sheets = "_".join(sorted({s.strip().upper() for s in sheet_ids if s and s.strip()}))
        if sheets:
            parts.append(sheets)
    return "_".join(parts)


def surfaces_cache_dir(
    bbox: tuple[float, float, float, float],
    *,
    resolution_m: float = 1.0,
    sheet_ids: list[str] | tuple[str, ...] | None = None,
) -> Path:
    """Sdílená cache DEM/DSM/CHM meziproduktů podle AOI (ne job id)."""
    key = aoi_cache_key(bbox, resolution_m=resolution_m, sheet_ids=sheet_ids)
    return settings.DOWNLOADS_DIR / "surfaces" / key


def config_version(path: Path) -> str:
    if not path.is_file():
        return "none"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def lidar_sheet_dir(mapnom: str) -> Path:
    root = settings.DOWNLOADS_DIR
    primary = root / "lidar" / "sm5" / mapnom
    legacy = root / "sm5" / mapnom
    if legacy.is_dir() and not primary.is_dir():
        return legacy
    return primary


def zabaged_cache_dir(bbox: tuple[float, float, float, float], config_path: Path) -> Path:
    key = bbox_cache_key(bbox)
    ver = config_version(config_path)
    return settings.DOWNLOADS_DIR / "zabaged" / f"{key}_{ver}"


def references_cache_dir(
    bbox: tuple[float, float, float, float],
    *,
    ref_wh: tuple[int, int],
    osm_wh: tuple[int, int],
) -> Path:
    """Sdílená cache referenčních PNG (orto, OSM, ZTM, …) podle výřezu a rozlišení."""
    key = bbox_cache_key(bbox)
    rw, rh = ref_wh
    ow, oh = osm_wh
    return (
        settings.DOWNLOADS_DIR
        / "references"
        / f"{key}_r{rw}x{rh}_o{ow}x{oh}"
    )
