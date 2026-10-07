from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import settings

META_FILENAME = "meta.json"

# DEM/DSM/CHM artefakty persistované pod surfaces_cache_dir.
SURFACE_ARTIFACT_NAMES = (
    "dem_raw.tif",
    "dem_filled.tif",
    "dsm_raw.tif",
    "dsm_filled.tif",
    "chm.tif",
    "dem_meta.json",
)
# Změna výpočtu DSM/CHM → starší AOI surfaces cache se nesmí obnovit.
SURFACES_RECIPE = "dsm-on-dem-grid-chm0-v2"
# Ořez/merge LAZ pro AOI (nezávislé na ekvidistance / lavičkách).
# Dvojice ground + veg; jeden list = ořez listu bez merge (dmr_ground_0 / dmp_veg_0).
LIDAR_CROP_ARTIFACT_NAMES = (
    "ground_merged.laz",
    "veg_merged.laz",
)
# Pozůstatek KP (vstup pullauta) – už se nevytváří; legacy reuse ho ještě čte.
LEGACY_MERGED_LAZ_NAMES = (
    "merged_crop.laz",
    "merged_crop_retry.laz",
    "merged.laz",
)
LIDAR_GROUND_MERGED = "ground_merged.laz"
LIDAR_VEG_MERGED = "veg_merged.laz"
LIDAR_MIN_BYTES = 1000
SHADE_ARTIFACT_NAMES = ("hillshade.png", "hillshade.pgw")


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


def force_refresh_enabled(options: dict[str, Any] | None = None) -> bool:
    """Escape hatch: options.force_refresh nebo PODKLADARNA_FORCE_REFRESH.

    Default = reuse (False). Parametry jako ekvidistance/lavičky force neznamenají.
    """
    if options and options.get("force_refresh"):
        return True
    return bool(getattr(settings, "FORCE_REFRESH_DEFAULT", False))


def file_fingerprint(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    st = path.stat()
    return {
        "name": path.name,
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
    }


def fingerprints_equal(a: dict | None, b: dict | None) -> bool:
    if a is None and b is None:
        return True
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    # size+name stačí na invalidaci při výměně LAZ; mtime je nestabilní
    # po link_or_copy na některých FS.
    return a.get("name") == b.get("name") and a.get("size") == b.get("size")


def bbox_cache_key(bbox: tuple[float, float, float, float], precision: int = 4) -> str:
    west, south, east, north = bbox
    return "_".join(f"{v:.{precision}f}" for v in (west, south, east, north))


def aoi_cache_key(
    bbox: tuple[float, float, float, float],
    *,
    resolution_m: float | None = None,
    sheet_ids: list[str] | tuple[str, ...] | None = None,
    scalefactor: float | None = None,
    precision: int = 4,
) -> str:
    """Klíč cache podle AOI (+ mřížka / listy / scalefactor) – ne podle job ID.

    Seam §10: stejný výřez sdílí underlaye/meziprodukty napříč joby.
    Ekvidistance / lavičky / cliff symbol klíč nemění — invalidují jen
    závislé větve (kontury, OSM, package).
    """
    parts = [bbox_cache_key(bbox, precision=precision)]
    if resolution_m is not None:
        parts.append(f"r{float(resolution_m):g}")
    if scalefactor is not None:
        parts.append(f"s{float(scalefactor):g}")
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
    scalefactor: float | None = None,
) -> Path:
    """Sdílená cache DEM/DSM/CHM (+ shade) podle AOI (ne job id)."""
    key = aoi_cache_key(
        bbox,
        resolution_m=resolution_m,
        sheet_ids=sheet_ids,
        scalefactor=scalefactor,
    )
    return settings.DOWNLOADS_DIR / "surfaces" / key


def lidar_crop_cache_dir(
    bbox: tuple[float, float, float, float],
    *,
    scalefactor: float = 1.0,
    sheet_ids: list[str] | tuple[str, ...] | None = None,
) -> Path:
    """Sdílená cache ořezaného/sloučeného LAZ podle AOI (+ KP pad ze scalefactor)."""
    key = aoi_cache_key(bbox, scalefactor=scalefactor, sheet_ids=sheet_ids)
    return settings.DOWNLOADS_DIR / "lidar_crop" / key


# Per-list PDAL crop (DMR ground / DMP vegetace) – stejný list+bounds+filtr → bez PDAL.
SHEET_CROP_GROUND = "ground_cls2"
SHEET_CROP_VEG = "veg_cls5_6"
SHEET_CROP_ARTIFACT = "cropped.laz"
# Prázdný ořez může být < 1 KB; použitelný sheet crop bereme stejně jako prepare_lidar.
MIN_LAZ_BYTES_SHEET = 1000


def sheet_id_for_laz(src: Path) -> str:
    """SM5 mapnom z cesty cache (…/sm5/VRCH31/DMPOK.laz), jinak stem souboru."""
    parent = src.parent.name.strip().upper()
    if parent and parent not in {"SM5", "LIDAR", "CACHE", "DATA"}:
        return parent
    return src.stem.strip().upper() or "UNKNOWN"


def sheet_crop_bounds_key(bounds: tuple[float, float, float, float]) -> str:
    """Stabilní klíč ořezu (mm) – musí sedět s ``bounds_from_sheet_crop_key``."""
    return "_".join(f"{float(v):.3f}" for v in bounds)


def bounds_from_sheet_crop_key(key: str) -> tuple[float, float, float, float] | None:
    parts = key.split("_")
    if len(parts) != 4:
        return None
    try:
        return tuple(float(p) for p in parts)  # type: ignore[return-value]
    except ValueError:
        return None


def bounds_contain(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    *,
    eps: float = 1e-3,
) -> bool:
    """True, když outer pokrývá inner (kvalitně bezpečný subset ořez)."""
    return (
        outer[0] <= inner[0] + eps
        and outer[1] <= inner[1] + eps
        and outer[2] >= inner[2] - eps
        and outer[3] >= inner[3] - eps
    )


def sheet_crop_recipe_dir(sheet_id: str, recipe: str) -> Path:
    safe_sheet = "".join(c if c.isalnum() or c in "-_" else "_" for c in sheet_id.upper())
    safe_recipe = "".join(c if c.isalnum() or c in "-_" else "_" for c in recipe)
    return settings.DOWNLOADS_DIR / "lidar_sheet_crop" / safe_sheet / safe_recipe


def sheet_crop_cache_dir(
    sheet_id: str,
    recipe: str,
    bounds: tuple[float, float, float, float],
) -> Path:
    return sheet_crop_recipe_dir(sheet_id, recipe) / sheet_crop_bounds_key(bounds)


def try_lookup_sheet_crop(
    src: Path,
    bounds: tuple[float, float, float, float],
    recipe: str,
    *,
    force: bool = False,
    max_age_days: int | None = None,
) -> tuple[str, Path, tuple[float, float, float, float]] | None:
    """Najde cache ořezu listu.

    Vrací ``(kind, path, cached_bounds)`` kde kind je ``exact`` nebo ``superset``.
    ``superset`` = větší dřívější ořez obsahuje požadovaný bbox → PDAL jen z malého LAZ.
    """
    if force or bounds is None:
        return None
    age_limit = (
        settings.LIDAR_CACHE_MAX_AGE_DAYS if max_age_days is None else max_age_days
    )
    src_fp = file_fingerprint(src)
    sheet = sheet_id_for_laz(src)
    recipe_root = sheet_crop_recipe_dir(sheet, recipe)
    if not recipe_root.is_dir():
        return None

    exact_dir = sheet_crop_cache_dir(sheet, recipe, bounds)
    exact_laz = exact_dir / SHEET_CROP_ARTIFACT
    if is_fresh(exact_dir, exact_laz, age_limit, min_size=MIN_LAZ_BYTES_SHEET):
        meta = read_meta(exact_dir) or {}
        if fingerprints_equal(meta.get("src_fp"), src_fp):
            return "exact", exact_laz, bounds

    # Nejmenší nadmnožina (méně bodů k dořezání).
    best: tuple[float, Path, tuple[float, float, float, float]] | None = None
    for child in recipe_root.iterdir():
        if not child.is_dir():
            continue
        laz = child / SHEET_CROP_ARTIFACT
        if not is_fresh(child, laz, age_limit, min_size=MIN_LAZ_BYTES_SHEET):
            continue
        meta = read_meta(child) or {}
        if not fingerprints_equal(meta.get("src_fp"), src_fp):
            continue
        cached_bounds = meta.get("bounds")
        if cached_bounds is None:
            cached_bounds = bounds_from_sheet_crop_key(child.name)
        try:
            cb = tuple(float(x) for x in cached_bounds)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if len(cb) != 4 or not bounds_contain(cb, bounds):
            continue
        area = max(0.0, (cb[2] - cb[0]) * (cb[3] - cb[1]))
        if best is None or area < best[0]:
            best = (area, laz, cb)  # type: ignore[assignment]
    if best is None:
        return None
    return "superset", best[1], best[2]


def persist_sheet_crop(
    src: Path,
    cropped: Path,
    bounds: tuple[float, float, float, float],
    recipe: str,
    *,
    log=None,
) -> Path | None:
    """Uloží ořez listu do sdílené cache (stejný src+bounds+filtr)."""
    if not cropped.is_file() or cropped.stat().st_size < MIN_LAZ_BYTES_SHEET:
        return None
    sheet = sheet_id_for_laz(src)
    cache_dir = sheet_crop_cache_dir(sheet, recipe, bounds)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / SHEET_CROP_ARTIFACT
    link_or_copy(cropped, dest)
    write_meta(
        cache_dir,
        kind="lidar_sheet_crop",
        recipe=recipe,
        sheet_id=sheet,
        bounds=list(bounds),
        src_fp=file_fingerprint(src),
        crop_fp=file_fingerprint(dest),
        source_name=src.name,
    )
    if log:
        log(
            f"Cache ořezu listu {sheet}/{recipe} uložena "
            f"({dest.stat().st_size / 1e6:.1f} MB) → {cache_dir.name}"
        )
    return dest


def shade_cache_dir(surfaces_dir: Path) -> Path:
    return Path(surfaces_dir) / "shade"


def _copy_named_artifacts(
    src_dir: Path,
    dst_dir: Path,
    names: tuple[str, ...],
    *,
    required: tuple[str, ...] = (),
) -> list[str]:
    dst_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in names:
        src = src_dir / name
        if not src.is_file():
            if name in required:
                raise FileNotFoundError(f"Chybí povinný artefakt {name} v {src_dir}")
            continue
        link_or_copy(src, dst_dir / name)
        copied.append(name)
    return copied


def _laz_usable(path: Path) -> bool:
    """Shodný práh jako ``prepare_lidar.MIN_LAZ_BYTES`` (prázdný ořez < 1 KB)."""
    try:
        return path.is_file() and path.stat().st_size >= LIDAR_MIN_BYTES
    except OSError:
        return False


def _single_usable(lidar_dir: Path, pattern: str) -> Path | None:
    usable = [p for p in sorted(lidar_dir.glob(pattern)) if _laz_usable(p)]
    return usable[0] if len(usable) == 1 else None


def lidar_pair_paths(lidar_dir: Path) -> tuple[Path, Path] | None:
    """Dvojice (ground, veg) LAZ jobu, jak ji nechá ``merge_dmr_dmp``.

    Víc listů → ``ground_merged`` / ``veg_merged``; jediný použitelný list →
    jeho ořez (``dmr_ground_N`` / ``dmp_veg_N``) bez kopie. Jména zůstávají
    stejná jako dřív, ať sedí fingerprinty AOI surfaces cache.
    """
    lidar_dir = Path(lidar_dir)
    ground = lidar_dir / LIDAR_GROUND_MERGED
    if not _laz_usable(ground):
        ground = _single_usable(lidar_dir, "dmr_ground_*.laz")
    veg = lidar_dir / LIDAR_VEG_MERGED
    if not _laz_usable(veg):
        veg = _single_usable(lidar_dir, "dmp_veg_*.laz")
    if ground is None or veg is None:
        return None
    return ground, veg


def try_restore_lidar_crop(
    cache_dir: Path,
    lidar_work: Path,
    *,
    max_age_days: int | None = None,
    force: bool = False,
    log=None,
) -> Path | None:
    """Obnoví dvojici ground + veg z AOI cache → work/lidar. Vrací ground; None = miss.

    Starší cache (bez ``pair`` v meta) mají ``ground_merged`` + ``veg_merged``;
    jejich ``merged_crop.laz`` se už nekopíruje.
    """
    if force:
        return None
    age_limit = (
        settings.LIDAR_CACHE_MAX_AGE_DAYS if max_age_days is None else max_age_days
    )
    meta = read_meta(cache_dir) or {}
    pair = meta.get("pair") or [LIDAR_GROUND_MERGED, LIDAR_VEG_MERGED]
    if not isinstance(pair, list) or len(pair) != 2:
        return None
    ground_name, veg_name = (str(n) for n in pair)
    if not is_fresh(cache_dir, cache_dir / ground_name, age_limit, min_size=LIDAR_MIN_BYTES):
        return None
    if not _laz_usable(cache_dir / veg_name):
        return None
    _copy_named_artifacts(
        cache_dir,
        lidar_work,
        (ground_name, veg_name),
        required=(ground_name, veg_name),
    )
    # KP merged_crop ze starší cache už nic nečte – uvolni místo.
    for name in LEGACY_MERGED_LAZ_NAMES:
        (cache_dir / name).unlink(missing_ok=True)
    if log:
        age = age_days(meta.get("downloaded_at"))
        age_s = f", stáří {age:.1f} d" if age is not None else ""
        log(
            "AOI cache zásah (ořezaný LAZ) – přeskakuji PDAL ořez/třídění"
            f"{age_s} ← {cache_dir.name}"
        )
    return lidar_work / ground_name


def persist_lidar_crop(
    cache_dir: Path,
    lidar_work: Path,
    *,
    bbox_wgs84: tuple[float, float, float, float] | None = None,
    sheet_ids: list[str] | None = None,
    scalefactor: float | None = None,
    log=None,
) -> None:
    pair = lidar_pair_paths(lidar_work)
    if pair is None:
        return
    ground, veg = pair
    copied = _copy_named_artifacts(lidar_work, cache_dir, (ground.name, veg.name))
    # Uvolni místo po KP merged_crop ze starší cache stejné AOI.
    for name in LEGACY_MERGED_LAZ_NAMES:
        (cache_dir / name).unlink(missing_ok=True)
    write_meta(
        cache_dir,
        kind="lidar_crop",
        bbox_wgs84=list(bbox_wgs84) if bbox_wgs84 else None,
        sheet_ids=list(sheet_ids or []),
        scalefactor=scalefactor,
        files=copied,
        pair=[ground.name, veg.name],
        ground_fp=file_fingerprint(ground),
        veg_fp=file_fingerprint(veg),
        merged_fp=None,
    )
    if log:
        log(f"AOI lidar crop uložen → {cache_dir.name} ({len(copied)} souborů)")


def try_restore_surfaces(
    cache_dir: Path,
    dem_dir: Path,
    *,
    bounds: tuple[float, float, float, float],
    resolution_m: float,
    ground_fp: dict | None,
    surface_fp: dict | None,
    max_age_days: int | None = None,
    force: bool = False,
    log=None,
) -> bool:
    """Obnoví dem_filled (+ DSM/CHM) z AOI cache. False = miss / invalidate."""
    if force:
        return False
    age_limit = (
        settings.SURFACES_CACHE_MAX_AGE_DAYS
        if max_age_days is None
        else max_age_days
    )
    dem_filled = cache_dir / "dem_filled.tif"
    if not is_fresh(cache_dir, dem_filled, age_limit, min_size=500):
        return False
    meta = read_meta(cache_dir) or {}
    if meta.get("kind") not in (None, "surfaces"):
        # starší meta bez kind ještě bereme, pokud sedí fingerprinty
        pass
    cached_bounds = meta.get("bounds")
    if cached_bounds is not None:
        try:
            cb = tuple(float(x) for x in cached_bounds)
            if len(cb) != 4 or any(abs(a - b) > 1e-3 for a, b in zip(cb, bounds)):
                return False
        except (TypeError, ValueError):
            return False
    if meta.get("resolution_m") is not None:
        try:
            if abs(float(meta["resolution_m"]) - float(resolution_m)) > 1e-9:
                return False
        except (TypeError, ValueError):
            return False
    if not fingerprints_equal(meta.get("ground_fp"), ground_fp):
        return False
    if not fingerprints_equal(meta.get("surface_fp"), surface_fp):
        return False
    if meta.get("recipe") != SURFACES_RECIPE:
        return False
    _copy_named_artifacts(
        cache_dir,
        dem_dir,
        SURFACE_ARTIFACT_NAMES,
        required=("dem_filled.tif",),
    )
    if log:
        age = age_days(meta.get("downloaded_at"))
        age_s = f", stáří {age:.1f} d" if age is not None else ""
        log(f"AOI surfaces cache hit{age_s} ← {cache_dir.name}")
    return True


def persist_surfaces(
    cache_dir: Path,
    dem_dir: Path,
    *,
    bounds: tuple[float, float, float, float],
    resolution_m: float,
    ground_fp: dict | None,
    surface_fp: dict | None,
    log=None,
) -> None:
    dem_filled = dem_dir / "dem_filled.tif"
    if not dem_filled.is_file() or dem_filled.stat().st_size < 500:
        return
    copied = _copy_named_artifacts(dem_dir, cache_dir, SURFACE_ARTIFACT_NAMES)
    write_meta(
        cache_dir,
        kind="surfaces",
        recipe=SURFACES_RECIPE,
        bounds=list(bounds),
        resolution_m=float(resolution_m),
        ground_fp=ground_fp,
        surface_fp=surface_fp,
        files=copied,
        dem_fp=file_fingerprint(dem_filled),
    )
    if log:
        log(f"AOI surfaces uloženy → {cache_dir.name} ({len(copied)} souborů)")


def try_restore_shade(
    cache_dir: Path,
    shade_dir: Path,
    *,
    dem_fp: dict | None = None,
    max_age_days: int | None = None,
    force: bool = False,
    log=None,
) -> bool:
    """Obnoví hillshade.png z AOI cache. False = miss.

    ČÚZK WMS shade je vázaný na AOI (stejný surfaces_cache_dir), ne na DEM.
    ``dem_fp`` se kontroluje jen u staršího / lokálního ``source=gdaldem``.
    """
    if force:
        return False
    shade_root = shade_cache_dir(cache_dir)
    png = shade_root / "hillshade.png"
    age_limit = (
        settings.SURFACES_CACHE_MAX_AGE_DAYS
        if max_age_days is None
        else max_age_days
    )
    if not is_fresh(shade_root, png, age_limit, min_size=64):
        return False
    meta = read_meta(shade_root) or {}
    source = str(meta.get("source") or "")
    # Legacy cache bez ``source``: když meta má dem_fp, chovej se jako gdaldem.
    if source == "gdaldem" or (not source and meta.get("dem_fp") is not None):
        if not fingerprints_equal(meta.get("dem_fp"), dem_fp):
            return False
    pgw = shade_root / "hillshade.pgw"
    if not pgw.is_file():
        return False
    _copy_named_artifacts(
        shade_root,
        shade_dir,
        SHADE_ARTIFACT_NAMES,
        required=("hillshade.png", "hillshade.pgw"),
    )
    if log:
        log(f"AOI shade cache hit ← {shade_root.parent.name}/shade")
    return True


def persist_shade(
    cache_dir: Path,
    shade_dir: Path,
    *,
    dem_fp: dict | None = None,
    source: str | None = None,
    log=None,
) -> None:
    png = shade_dir / "hillshade.png"
    if not png.is_file() or png.stat().st_size < 64:
        return
    dest = shade_cache_dir(cache_dir)
    copied = _copy_named_artifacts(shade_dir, dest, SHADE_ARTIFACT_NAMES)
    write_meta(
        dest,
        kind="shade",
        dem_fp=dem_fp,
        source=source or ("gdaldem" if dem_fp else "cuzk_wms"),
        files=copied,
    )
    if log:
        log(f"AOI shade uložen → {cache_dir.name}/shade")


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
