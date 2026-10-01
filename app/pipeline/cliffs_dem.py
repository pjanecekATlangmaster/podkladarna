"""Kandidáti srázů ze sdíleného DEM (vlna 1 bez KP c2g/c3g).

Počítá lokální schod na ``work/dem/dem_filled.tif`` (stejná mřížka jako
shade/kontury). Výstup: KP-kompatibilní tick DXF do ``work/temp/c2g.dxf``
(+ ``c3g.dxf`` pro větší schody), ať ``cliff_merge`` + ``cliff_height``
a OOM ``build_dxf_object_part`` zůstanou beze změny.

Symbolika earth_bank / rock_face / 206 / off zůstává UI volbou
(``kp_cliff_symbol``) — žádná auto-litologie. Citlivost mapuje
``kp_cliff_sensitivity`` na prahy cliff1/cliff2 (ini_builder).
"""

from __future__ import annotations

import math
from pathlib import Path

from app.pipeline.cliff_height import MAJOR_DROP_M, MIN_DROP_M
from app.pipeline.dem_prep import DEM_DIR_NAME
from app.pipeline.ini_builder import (
    KP_CLIFF_SENSITIVITY,
    KP_CLIFF_SENSITIVITY_DEFAULT,
    resolve_cliff_sensitivity,
)

CLIFFS_DIR_NAME = "cliffs"
# Délka ticku ~ KP buňka 3 m (merge_cliff_ticks očekává krátké úsečky).
TICK_HALF_LEN_M = 1.45
# Krok vzorkování kandidátů po rastru (m) – podmnožina buněk.
SAMPLE_STRIDE_CELLS = 2
# Max. počet ticků na AOI (ochrana RAM/OOM).
MAX_TICKS = 80_000


def dem_filled_path(work_dir: Path) -> Path | None:
    path = Path(work_dir) / DEM_DIR_NAME / "dem_filled.tif"
    if path.is_file() and path.stat().st_size > 500:
        return path
    return None


def resolve_drop_thresholds(options: dict | None) -> tuple[float, float]:
    """(cliff1, cliff2) z kp_cliff_sensitivity; floor na MIN_DROP_M."""
    key = resolve_cliff_sensitivity(options)
    c1, c2 = KP_CLIFF_SENSITIVITY.get(
        key, KP_CLIFF_SENSITIVITY[KP_CLIFF_SENSITIVITY_DEFAULT]
    )
    return max(float(c1), MIN_DROP_M), max(float(c2), MAJOR_DROP_M)


def _pixel_to_world(gt, col: float, row: float) -> tuple[float, float]:
    x = gt[0] + col * gt[1] + row * gt[2]
    y = gt[3] + col * gt[4] + row * gt[5]
    return x, y


def detect_cliff_ticks(
    elev,
    gt,
    *,
    min_drop_m: float = 1.4,
    major_drop_m: float = 2.8,
    nodata=None,
    stride: int = SAMPLE_STRIDE_CELLS,
    max_ticks: int = MAX_TICKS,
) -> tuple[list[tuple[tuple[float, float], tuple[float, float]]], list[tuple[tuple[float, float], tuple[float, float]]]]:
    """Vrátí (malé_ticky, velké_ticky) jako 2bodové úsečky kolmo na spád.

    Lokální schod = max(center − 4-soused) očištěný o hrubý trend ze vzdálenější
    buňky (stejný princip jako ``cliff_height.measure_drop``, zjednodušeně
    po rastru).
    """
    import numpy as np

    arr = np.asarray(elev, dtype=np.float32)
    h, w = arr.shape
    if h < 5 or w < 5:
        return [], []

    valid = np.isfinite(arr)
    if nodata is not None:
        valid &= arr != float(nodata)

    # Pixel size (S-JTSK: gt[1] > 0, gt[5] < 0 typicky).
    px = abs(float(gt[1])) or 1.0
    py = abs(float(gt[5])) or 1.0
    # Sonda ~ 2–3 m v pixelech.
    near = max(1, int(round(2.5 / min(px, py))))
    far = max(near + 1, int(round(7.5 / min(px, py))))

    small: list[tuple[tuple[float, float], tuple[float, float]]] = []
    large: list[tuple[tuple[float, float], tuple[float, float]]] = []
    stride = max(1, int(stride))

    for r in range(far, h - far, stride):
        for c in range(far, w - far, stride):
            if not valid[r, c]:
                continue
            z0 = float(arr[r, c])
            # Gradient in world units (X east / Y north). gt[5] usually < 0.
            z_e = float(arr[r, c + near]) if valid[r, c + near] else z0
            z_w = float(arr[r, c - near]) if valid[r, c - near] else z0
            z_s = float(arr[r + near, c]) if valid[r + near, c] else z0
            z_n = float(arr[r - near, c]) if valid[r - near, c] else z0
            gx = (z_e - z_w) / (2.0 * near * px)  # dZ/dX
            # Row+ is south when gt[5] < 0 → dZ/dY_north = -(z_s - z_n)/(2*near*py)
            gy = -(z_s - z_n) / (2.0 * near * py)
            gmag = math.hypot(gx, gy)
            if gmag < 1e-6:
                continue
            ux, uy = gx / gmag, gy / gmag  # unit downhill in world XY
            # Pixel step along downhill (col east, row south when gt[5]<0).
            dcol = ux
            drow = -uy if gt[5] < 0 else uy
            plen = math.hypot(dcol, drow)
            if plen < 1e-9:
                continue
            dcol /= plen
            drow /= plen

            def _at(dist_px: float) -> float | None:
                cc = int(round(c + dcol * dist_px))
                rr = int(round(r + drow * dist_px))
                if rr < 0 or rr >= h or cc < 0 or cc >= w or not valid[rr, cc]:
                    return None
                return float(arr[rr, cc])

            near_lo = _at(-near)
            near_hi = _at(near)
            far_lo = _at(-far)
            far_hi = _at(far)
            if None in (near_lo, near_hi, far_lo, far_hi):
                continue
            step_m = min(px, py)
            span_m = (far - near) * step_m
            if span_m <= 0:
                continue
            grade = ((near_lo - far_lo) + (far_hi - near_hi)) / (2.0 * span_m)
            expected = grade * 2.0 * near * step_m
            drop = abs((near_hi - near_lo) - expected)
            if drop < min_drop_m:
                continue

            # Tick perpendicular to downhill (along contour / cliff face).
            nx, ny = -uy, ux
            nlen = math.hypot(nx, ny)
            if nlen < 1e-9:
                continue
            nx /= nlen
            ny /= nlen
            cx, cy = _pixel_to_world(gt, c + 0.5, r + 0.5)
            a = (cx - nx * TICK_HALF_LEN_M, cy - ny * TICK_HALF_LEN_M)
            b = (cx + nx * TICK_HALF_LEN_M, cy + ny * TICK_HALF_LEN_M)
            if drop >= major_drop_m:
                large.append((a, b))
            else:
                small.append((a, b))
            if len(small) + len(large) >= max_ticks:
                return small, large

    return small, large


def write_cliff_ticks_dxf(
    ticks: list[tuple[tuple[float, float], tuple[float, float]]],
    dest: Path,
) -> Path | None:
    """Minimální ASCII DXF (LINE) – čte OGR / pyogrio v oom_import."""
    if not ticks:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "0",
        "SECTION",
        "2",
        "HEADER",
        "0",
        "ENDSEC",
        "0",
        "SECTION",
        "2",
        "ENTITIES",
    ]
    for (x1, y1), (x2, y2) in ticks:
        lines.extend(
            [
                "0",
                "LINE",
                "8",
                "CLIFF",
                "10",
                f"{x1:.3f}",
                "20",
                f"{y1:.3f}",
                "30",
                "0.0",
                "11",
                f"{x2:.3f}",
                "21",
                f"{y2:.3f}",
                "31",
                "0.0",
            ]
        )
    lines.extend(["0", "ENDSEC", "0", "EOF", ""])
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest if dest.is_file() else None


def generate_cliffs_from_dem(
    dem_tif: Path,
    temp_dir: Path,
    *,
    min_drop_m: float = 1.4,
    major_drop_m: float = 2.8,
    log=None,
) -> dict[str, Path]:
    """Detekce → ``temp/c2g.dxf`` (malé) + ``temp/c3g.dxf`` (větší)."""
    try:
        from osgeo import gdal
    except ImportError:
        if log:
            log("Srázy DEM: osgeo/GDAL není k dispozici – přeskočeno")
        return {}

    gdal.UseExceptions()
    src = gdal.Open(str(dem_tif))
    if src is None:
        if log:
            log(f"Srázy DEM: nelze otevřít {dem_tif.name}")
        return {}
    band = src.GetRasterBand(1)
    arr = band.ReadAsArray()
    if arr is None:
        return {}
    nodata = band.GetNoDataValue()
    gt = src.GetGeoTransform()
    src = None

    small, large = detect_cliff_ticks(
        arr,
        gt,
        min_drop_m=min_drop_m,
        major_drop_m=major_drop_m,
        nodata=nodata,
    )
    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    # KP mapování: c2g → cliffs_small, c3g → cliffs_large.
    # Velké i malé dát do c2g, velké navíc do c3g – merge je sloučí.
    all_smallish = list(small) + list(large)
    p2 = write_cliff_ticks_dxf(all_smallish, temp_dir / "c2g.dxf")
    if p2:
        out["c2g.dxf"] = p2
    p3 = write_cliff_ticks_dxf(large, temp_dir / "c3g.dxf")
    if p3:
        out["c3g.dxf"] = p3
    if log:
        log(
            f"Srázy DEM: {len(small)} malých + {len(large)} větších ticků "
            f"(drop≥{min_drop_m:g}/{major_drop_m:g} m) → temp/"
        )
    return out


def generate_job_cliffs_dem(
    work_dir: Path,
    *,
    options: dict | None = None,
    log=None,
) -> dict[str, Path]:
    """Job fáze bez KP: kandidáti srázů do ``work/temp/`` pro OOM import."""
    work_dir = Path(work_dir)
    cliff_symbol = str((options or {}).get("kp_cliff_symbol") or "earth_bank").strip().lower()
    if cliff_symbol == "off":
        if log:
            log("Srázy DEM: vypnuto (kp_cliff_symbol=off)")
        return {}
    dem = dem_filled_path(work_dir)
    if dem is None:
        if log:
            log("Srázy DEM: chybí work/dem/dem_filled.tif – přeskočeno")
        return {}
    c1, c2 = resolve_drop_thresholds(options)
    if log:
        log("=== Fáze: srázy z DEM (bez KP) ===")
    return generate_cliffs_from_dem(
        dem,
        work_dir / "temp",
        min_drop_m=c1,
        major_drop_m=c2,
        log=log,
    )
