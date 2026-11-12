"""Obarvená výška vegetace (DMP OK − DMR 5G) jako PNG šablona (Petr 2026-11-12, námět z omapa.cz).

Hotová online vrstva výšky vegetace pro ČR není (ČÚZK ``dmp`` ImageServer má jen stínování, sklon a orientaci),
proto se obarví ``work/dem/chm.tif`` jobu. Přibaluje se jen se zapnutými referenčními podklady
(``output_references``) jako ostatní PNG podklady. Pod ``MIN_HEIGHT_M`` průhledné (bez vegetace), aby šablona
nepřekrývala louky a cesty. Mapař podle ní zkontroluje porosty 401–410; do kresby nevstupuje.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from app.pipeline.georef import PgwGeoref
from app.pipeline.gdal_cli_raster import read_float32_geotiff

PNG_NAME = "vyska_vegetace.png"
PGW_NAME = "vyska_vegetace.pgw"
REF_KEY = "vyska_vegetace"
LABEL = "Výška vegetace (DMP OK − DMR 5G)"
# Pod touto výškou průhledné (tráva, šum DMP OK z obrazové korelace).
MIN_HEIGHT_M = 0.5
# Barevná stupnice (výška m, R, G, B), mezi body lineárně: keře žlutě, mlazina oranžově, les zelenými odstíny.
COLOR_STOPS: tuple[tuple[float, int, int, int], ...] = (
    (0.5, 255, 247, 160),
    (2.0, 255, 196, 70),
    (5.0, 214, 120, 40),
    (8.0, 175, 220, 100),
    (15.0, 80, 170, 60),
    (25.0, 25, 105, 45),
    (35.0, 10, 55, 60),
)
# Strop strany PNG (Mapper načte celý obrázek, jako ``MAX_REF_PIXELS``).
MAX_PX = 8192


def colorize_heights(heights: np.ndarray, nodata: float | None = None) -> np.ndarray:
    """Výšky (m) → RGBA uint8; pod ``MIN_HEIGHT_M``, NaN a NoData průhledné."""
    h = np.asarray(heights, dtype=np.float32)
    valid = np.isfinite(h) & (h >= MIN_HEIGHT_M)
    if nodata is not None:
        valid &= h != np.float32(nodata)
    levels = np.array([s[0] for s in COLOR_STOPS], dtype=np.float32)
    rgba = np.zeros(h.shape + (4,), dtype=np.uint8)
    hv = np.where(valid, h, levels[0])
    for band in range(3):
        values = np.array([s[band + 1] for s in COLOR_STOPS], dtype=np.float32)
        rgba[..., band] = np.interp(hv, levels, values).round().astype(np.uint8)
    rgba[..., 3] = np.where(valid, 255, 0).astype(np.uint8)
    return rgba


def _downsample_max(arr: np.ndarray, factor: int) -> np.ndarray:
    """Zmenšení blokovým maximem (nejvyšší strom bloku zůstane vidět); okraj doplněný NaN."""
    if factor <= 1:
        return arr
    rows = math.ceil(arr.shape[0] / factor) * factor
    cols = math.ceil(arr.shape[1] / factor) * factor
    padded = np.full((rows, cols), np.nan, dtype=np.float32)
    padded[: arr.shape[0], : arr.shape[1]] = arr
    blocks = padded.reshape(rows // factor, factor, cols // factor, factor)
    with np.errstate(all="ignore"):
        out = np.nanmax(blocks, axis=(1, 3))
    return out


def write_vegetation_height_png(chm_tif: Path, out_dir: Path, *, log=None) -> Path | None:
    """Zapíše ``vyska_vegetace.png`` + ``.pgw`` do ``out_dir``; None, když CHM chybí nebo je prázdné."""
    from PIL import Image

    chm_tif = Path(chm_tif)
    if not chm_tif.is_file():
        return None
    arr, gt, nodata = read_float32_geotiff(chm_tif, log=log)
    if nodata is not None:
        arr = np.where(arr == np.float32(nodata), np.nan, arr)
    if gt[2] or gt[4]:
        raise RuntimeError("CHM s rotací nepodporováno")
    factor = max(1, math.ceil(max(arr.shape) / MAX_PX))
    arr = _downsample_max(arr, factor)
    rgba = colorize_heights(arr)
    if not rgba[..., 3].any():
        if log:
            log("Výška vegetace: v CHM žádná vegetace – PNG se nepřibaluje")
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / PNG_NAME
    Image.fromarray(rgba, "RGBA").save(png, optimize=True)
    px, py = gt[1] * factor, gt[5] * factor
    PgwGeoref(
        pixel_x=px,
        rot_row=0.0,
        rot_col=0.0,
        pixel_y=py,
        origin_x=gt[0] + px / 2,
        origin_y=gt[3] + py / 2,
    ).write(out_dir / PGW_NAME)
    if log:
        log(f"Výška vegetace: {PNG_NAME} {rgba.shape[1]}×{rgba.shape[0]} px ({abs(px):g} m/px)")
    return png
