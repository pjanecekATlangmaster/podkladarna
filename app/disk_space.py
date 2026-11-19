"""Místo na disku s daty (``DATA_ROOT``): rozpoznání plného disku, rezerva, kontrola před jobem, úklid cache.

Ostrá A 2026-11-15 (job e38f9268e26f, 3.37.1): plný ``/data`` shodil job a stav „selhalo“ se nezapsal
(zápis logu selhal dřív než zápis stavu), web ukazoval „běží“. Proto:

* ``is_disk_full`` pozná ENOSPC (OSError, SQLite „database or disk is full“, text výjimky),
* malá rezerva ``.disk_reserve`` se při plném disku smaže, aby šel zapsat stav jobu do DB,
* před jobem se ověří volné místo podle plochy výřezu; když chybí, smaže se nejdřív nejstarší cache,
  teprve pak job skončí s hláškou „Na serveru došlo místo na disku“,
* strop cache LiDARu se omezí i podílem velikosti disku (``LIDAR_CACHE_MAX_DISK_SHARE``),
* cache podle výřezu (podklady, DEM, ZABAGED, RÚIAN, AOPK) starší než jejich platnost se maže –
  po ní by se stejně znovu stahovala, výsledek mapy se tím nemění.

Ostrá A 2026-11-19 (job bcc359d5458f): disk s joby (130 GB) zaplnilo 68 hotových jobů po 3–4 GB, cache leží
na jiném disku, takže úklid cache nepomohl a job selhal. Při nedostatku místa na disku s joby se proto
smažou nejdřív mezivýsledky (``work/``) nejstarších hotových jobů a pak celé nejstarší hotové joby
(ne objednávky, ne joby dokončené před méně než ``DISK_JOB_DELETE_MIN_AGE_HOURS``).
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

from app import settings

logger = logging.getLogger("podkladarna.disk_space")

# Technická zpráva (log jobu, výjimka); ``failure_summary`` z ní udělá hlášku pro uživatele.
DISK_FULL_MESSAGE = "Na serveru došlo místo na disku"
RESERVE_NAME = ".disk_reserve"

_DISK_FULL_TEXTS = ("no space left on device", "disk is full", "disk full", "errno 28", "out of disk space")


class DiskFullError(OSError):
    """Na disku s daty není dost místa pro job (ani po úklidu cache)."""

    def __init__(self, detail: str = ""):
        msg = DISK_FULL_MESSAGE + (f" ({detail})" if detail else "")
        super().__init__(errno.ENOSPC, msg)

    def __str__(self) -> str:  # bez „[Errno 28]“ – text jde do logu i do hlášky
        return self.strerror or DISK_FULL_MESSAGE


def is_disk_full(exc: BaseException | None) -> bool:
    """Výjimka (nebo některá z jejích příčin) je plný disk."""
    seen = 0
    while exc is not None and seen < 5:
        if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
            return True
        if isinstance(exc, sqlite3.OperationalError) and "full" in str(exc).lower():
            return True
        text = str(exc).lower()
        if any(t in text for t in _DISK_FULL_TEXTS):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def data_root() -> Path:
    return Path(settings.DATA_ROOT)


def _usage(path: Path):
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe)


def free_bytes(path: Path | None = None) -> int | None:
    try:
        return int(_usage(path or data_root()).free)
    except OSError:
        return None


def total_bytes(path: Path | None = None) -> int | None:
    try:
        return int(_usage(path or data_root()).total)
    except OSError:
        return None


def cache_root() -> Path:
    return Path(settings.DOWNLOADS_DIR)


def _device(path: Path) -> int | None:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return probe.stat().st_dev
    except OSError:
        return None


def same_disk(a: Path, b: Path) -> bool:
    """Obě cesty leží na stejném souborovém systému (neznámé = ano, jako dosud).

    Ostrá A na pve1 má cache (``/data/cache``) na vlastním 2TB disku a joby s DB na 130GB disku:
    mazání cache tam místo pro joby neuvolní.
    """
    da, db_ = _device(a), _device(b)
    return da is None or db_ is None or da == db_


# --- Rezerva pro zápis stavu jobu ---------------------------------------------------------------


def reserve_path() -> Path:
    return data_root() / RESERVE_NAME


def ensure_reserve() -> bool:
    """Vytvoří rezervu (``DISK_RESERVE_MB``), je-li místo; vrací, zda rezerva existuje."""
    size = int(max(0.0, float(settings.DISK_RESERVE_MB)) * 1024 * 1024)
    path = reserve_path()
    if size <= 0:
        return False
    try:
        if path.is_file() and path.stat().st_size >= size:
            return True
        free = free_bytes(path.parent)
        # Rezervu nebrat z posledního místa (job by kvůli ní spadl dřív).
        if free is None or free < size * 4:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        chunk = b"\0" * (1024 * 1024)
        with tmp.open("wb") as f:
            for _ in range(size // len(chunk)):
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            path.with_name(path.name + ".tmp").unlink(missing_ok=True)
        except OSError:
            pass
        return False


def release_reserve() -> bool:
    """Smaže rezervu (plný disk: uvolní místo pro zápis stavu jobu); vrací, zda něco uvolnila."""
    try:
        path = reserve_path()
        if path.is_file():
            path.unlink()
            logger.warning("Plný disk: uvolněna rezerva %s", path)
            return True
    except OSError:
        pass
    return False


def write_with_reserve(fn, *args, **kwargs):
    """Zavolá ``fn``; selže-li na plném disku, smaže rezervu a zkusí to ještě jednou."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 – opakujeme jen při plném disku
        if not is_disk_full(exc) or not release_reserve():
            raise
    return fn(*args, **kwargs)


# --- Cache ---------------------------------------------------------------------------------------


def lidar_cache_max_bytes() -> int:
    """Strop cache LiDARu: ``LIDAR_CACHE_MAX_GB``, nejvýš ``LIDAR_CACHE_MAX_DISK_SHARE`` velikosti disku. 0 = bez limitu."""
    cap = int(float(settings.LIDAR_CACHE_MAX_GB) * 1e9)
    share = float(settings.LIDAR_CACHE_MAX_DISK_SHARE)
    total = total_bytes(Path(settings.DOWNLOADS_DIR)) if share > 0 else None
    if total and share > 0:
        by_disk = int(total * share)
        cap = by_disk if cap <= 0 else min(cap, by_disk)
    return max(0, cap)


def aoi_cache_roots() -> list[tuple[Path, int]]:
    """Cache podle výřezu (položka = přímý potomek složky) a její platnost ve dnech."""
    root = Path(settings.DOWNLOADS_DIR)
    return [
        (root / "references", settings.REF_CACHE_MAX_AGE_DAYS),
        (root / "surfaces", settings.SURFACES_CACHE_MAX_AGE_DAYS),
        (root / "zabaged", settings.ZABAGED_CACHE_MAX_AGE_DAYS),
        (root / "ruian", settings.RUIAN_CACHE_MAX_AGE_DAYS),
        (root / "aopk", settings.AOPK_CACHE_MAX_AGE_DAYS),
        (root / "osm", osm_cache_max_age_days()),
        (root / "sk_zbgis", settings.SK_ZBGIS_CACHE_MAX_AGE_DAYS),  # celostátní sady, položka = sada (bu / tn)
    ]


def osm_cache_max_age_days() -> int:
    """Stáří odpovědi Overpass, po kterém se smaže: ``OSM_CACHE_MAX_AGE_DAYS``, nejméně platnost cache + 1 den
    (starší soubor job nepoužije, výsledek mapy se smazáním nemění). 0 = nemazat."""
    import math

    from app.pipeline.osm_paths import osm_cache_max_age_hours

    days = int(settings.OSM_CACHE_MAX_AGE_DAYS)
    if days <= 0:
        return 0
    return max(days, math.ceil(osm_cache_max_age_hours() / 24) + 1)


def _entry_mtime(path: Path) -> float:
    """Poslední zápis / použití položky (nejnovější z ní a jejích přímých souborů)."""
    try:
        best = path.stat().st_mtime
    except OSError:
        return 0.0
    if path.is_dir():
        try:
            for child in path.iterdir():
                try:
                    best = max(best, child.stat().st_mtime)
                except OSError:
                    continue
        except OSError:
            pass
    return best


def _entry_bytes(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def aoi_cache_entries() -> list[tuple[Path, int, float]]:
    """(položka, platnost dní, čas posledního použití) pro všechny cache podle výřezu."""
    out: list[tuple[Path, int, float]] = []
    for root, days in aoi_cache_roots():
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        out.extend((child, days, _entry_mtime(child)) for child in children)
    return out


def prune_expired_aoi_cache(*, now: float | None = None, log=None) -> int:
    """Smaže položky cache podle výřezu starší než jejich platnost; vrací uvolněné bajty."""
    now = time.time() if now is None else now
    freed = 0
    removed = 0
    for path, days, mtime in aoi_cache_entries():
        if days <= 0 or now - mtime <= days * 86400:
            continue
        freed += _entry_bytes(path)
        _remove(path)
        removed += 1
    if removed and log:
        log(f"Cache podle výřezu: smazáno {removed} prošlých položek ({freed / 1e9:.1f} GB)")
    return freed


def prune_all_caches(*, log=None) -> int:
    """Pravidelný úklid: prošlá cache podle výřezu (i odpovědi Overpass) + strop cache LiDARu + cache dlaždic
    webu; vrací uvolněné bajty."""
    from app.download_cache import prune_lidar_cache
    from app.tiles import prune_tile_cache

    freed = prune_expired_aoi_cache(log=log)
    freed += prune_lidar_cache(log=log)
    freed += prune_tile_cache(log=log)
    return freed


def free_space_for(need_bytes: int, *, path: Path | None = None, keep_sheets=frozenset(), log=None) -> int:
    """Uvolní na disku s ``path`` (výchozí ``DATA_ROOT``) místo do ``need_bytes`` volného: nejdřív nejdéle
    nepoužitá cache podle výřezu, pak listy LiDARu. Je-li cache na jiném disku, nemaže nic (nepomohlo by)."""
    from app.download_cache import lidar_cache_units, prune_lidar_cache, _unit_bytes

    target_path = Path(path) if path is not None else data_root()
    free = free_bytes(target_path)
    if free is None or free >= need_bytes:
        return 0
    if not same_disk(target_path, cache_root()):
        return 0
    freed = prune_expired_aoi_cache(log=log)
    free = free_bytes(target_path) or 0
    if free >= need_bytes:
        return freed
    removed = 0
    for entry, _days, _mtime in sorted(aoi_cache_entries(), key=lambda e: e[2]):
        if (free_bytes(target_path) or 0) >= need_bytes:
            break
        freed += _entry_bytes(entry)
        _remove(entry)
        removed += 1
    if removed and log:
        log(f"Málo místa na disku: smazáno {removed} nejdéle nepoužitých položek cache podle výřezu")
    free = free_bytes(target_path) or 0
    if free >= need_bytes:
        return freed
    seen: set[tuple[int, int]] = set()
    lidar_total = sum(_unit_bytes(dirs, seen) for _n, dirs in lidar_cache_units())
    target = max(0, lidar_total - (need_bytes - free))
    if lidar_total > 0:
        # 0 by znamenalo „bez limitu“; aspoň 1 bajt = smazat vše kromě listů jobu.
        freed += prune_lidar_cache(max_bytes=max(1, target), keep_sheets=keep_sheets, log=log)
    return freed


# Z ``work/`` hotového jobu zůstane jen to, co dál čte web a statistika (náhled, souhrny).
WORK_KEEP = frozenset(
    {"build_stats.json", "source_meta.json", "dtm_summary.json", "preview.jpg", "preview.png", "preview"}
)


def trim_job_work(job_dir: Path) -> int:
    """Smaže mezivýsledky jobu (``work/`` kromě ``WORK_KEEP``); vrací uvolněné bajty. ZIP a náhled jsou v ``output/``."""
    work = Path(job_dir) / "work"
    if not work.is_dir():
        return 0
    freed = 0
    for entry in list(work.iterdir()):
        if entry.name in WORK_KEEP:
            continue
        freed += _entry_bytes(entry)
        _remove(entry)
    return freed


def _finished_jobs_oldest_first() -> list[tuple[str, float]]:
    """(id, čas dokončení jako unix čas) hotových / selhaných jobů mimo objednávky, nejstarší první."""
    from datetime import datetime, timezone

    from app import db

    out: list[tuple[str, float]] = []
    for job_id, updated_at in db.list_finished_public_job_times():
        try:
            updated = datetime.fromisoformat(updated_at)
        except ValueError:
            updated = datetime.fromtimestamp(0, timezone.utc)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        out.append((job_id, updated.timestamp()))
    out.sort(key=lambda e: e[1])
    return out


def free_job_space(need_bytes: int, *, path: Path | None = None, now: float | None = None, log=None) -> int:
    """Uvolní na disku s joby místo do ``need_bytes`` volného: nejdřív ``work/`` nejstarších hotových jobů,
    pak celé nejstarší hotové joby dokončené dřív než před ``DISK_JOB_DELETE_MIN_AGE_HOURS``; vrací uvolněné bajty."""
    from app import db

    target_path = Path(path) if path is not None else data_root()
    if (free_bytes(target_path) or 0) >= need_bytes:
        return 0
    jobs_dir = Path(db.JOBS_DIR)
    if not jobs_dir.is_dir() or not same_disk(target_path, jobs_dir):
        return 0
    jobs = _finished_jobs_oldest_first()
    freed = 0
    trimmed = 0
    for job_id, _t in jobs:
        if (free_bytes(target_path) or 0) >= need_bytes:
            break
        got = trim_job_work(jobs_dir / job_id)
        if got:
            freed += got
            trimmed += 1
    if trimmed and log:
        log(f"Málo místa na disku s joby: smazány mezivýsledky {trimmed} nejstarších hotových jobů ({freed / 1e9:.1f} GB)")
    now = time.time() if now is None else now
    min_age = max(0.0, float(settings.DISK_JOB_DELETE_MIN_AGE_HOURS)) * 3600
    deleted = 0
    for job_id, finished in jobs:
        if (free_bytes(target_path) or 0) >= need_bytes:
            break
        if now - finished < min_age:
            break  # seřazeno od nejstarších: dál jsou jen čerstvější
        size = _entry_bytes(jobs_dir / job_id)
        if db.delete_job(job_id):
            freed += size
            deleted += 1
    if deleted and log:
        log(f"Málo místa na disku s joby: smazáno {deleted} nejstarších hotových jobů")
    return freed


def job_need_bytes(area_km2: float | None) -> int:
    """Odhad místa, které job potřebuje (stažené listy, ořezy, rastry, podklady, výstup)."""
    area = max(0.0, float(area_km2 or 0.0))
    gb = float(settings.DISK_JOB_BASE_GB) + float(settings.DISK_JOB_GB_PER_KM2) * area
    return int(max(0.0, gb) * 1e9)


def _ensure_space_on(path: Path, need: int, label: str, *, jobs: bool = False, log=None) -> None:
    free = free_bytes(path)
    if free is None or free >= need:
        return
    if log:
        log(f"Málo místa na disku {label}: volno {free / 1e9:.1f} GB, job potřebuje asi {need / 1e9:.1f} GB – uklízím cache…")
    try:
        free_space_for(need, path=path, log=log)
    except Exception:  # noqa: BLE001 – úklid nesmí shodit job, rozhodne kontrola níže
        logger.exception("Úklid cache při nedostatku místa selhal")
    if jobs:
        try:
            free_job_space(need, path=path, log=log)
        except Exception:  # noqa: BLE001
            logger.exception("Úklid starých jobů při nedostatku místa selhal")
    free = free_bytes(path)
    if free is not None and free < need:
        raise DiskFullError(f"{label}: volno {free / 1e9:.1f} GB, job potřebuje asi {need / 1e9:.1f} GB")


def ensure_job_space(area_km2: float | None, *, log=None) -> None:
    """Před jobem: dost místa, jinak úklid cache; když ani pak nestačí, ``DiskFullError``.

    Disk s joby (``DATA_ROOT``) a disk s cache (``DOWNLOADS_DIR``) se kontrolují zvlášť, leží-li jinde
    (ostrá A na pve1); odhad místa je pro oba stejný (stažené listy jdou do cache, rastry a ZIP k jobu).
    """
    need = job_need_bytes(area_km2)
    if need <= 0:
        return
    _ensure_space_on(data_root(), need, "s daty", jobs=True, log=log)
    if not same_disk(data_root(), cache_root()):
        _ensure_space_on(cache_root(), need, "s cache", log=log)
