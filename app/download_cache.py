from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
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


# DMR a DMP téhož listu se stahují souběžně do jedné složky → read-modify-write meta pod zámkem.
_META_LOCK = threading.Lock()


def write_meta(folder: Path, **fields) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with _META_LOCK:
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
# Celý list vyfiltrovaný receptem (bez ořezu na výřez): ``lidar_sheet_crop/<list>/<recept>/full_<zdroj>``.
SHEET_CROP_FULL_PREFIX = "full_"
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

    # Nejmenší nadmnožina (méně bodů k dořezání); celý list (``full_*``) až nakonec.
    best: tuple[float, Path, tuple[float, float, float, float]] | None = None
    for child in recipe_root.iterdir():
        if not child.is_dir():
            continue
        laz = child / SHEET_CROP_ARTIFACT
        meta = read_meta(child) or {}
        if meta.get("full_sheet"):
            # Celý vyfiltrovaný list platí, dokud se nezmění zdrojový list (ne podle stáří).
            if _laz_usable(laz) and fingerprints_equal(meta.get("src_fp"), src_fp):
                if best is None:
                    best = (float("inf"), laz, bounds)
            continue
        if not is_fresh(child, laz, age_limit, min_size=MIN_LAZ_BYTES_SHEET):
            continue
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


_SHEET_CROP_LOCK = threading.Lock()


def persist_sheet_crop(
    src: Path,
    cropped: Path,
    bounds: tuple[float, float, float, float],
    recipe: str,
    *,
    log=None,
) -> Path | None:
    """Uloží ořez listu do sdílené cache (stejný src+bounds+filtr).

    Ořezy listů běží souběžně (``PODKLADARNA_LIDAR_WORKERS``) → zámek, ať dva
    zápisy do stejné složky (link + meta.json) nezávodí.
    """
    if not cropped.is_file() or cropped.stat().st_size < MIN_LAZ_BYTES_SHEET:
        return None
    sheet = sheet_id_for_laz(src)
    cache_dir = sheet_crop_cache_dir(sheet, recipe, bounds)
    dest = cache_dir / SHEET_CROP_ARTIFACT
    with _SHEET_CROP_LOCK:
        cache_dir.mkdir(parents=True, exist_ok=True)
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


def full_sheet_crop_dir(src: Path, recipe: str) -> Path:
    """Cache celého listu vyfiltrovaného podle receptu (bez ořezu na výřez).

    Klíč obsahuje jméno zdroje (DMPOK / DMP1G mají stejný recept vegetace)."""
    stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in src.stem.upper())
    return sheet_crop_recipe_dir(sheet_id_for_laz(src), recipe) / f"{SHEET_CROP_FULL_PREFIX}{stem}"


def lookup_full_sheet_crop(src: Path, recipe: str) -> Path | None:
    folder = full_sheet_crop_dir(src, recipe)
    laz = folder / SHEET_CROP_ARTIFACT
    meta = read_meta(folder) or {}
    if meta.get("full_sheet") and _laz_usable(laz) and fingerprints_equal(
        meta.get("src_fp"), file_fingerprint(src)
    ):
        return laz
    return None


def persist_full_sheet_crop(src: Path, built: Path, recipe: str, *, log=None) -> Path | None:
    """Přesune hotový vyfiltrovaný celý list (``built``) do cache; vrací cestu v cache."""
    if not _laz_usable(built):
        return None
    folder = full_sheet_crop_dir(src, recipe)
    dest = folder / SHEET_CROP_ARTIFACT
    with _SHEET_CROP_LOCK:
        folder.mkdir(parents=True, exist_ok=True)
        os.replace(built, dest)
        write_meta(
            folder,
            kind="lidar_sheet_crop",
            full_sheet=True,
            recipe=recipe,
            sheet_id=sheet_id_for_laz(src),
            src_fp=file_fingerprint(src),
            crop_fp=file_fingerprint(dest),
            source_name=src.name,
            downloaded_at=utcnow_iso(),
        )
    if log:
        log(
            f"Cache celého listu {sheet_id_for_laz(src)}/{recipe} uložena "
            f"({dest.stat().st_size / 1e6:.1f} MB) – další výřezy v listu bez dekomprese celého listu"
        )
    return dest


# --- Strop velikosti cache LiDARu (LRU) ---------------------------------------------------------

LAST_USED_FILENAME = ".last_used"


def touch_last_used(folder: Path) -> None:
    """Poznamená použití položky cache (čas souboru ``.last_used``); chyba nevadí."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        marker = folder / LAST_USED_FILENAME
        marker.touch(exist_ok=True)
        os.utime(marker, None)
    except OSError:
        pass


def _last_used(folders: list[Path]) -> float:
    best = 0.0
    for folder in folders:
        for probe in (folder / LAST_USED_FILENAME, folder / META_FILENAME, folder):
            try:
                best = max(best, probe.stat().st_mtime)
                break
            except OSError:
                continue
    return best


def _tree_files(folder: Path):
    try:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                yield Path(root) / name
    except OSError:
        return


def _unit_bytes(folders: list[Path], seen: set[tuple[int, int]]) -> int:
    """Velikost položky; tvrdé odkazy (link_or_copy) se počítají jednou."""
    total = 0
    for folder in folders:
        for path in _tree_files(folder):
            try:
                st = path.stat()
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += st.st_size
    return total


def lidar_cache_units() -> list[tuple[str, list[Path]]]:
    """Položky cache LiDARu k mazání jako celek: list SM5 (stažené LAZ + jeho ořezy) a AOI ořez."""
    root = settings.DOWNLOADS_DIR
    sheets: dict[str, list[Path]] = {}
    for base in (root / "lidar" / "sm5", root / "sm5", root / "lidar_sheet_crop"):
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.is_dir():
                sheets.setdefault(child.name.upper(), []).append(child)
    units = [(f"sheet:{name}", dirs) for name, dirs in sorted(sheets.items())]
    aoi_root = root / "lidar_crop"
    if aoi_root.is_dir():
        units.extend(
            (f"aoi:{child.name}", [child]) for child in sorted(aoi_root.iterdir()) if child.is_dir()
        )
    return units


def prune_lidar_cache(
    *,
    max_bytes: int | None = None,
    keep_sheets: set[str] | frozenset[str] = frozenset(),
    keep_dirs: tuple[Path, ...] = (),
    log=None,
) -> int:
    """Nad stropem ``LIDAR_CACHE_MAX_GB`` smaže nejdéle nepoužité listy / AOI ořezy; vrací uvolněné bajty.

    Listy a AOI právě běžícího jobu (``keep_sheets``, ``keep_dirs``) se nemažou."""
    if max_bytes is None:
        max_bytes = int(float(settings.LIDAR_CACHE_MAX_GB) * 1e9)
    if max_bytes <= 0:
        return 0
    keep_sheets = {s.upper() for s in keep_sheets}
    keep_resolved = set()
    for d in keep_dirs:
        try:
            keep_resolved.add(Path(d).resolve())
        except OSError:
            continue
    seen: set[tuple[int, int]] = set()
    sized = [(name, dirs, _unit_bytes(dirs, seen), _last_used(dirs)) for name, dirs in lidar_cache_units()]
    total = sum(size for _n, _d, size, _t in sized)
    if total <= max_bytes:
        return 0
    freed = 0
    removed: list[str] = []
    for name, dirs, size, _t in sorted(sized, key=lambda u: u[3]):
        if total - freed <= max_bytes:
            break
        if name.startswith("sheet:") and name[6:] in keep_sheets:
            continue
        if any(d.resolve() in keep_resolved for d in dirs):
            continue
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
        freed += size
        removed.append(name.split(":", 1)[1])
    if log and removed:
        log(
            f"Cache LiDARu nad limitem {max_bytes / 1e9:.0f} GB ({total / 1e9:.1f} GB): smazáno "
            f"{len(removed)} nejdéle nepoužitých položek ({freed / 1e9:.1f} GB), např. {', '.join(removed[:5])}"
        )
    return freed


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
    """Prázdný ořez (< 1 KB) není použitelný LAZ."""
    try:
        return path.is_file() and path.stat().st_size >= LIDAR_MIN_BYTES
    except OSError:
        return False


def _single_usable(lidar_dir: Path, pattern: str) -> Path | None:
    usable = [p for p in sorted(lidar_dir.glob(pattern)) if _laz_usable(p)]
    return usable[0] if len(usable) == 1 else None


def _part_index(path: Path) -> tuple[int, str]:
    """``dmp_veg_10.laz`` řadit za ``dmp_veg_2.laz`` (pořadí listů jako při ořezu)."""
    tail = path.stem.rsplit("_", 1)[-1]
    return (int(tail), path.name) if tail.isdigit() else (1 << 30, path.name)


def lidar_veg_parts(lidar_dir: Path) -> list[Path]:
    """DMP (vegetace) LAZ jobu: ``veg_merged`` (starší joby a cache), jinak ořezy listů ``dmp_veg_N``.

    Od 3.8.12 se ořezy DMP neslučují (sloučení = další čtení i zápis všech bodů); DSM i vegetace
    čtou ořezy listů přímo v pořadí listů. Listy SM5 se nepřekrývají, výsledek je stejný.
    """
    lidar_dir = Path(lidar_dir)
    merged = lidar_dir / LIDAR_VEG_MERGED
    if _laz_usable(merged):
        return [merged]
    parts = [p for p in lidar_dir.glob("dmp_veg_*.laz") if _laz_usable(p)]
    return sorted(parts, key=_part_index)


def lidar_point_files(lidar_dir: Path) -> list[Path] | None:
    """Ground LAZ + všechny DMP části (``lidar_veg_parts``); None = chybí ground nebo DMP."""
    lidar_dir = Path(lidar_dir)
    ground = lidar_dir / LIDAR_GROUND_MERGED
    if not _laz_usable(ground):
        ground = _single_usable(lidar_dir, "dmr_ground_*.laz")
    veg = lidar_veg_parts(lidar_dir)
    if ground is None or not veg:
        return None
    return [ground, *veg]


def lidar_pair_paths(lidar_dir: Path) -> tuple[Path, Path] | None:
    """(ground, první DMP část) LAZ jobu, jak je nechá ``merge_dmr_dmp``; None = nepoužitelné.

    Víc listů → ``ground_merged`` + ořezy ``dmp_veg_N`` (všechny vrací ``lidar_point_files``);
    starší joby ``veg_merged``; jediný list → jeho ořezy bez kopie.
    """
    files = lidar_point_files(lidar_dir)
    if files is None:
        return None
    return files[0], files[1]


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
    ground_name = str(pair[0])
    # Od 3.8.12 víc DMP částí (``veg_parts``); starší cache jen ``pair[1]``.
    veg_names = meta.get("veg_parts") or [pair[1]]
    if not isinstance(veg_names, list) or not veg_names:
        return None
    veg_names = [str(n) for n in veg_names]
    if not is_fresh(cache_dir, cache_dir / ground_name, age_limit, min_size=LIDAR_MIN_BYTES):
        return None
    if not all(_laz_usable(cache_dir / n) for n in veg_names):
        return None
    names = (ground_name, *veg_names)
    _copy_named_artifacts(cache_dir, lidar_work, names, required=names)
    # KP merged_crop ze starší cache už nic nečte – uvolni místo.
    for name in LEGACY_MERGED_LAZ_NAMES:
        (cache_dir / name).unlink(missing_ok=True)
    touch_last_used(cache_dir)
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
    files = lidar_point_files(lidar_work)
    if files is None:
        return
    ground, veg_parts = files[0], files[1:]
    veg = veg_parts[0]
    copied = _copy_named_artifacts(lidar_work, cache_dir, tuple(p.name for p in files))
    if veg.name != LIDAR_VEG_MERGED:
        # Starší sloučený DMP téže AOI by jen zabíral místo (čte se ``veg_parts``).
        (cache_dir / LIDAR_VEG_MERGED).unlink(missing_ok=True)
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
        veg_parts=[p.name for p in veg_parts],
        ground_fp=file_fingerprint(ground),
        veg_fp=file_fingerprint(veg),
        merged_fp=None,
    )
    touch_last_used(cache_dir)
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
