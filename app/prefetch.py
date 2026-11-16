"""Předstažení velkých dat mimo frontu výpočtů (Petr 2026-11-16).

Job v Česku, kterému chybí v cache aspoň ``PREFETCH_MIN_SHEETS`` listů LiDARu (DMP OK ~350 MB na list),
nečeká při obsazeném serveru ve frontě s celým stahováním uvnitř buildu: listy stáhne samostatný proces
s nižší prioritou (``python -m app.prefetch <job>``, nejvýš ``PREFETCH_CONCURRENCY`` naráz) a teprve pak se job
zařadí do normální fronty. Stažené listy jsou v téže sdílené cache, kterou build čte (``_cached_laz``),
takže kresba je stejná jako bez předstažení. Selhání předstažení job nezastaví: zařadí se a build si
data stáhne sám jako dřív. Mimo Česko se výšky stahují jen pro výřez do složky jobu, tam se nic nemění.
"""

from __future__ import annotations

import logging
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app import db, i18n, settings

logger = logging.getLogger(__name__)

# Velikost jednoho listu SM5 ke stažení (DMP OK ~340 MB + DMR 5G pár MB), pro odhad v GUI.
SHEET_DOWNLOAD_MB = 350


def _lidar_job(options: dict[str, Any]) -> bool:
    from app.pipeline.country_profile import elevation_source

    return elevation_source(options) == "cz_openzu" and len(options.get("bbox_wgs84") or []) == 4


def missing_sheets(sheet_names: list[str]) -> list[str]:
    """Listy SM5 bez DMP OK v cache (stejné kritérium jako odhad času)."""
    from app.pipeline.fetch_openzu import dmpok_cached_mapnoms

    names = [str(n) for n in sheet_names or []]
    cached = dmpok_cached_mapnoms(names)
    return [n for n in names if n not in cached]


def is_big_download(missing: list[str]) -> bool:
    return settings.PREFETCH_ENABLED and len(missing) >= max(1, settings.PREFETCH_MIN_SHEETS)


def job_missing_sheets(options: dict[str, Any] | None) -> list[str]:
    """Chybějící listy jobu, které má smysl stáhnout předem (prázdné = job jde rovnou do fronty)."""
    opts = options or {}
    if not settings.PREFETCH_ENABLED or not _lidar_job(opts):
        return []
    # Znovupoužitý LiDAR z jiného jobu nebo vynucené nové stažení: build stejně stahuje / nestahuje sám.
    if opts.get("reused_from") or opts.get("force_refresh"):
        return []
    try:
        missing = missing_sheets(list(opts.get("sm5_sheets") or []))
    except Exception:  # noqa: BLE001 – předstažení je jen optimalizace fronty
        logger.exception("Kontrola cache listů selhala")
        return []
    return missing if is_big_download(missing) else []


def download_minutes(missing: int, prep_factor: float = 1.0) -> int:
    from app.pipeline.fetch_openzu import _EST_DOWNLOAD_MIN_PER_SHEET

    return max(1, math.ceil(missing * _EST_DOWNLOAD_MIN_PER_SHEET * max(prep_factor, 0.25)))


def big_download_info(sheet_names: list[str], prep_factor: float = 1.0) -> dict[str, Any] | None:
    """Pro ``/api/sheets``: velké stahování výřezu (počet listů, MB, minuty, hláška), jinak None."""
    if not settings.PREFETCH_ENABLED:
        return None
    missing = missing_sheets(sheet_names)
    if not is_big_download(missing):
        return None
    mb = len(missing) * SHEET_DOWNLOAD_MB
    minutes = download_minutes(len(missing), prep_factor)
    return {
        "sheets": len(missing),
        "mb": mb,
        "minutes": minutes,
        "message": i18n.Msg("api.sheets.big_download", count=len(missing), gb=round(mb / 1000, 1), minutes=minutes),
    }


# --- Proces předstažení ---------------------------------------------------------------------


def _lower_priority() -> None:
    """Nižší priorita CPU (a na Linuxu I/O) – stahování nesmí brzdit běžící build."""
    try:
        os.nice(max(0, settings.PREFETCH_NICE))
    except (AttributeError, OSError):
        pass
    if sys.platform.startswith("linux"):
        try:
            # ionice best-effort s nejnižší prioritou 7 (ioprio_set); bez práv nebo jádra tiše nic.
            import ctypes

            nr = {"x86_64": 251, "aarch64": 30}.get(os.uname().machine)
            if nr is not None:
                ctypes.CDLL(None, use_errno=True).syscall(nr, 1, 0, (2 << 13) | 7)
        except Exception:  # noqa: BLE001
            pass


def run_prefetch(job_id: str) -> int:
    """Stáhne chybějící listy jobu do cache. 0 = vše staženo, 1 = něco selhalo (job se zařadí i tak)."""
    from app import disk_space
    from app.download_cache import lidar_sheet_dir
    from app.pipeline.fetch_openzu import OPENZU_DMR, _cached_dmp_laz, _cached_laz

    def log(msg: str) -> None:
        db.append_log(job_id, f"Předstažení dat: {msg}")

    try:
        job = db.get_job(job_id)
    except KeyError:
        return 1
    options = job.get("options") or {}
    missing = job_missing_sheets(options) or missing_sheets(list(options.get("sm5_sheets") or []))
    if not missing:
        return 0
    log(
        f"{len(missing)} listů LiDAR (~{len(missing) * SHEET_DOWNLOAD_MB / 1000:.1f} GB) "
        "s nižší prioritou mimo frontu výpočtů …"
    )
    # Jen kontrola místa, nic se nemaže: úklid cache souběžně s běžícím buildem by mohl smazat jeho listy
    # (review PR 354). Když místo nestačí, stáhne data až build se svým ``ensure_job_space``.
    need = disk_space.job_need_bytes(db.job_area_km2(options)) + len(missing) * SHEET_DOWNLOAD_MB * 1_000_000
    free = disk_space.free_bytes(disk_space.cache_root())
    if need > 0 and free is not None and free < need:
        log(f"⚠ málo místa na disku s cache ({free / 1e9:.1f} GB volno) – data stáhne až výpočet")
        return 1

    def one(mapnom: str) -> None:
        # Jen chybějící soubory: list, který mezitím stáhl běžící job, se znovu neověřuje ani nepřepisuje.
        folder = lidar_sheet_dir(mapnom)
        if not (folder / "DMR5G.laz").is_file():
            _cached_laz(mapnom, "DMR5G", OPENZU_DMR.format(mapnom=mapnom), None)
        if missing_sheets([mapnom]):
            _cached_dmp_laz(mapnom, None)

    failed: list[str] = []
    workers = max(1, min(settings.PREFETCH_DOWNLOAD_WORKERS, len(missing)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="prefetch") as pool:
        futures = {name: pool.submit(one, name) for name in missing}
        for name, fut in futures.items():
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001
                failed.append(name)
                logger.warning("Předstažení listu %s selhalo: %s", name, exc)
    if failed:
        log(f"⚠ nestaženo {len(failed)} z {len(missing)} listů ({', '.join(failed)}) – stáhne je až výpočet")
        return 1
    log(f"hotovo, {len(missing)} listů je v mezipaměti – job čeká ve frontě výpočtů")
    _refresh_estimate(job_id, options)
    return 0


def _refresh_estimate(job_id: str, options: dict[str, Any]) -> None:
    """Odhad přípravy pro kalibraci bez stahování (listy už jsou v cache), ať kalibrace nepodstřelí."""
    try:
        from app.pipeline.fetch_openzu import estimate_job_parts

        area = db.job_area_km2(options)
        if area is not None:
            db.update_origin_estimate(job_id, estimate_job_parts(options, area)[0])
    except Exception:  # noqa: BLE001 – jen statistika
        logger.exception("Přepočet odhadu jobu %s po předstažení", job_id)


def main() -> int:
    if len(sys.argv) != 2:
        print("Použití: python -m app.prefetch <job_id>", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO)
    _lower_priority()
    return run_prefetch(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
