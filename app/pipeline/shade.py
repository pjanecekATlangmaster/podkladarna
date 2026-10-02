"""Hillshade stack na sdíleném job_grid – jeden primární azimut.

Primárně ČÚZK WMS ``dmr5g:GrayscaleHillshade`` (stažení do ``work/shade/``).
Lokální ``gdaldem`` z ``work/dem/dem_filled.tif`` je jen fallback, když WMS
selže (nebo ``prefer_local=True``). Výstup: ``hillshade.png`` (+ PGW)
na kanonické mřížce.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.download_cache import file_fingerprint, persist_shade, try_restore_shade
from app.pipeline.dem_prep import DEM_DIR_NAME
from app.pipeline.job_grid import JobGrid
from app.pipeline.prepare_lidar import find_tool, log_step, run_cmd
from app.pipeline.reference_layers import (
    HILLSHADE_ALTITUDE,
    HILLSHADE_AZIMUTH,
    HILLSHADE_WMS,
    _download_wms_raster,
    _gdal_tool,
)

SHADE_DIR_NAME = "shade"
SHADE_PNG_NAME = "hillshade.png"
SHADE_PGW_NAME = "hillshade.pgw"
SHADE_TIF_NAME = "hillshade.tif"
# Minimální použitelný PNG (malé AOI / unit testy mají desítky B).
MIN_SHADE_BYTES = 64
# Primární WMS vrstva (Z10/Z20 zůstávají v reference_layers, ne v preview).
PRIMARY_WMS_LAYER = "dmr5g:GrayscaleHillshade"


def shade_work_dir(work_dir: Path) -> Path:
    path = Path(work_dir) / SHADE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def dem_filled_path(work_dir: Path) -> Path | None:
    path = Path(work_dir) / DEM_DIR_NAME / "dem_filled.tif"
    if path.is_file() and path.stat().st_size > 500:
        return path
    return None


def resolve_shade_png(work_dir: Path) -> Path | None:
    path = Path(work_dir) / SHADE_DIR_NAME / SHADE_PNG_NAME
    if path.is_file() and path.stat().st_size >= MIN_SHADE_BYTES:
        return path
    return None


def _write_grid_pgw(grid: JobGrid, dest: Path) -> Path:
    grid.to_pgw().write(dest)
    return dest


def build_hillshade_from_dem(
    dem_tif: Path,
    dest_png: Path,
    dest_pgw: Path,
    grid: JobGrid,
    *,
    azimuth: float = HILLSHADE_AZIMUTH,
    altitude: float = HILLSHADE_ALTITUDE,
    log=None,
) -> bool:
    """``gdaldem hillshade`` → PNG na mřížce jobu."""
    if not dem_tif.is_file():
        return False
    shade_dir = dest_png.parent
    shade_dir.mkdir(parents=True, exist_ok=True)
    shade_tif = shade_dir / SHADE_TIF_NAME
    try:
        gdaldem = _gdal_tool("gdaldem")
    except RuntimeError:
        try:
            gdaldem = find_tool("gdaldem")
        except RuntimeError as exc:
            if log:
                log(f"Hillshade DEM: chybí gdaldem ({exc})")
            return False

    if log:
        log(
            f"Hillshade: gdaldem z {dem_tif.name} "
            f"(az {azimuth:g}°, alt {altitude:g}°)"
        )
    log_step(log, "Počítám stínování reliéfu z DEM (šedý podklad mapy)")
    run_cmd(
        [
            gdaldem,
            "hillshade",
            str(dem_tif),
            str(shade_tif),
            "-az",
            str(azimuth),
            "-alt",
            str(altitude),
            "-of",
            "GTiff",
        ],
        log=log,
    )
    if not shade_tif.is_file() or shade_tif.stat().st_size < MIN_SHADE_BYTES:
        return False

    xmin, ymin, xmax, ymax = grid.bounds()
    try:
        gdalwarp = _gdal_tool("gdalwarp")
    except RuntimeError:
        gdalwarp = find_tool("gdalwarp")
    log_step(log, "Zarovnávám stínování na mřížku mapy")
    run_cmd(
        [
            gdalwarp,
            "-te",
            str(xmin),
            str(ymin),
            str(xmax),
            str(ymax),
            "-ts",
            str(grid.width),
            str(grid.height),
            "-r",
            "cubic",
            "-of",
            "PNG",
            "-overwrite",
            str(shade_tif),
            str(dest_png),
        ],
        log=log,
    )
    shade_tif.unlink(missing_ok=True)
    # GDAL často dopíše world file / aux – přepíšeme kanonickým PGW.
    for extra in (
        dest_png.with_suffix(".png.aux.xml"),
        dest_png.with_suffix(".wld"),
    ):
        extra.unlink(missing_ok=True)
    if not dest_png.is_file() or dest_png.stat().st_size < MIN_SHADE_BYTES:
        return False
    _write_grid_pgw(grid, dest_pgw)
    if log:
        log(f"Hillshade DEM → {dest_png.name} ({grid.width}×{grid.height})")
    return True


def fetch_hillshade_wms_for_grid(
    bounds_5514: tuple[float, float, float, float],
    grid: JobGrid,
    dest_png: Path,
    dest_pgw: Path,
    *,
    layer: str = PRIMARY_WMS_LAYER,
    log=None,
) -> bool:
    """WMS grayscale hillshade zarovnaný na ``job_grid`` (bez KP šablony)."""
    log_step(
        log,
        "Stahuji stínovaný reliéf z ČÚZK (WMS DMR 5G, šedý podklad mapy)",
    )
    if log:
        log(
            f"Hillshade: WMS {layer} → {dest_png.name} "
            f"({grid.width}×{grid.height})"
        )
    ok = _download_wms_raster(
        HILLSHADE_WMS,
        layer,
        bounds_5514,
        dest_png,
        dest_pgw,
        width=grid.width,
        height=grid.height,
        image_format="image/png",
        log=log,
    )
    if ok:
        # Přepiš PGW mřížkou (WMS helper už zapisuje extent, ale drž kanoniku).
        _write_grid_pgw(grid, dest_pgw)
    return ok


def build_job_shade(
    work_dir: Path,
    *,
    bounds_5514: tuple[float, float, float, float] | None = None,
    prefer_local: bool = False,
    force: bool = False,
    cache_dir: Path | None = None,
    log=None,
) -> Path | None:
    """Sestaví primární hillshade do ``work/shade/``.

    Pořadí: existující soubor → AOI shade cache → ČÚZK WMS →
    lokální gdaldem z dem_filled (fallback). ``prefer_local=True``
    vrací staré pořadí DEM→WMS (testy / escape hatch).
    """
    work_dir = Path(work_dir)
    grid = JobGrid.load(work_dir)
    if grid is None:
        if log:
            log("Hillshade: chybí job_grid – přeskočeno")
        return None

    out_dir = shade_work_dir(work_dir)
    dest_png = out_dir / SHADE_PNG_NAME
    dest_pgw = out_dir / SHADE_PGW_NAME
    if not force and dest_png.is_file() and dest_pgw.is_file():
        if dest_png.stat().st_size >= MIN_SHADE_BYTES:
            return dest_png

    dem = dem_filled_path(work_dir)
    dem_fp = file_fingerprint(dem) if dem is not None else None
    if cache_dir is not None and try_restore_shade(
        cache_dir,
        out_dir,
        dem_fp=dem_fp,
        force=force,
        log=log,
    ):
        if dest_png.is_file() and dest_png.stat().st_size >= MIN_SHADE_BYTES:
            return dest_png

    bounds = bounds_5514 or grid.bounds()
    built = False
    source: str | None = None

    if prefer_local and dem is not None:
        try:
            if build_hillshade_from_dem(dem, dest_png, dest_pgw, grid, log=log):
                built = True
                source = "gdaldem"
        except Exception as exc:
            if log:
                log(f"Hillshade DEM selhal ({exc}) – zkouším WMS…")

    if not built:
        try:
            if fetch_hillshade_wms_for_grid(
                bounds, grid, dest_png, dest_pgw, log=log
            ):
                built = True
                source = "cuzk_wms"
        except Exception as exc:
            if log:
                log(f"Hillshade WMS selhal ({exc})")

    if not built and not prefer_local and dem is not None:
        try:
            if build_hillshade_from_dem(dem, dest_png, dest_pgw, grid, log=log):
                built = True
                source = "gdaldem"
                if log:
                    log("Hillshade: fallback gdaldem po selhání ČÚZK WMS")
        except Exception as exc:
            if log:
                log(f"Hillshade DEM fallback selhal ({exc})")

    if built and dest_png.is_file() and dest_png.stat().st_size >= MIN_SHADE_BYTES:
        if cache_dir is not None:
            persist_shade(
                cache_dir,
                out_dir,
                dem_fp=dem_fp,
                source=source,
                log=log,
            )
        return dest_png

    if log:
        log("Hillshade: žádný zdroj (WMS ani DEM)")
    return None


def copy_shade_to(
    work_dir: Path,
    dest_png: Path,
    dest_pgw: Path | None = None,
) -> bool:
    """Zkopíruje shade PNG (+ PGW) na cílové cesty."""
    src = resolve_shade_png(work_dir)
    if src is None:
        return False
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_png)
    src_pgw = src.with_suffix(".pgw")
    if dest_pgw is not None:
        if src_pgw.is_file():
            shutil.copy2(src_pgw, dest_pgw)
        else:
            grid = JobGrid.load(work_dir)
            if grid is not None:
                _write_grid_pgw(grid, dest_pgw)
            else:
                return False
    return True
