"""Stínování terénu (DTM) a povrchu (DSM) z rastrů jobu jako PNG šablony mimo Česko.

V Česku se hillshade DMR 5G a náhled DMP OK stahují z WMS ČÚZK (``reference_layers``). V zahraničí ty služby nejsou,
ale job má vlastní výšky z rastru země: ``work/dem/dem_filled.tif`` (terén) a ``work/dem/dsm_filled.tif`` (povrch,
obdoba DMP OK). Z nich se spočte šedé stínování (azimut a výška slunce jako u WMS ČÚZK) a přibalí se do
``references/`` jako ostatní PNG podklady. Do kresby nevstupuje.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from app.pipeline.georef import PgwGeoref
from app.pipeline.gdal_cli_raster import read_float32_geotiff

# (klíč v built_refs, zdrojový rastr ve work/dem, výstupní PNG, popisek šablony v OOM, průhlednost)
TERRAIN_SHADES: tuple[tuple[str, str, str, str, float], ...] = (
    ("hillshade_dtm", "dem_filled.tif", "hillshade_dtm.png", "Hillshade DTM", 0.55),
    ("hillshade_dsm", "dsm_filled.tif", "hillshade_dsm.png", "Hillshade DSM", 0.65),
)
AZIMUTH_DEG = 315.0
ALTITUDE_DEG = 45.0
# Jemnější než 1 m/px šablona nepotřebuje (NL AHN má 0,5 m); strop strany PNG jako u výšky vegetace.
MIN_PIXEL_M = 1.0
MAX_PX = 8192


def _downsample_mean(arr: np.ndarray, factor: int) -> np.ndarray:
    """Zmenšení průměrem bloku (NaN se nepočítá); okraj doplněný NaN."""
    if factor <= 1:
        return arr
    rows = math.ceil(arr.shape[0] / factor) * factor
    cols = math.ceil(arr.shape[1] / factor) * factor
    padded = np.full((rows, cols), np.nan, dtype=np.float32)
    padded[: arr.shape[0], : arr.shape[1]] = arr
    blocks = padded.reshape(rows // factor, factor, cols // factor, factor)
    with np.errstate(all="ignore"):
        return np.nanmean(blocks, axis=(1, 3)).astype(np.float32)


def hillshade(
    elev: np.ndarray, pixel_x: float, pixel_y: float, *, azimuth: float = AZIMUTH_DEG, altitude: float = ALTITUDE_DEG
) -> np.ndarray:
    """Stínování 0–255 (uint8) a maska platných buněk (Lambert jako ``gdaldem hillshade``, sever nahoře)."""
    z = np.asarray(elev, dtype=np.float32)
    valid = np.isfinite(z)
    if not valid.any():
        return np.zeros(z.shape, dtype=np.uint8), valid
    filled = np.where(valid, z, np.nanmean(z[valid])).astype(np.float32)
    dz_south, dz_east = np.gradient(filled, abs(pixel_y), abs(pixel_x))
    dz_north = -dz_south  # řádky rastru jdou k jihu
    # normála povrchu (−dz/dx, −dz/dy, 1) · směr ke slunci (azimut po směru hodin od severu)
    az, alt = math.radians(azimuth), math.radians(altitude)
    sun_e, sun_n, sun_z = math.sin(az) * math.cos(alt), math.cos(az) * math.cos(alt), math.sin(alt)
    shade = (sun_z - dz_east * sun_e - dz_north * sun_n) / np.sqrt(1.0 + dz_east**2 + dz_north**2)
    out = np.clip(shade * 255.0, 0, 255).round().astype(np.uint8)
    return out, valid


def write_shade_png(src_tif: Path, out_png: Path, *, log=None) -> Path | None:
    """PNG (šedá, mimo data průhledná) + ``.pgw`` ze zdrojového GeoTIFF; None, když rastr chybí nebo je prázdný."""
    from PIL import Image

    src_tif = Path(src_tif)
    if not src_tif.is_file():
        return None
    arr, gt, nodata = read_float32_geotiff(src_tif, log=log)
    if nodata is not None:
        arr = np.where(arr == np.float32(nodata), np.nan, arr)
    if gt[2] or gt[4]:
        raise RuntimeError("rastr s rotací nepodporováno")
    factor = max(1, math.ceil(MIN_PIXEL_M / max(abs(gt[1]), 1e-9) - 1e-6), math.ceil(max(arr.shape) / MAX_PX))
    arr = _downsample_mean(arr, factor)
    px, py = gt[1] * factor, gt[5] * factor
    gray, valid = hillshade(arr, px, py)
    if not valid.any():
        return None
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    if valid.all():
        Image.fromarray(gray, "L").save(out_png, optimize=True)
    else:
        alpha = np.where(valid, 255, 0).astype(np.uint8)
        Image.fromarray(np.dstack([gray, alpha]), "LA").save(out_png, optimize=True)
    PgwGeoref(
        pixel_x=px, rot_row=0.0, rot_col=0.0, pixel_y=py, origin_x=gt[0] + px / 2, origin_y=gt[3] + py / 2
    ).write(out_png.with_suffix(".pgw"))
    if log:
        log(f"Stínování: {out_png.name} {gray.shape[1]}×{gray.shape[0]} px ({abs(px):g} m/px)")
    return out_png


def write_terrain_shades(dem_dir: Path, out_dir: Path, *, log=None) -> dict[str, Path]:
    """``{klíč: png}`` stínování terénu a povrchu, které se podařilo spočítat (chyba jedné vrstvy jen do logu)."""
    from app.pipeline.job_progress import log_issue

    built: dict[str, Path] = {}
    for key, src, png_name, _label, _opacity in TERRAIN_SHADES:
        try:
            png = write_shade_png(Path(dem_dir) / src, Path(out_dir) / png_name, log=log)
        except Exception as exc:  # podklad nesmí shodit job
            log_issue(log, f"Stínování {png_name}: přeskočeno ({exc})")
            continue
        if png is not None:
            built[key] = png
    return built
