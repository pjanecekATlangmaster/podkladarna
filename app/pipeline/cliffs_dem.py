"""Kandidáti srázů ze sdíleného DEM.

Počítá lokální schod na ``work/dem/dem_filled.tif`` (stejná mřížka jako
shade/kontury). Výstup: tick DXF do ``work/temp/c2g.dxf``
(+ ``c3g.dxf`` pro větší schody), ať ``cliff_merge`` + ``cliff_height``
a OOM ``build_dxf_object_part`` zůstanou beze změny.

Skála (plocha 201.2/206) vs. zem (104) se rozhoduje sklonem schodu, ne jen
volbou ve formuláři. ``auto`` nechá obě třídy; ``earth_bank`` /
``symbol_206`` / ``off`` pořád přebijí všechno. Citlivost mapuje
``kp_cliff_sensitivity`` na prahy výšky.
"""

from __future__ import annotations

import math
from pathlib import Path

from app.pipeline.cliff_height import MAJOR_DROP_M, MIN_DROP_M
from app.pipeline.dem_prep import DEM_DIR_NAME
from app.pipeline.prepare_lidar import log_step
from app.pipeline.job_options import (
    KP_CLIFF_SENSITIVITY,
    KP_CLIFF_SENSITIVITY_DEFAULT,
    resolve_cliff_sensitivity,
)

# Délka ticku ~ 3 m buňka (merge_cliff_ticks očekává krátké úsečky).
TICK_HALF_LEN_M = 1.45
# Krok vzorkování kandidátů po rastru (m) – podmnožina buněk.
SAMPLE_STRIDE_CELLS = 2
# Max. počet ticků na AOI (ochrana RAM/OOM).
MAX_TICKS = 80_000
# drop / vodorovná délka sondy. Nad tím skála (krátká stěna), pod tím zemní sráz.
ROCK_MIN_GRADE = 0.55
# Skála potřebuje vyšší schod než zemní sráz při stejné citlivosti GUI
# (kp_cliff_sensitivity / cliff1). Slabé strmé kandidáty se zahodí – nepřelévají
# se do 104, aby srázy zůstaly beze změny.
# 1.23.2/1.25.0 = 0.7 (málo skal); 1.25.4 mírně níž – víc 201.2, ne flood.
ROCK_MIN_DROP_BONUS_M = 0.5


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
    min_drop_m: float = 1.8,
    major_drop_m: float = 3.4,
    nodata=None,
    stride: int = SAMPLE_STRIDE_CELLS,
    max_ticks: int = MAX_TICKS,
) -> tuple[list[tuple[tuple[float, float], tuple[float, float]]], list[tuple[tuple[float, float], tuple[float, float]]]]:
    """Vrátí (zemní_ticky, skalní_ticky) jako 2bodové úsečky kolmo na spád.

    Schod je výškový skok očištěný o hrubý trend okolí. Skála = strmý skok
    na krátké vzdálenosti (``ROCK_MIN_GRADE``) **a** vyšší drop
    (``min_drop_m + ROCK_MIN_DROP_BONUS_M``); nižší sklon při stejném prahu
    výšky je zemní sráz. Strmé ale nízké kandidáty se zahodí (ne → 104).
    ``major_drop_m`` zůstává v signatuře kvůli volajícím.
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
    rock_min_drop = float(min_drop_m) + ROCK_MIN_DROP_BONUS_M

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
            run_m = 2.0 * near * step_m
            grade = drop / run_m if run_m > 0 else 0.0
            # major_drop_m: vysoký schod, který je pořád dost strmý, ber jako skálu.
            steep_enough = grade >= ROCK_MIN_GRADE or (
                drop >= major_drop_m and grade >= ROCK_MIN_GRADE * 0.75
            )

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
            if steep_enough:
                # Přísnější práh jen pro skálu; slabé strmé → pryč (ne 104).
                if drop >= rock_min_drop:
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
    """Minimální ASCII DXF (LINE) – pár k ``parse_cliff_ticks_dxf``."""
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


def parse_cliff_ticks_dxf(
    path: Path,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Načte LINE entity z našeho minimálního cliff DXF (bez OGR/GDAL).

    GDAL DXF driver na Dockeru často vrátí 0 vrstev u ASCII jen s HEADER+ENTITIES
    (bez TABLES) → 457/1859 ticků v temp/ se do merge vůbec nedostane → 0×201.2.
    Tento parser je zdrojem pravdy pro formát z ``write_cliff_ticks_dxf``.
    """
    path = Path(path)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    lines = text.splitlines()
    ticks: list[tuple[tuple[float, float], tuple[float, float]]] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip() != "LINE":
            i += 1
            continue
        vals: dict[str, str] = {}
        i += 1
        while i < n and lines[i].strip() != "0":
            code = lines[i].strip()
            i += 1
            if i >= n:
                break
            vals[code] = lines[i].strip()
            i += 1
        try:
            if {"10", "20", "11", "21"} <= vals.keys():
                ticks.append(
                    (
                        (float(vals["10"]), float(vals["20"])),
                        (float(vals["11"]), float(vals["21"])),
                    )
                )
        except ValueError:
            continue
    return ticks


def generate_cliffs_from_dem(
    dem_tif: Path,
    temp_dir: Path,
    *,
    min_drop_m: float = 1.8,
    major_drop_m: float = 3.4,
    log=None,
) -> dict[str, Path]:
    """Detekce → ``temp/c2g.dxf`` (zem) + ``temp/c_rock.dxf`` (skála)."""
    from app.pipeline.gdal_cli_raster import read_float32_geotiff

    log_step(
        log,
        f"Hledám srázy na DEM (drop≥{min_drop_m:g} m, skála≥{min_drop_m + ROCK_MIN_DROP_BONUS_M:g} m "
        f"/ sklon≥{ROCK_MIN_GRADE:g}; krátké ticky a plochy řeší merge v OOM)",
    )
    try:
        arr, gt, nodata = read_float32_geotiff(dem_tif, log=log)
    except Exception as exc:
        if log:
            log(f"Srázy DEM: nelze číst {dem_tif.name} ({exc}) – přeskočeno")
        return {}

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
    # c2g → zem (104). c_rock → skála (201). Neslévej je do jednoho DXF.
    p_earth = write_cliff_ticks_dxf(small, temp_dir / "c2g.dxf")
    if p_earth:
        out["c2g.dxf"] = p_earth
    p_rock = write_cliff_ticks_dxf(large, temp_dir / "c_rock.dxf")
    if p_rock:
        out["c_rock.dxf"] = p_rock
    if log:
        log(
            f"Srázy DEM: {len(small)} zemních + {len(large)} skalních ticků "
            f"(zem drop≥{min_drop_m:g} m / skála≥{min_drop_m + ROCK_MIN_DROP_BONUS_M:g} m, "
            f"major≥{major_drop_m:g} m, sklon≥{ROCK_MIN_GRADE:g}; "
            f"body 204 z šumu DEM nevznikají) → temp/"
        )
    return out


def generate_job_cliffs_dem(
    work_dir: Path,
    *,
    options: dict | None = None,
    log=None,
) -> dict[str, Path]:
    """Job fáze: kandidáti srázů do ``work/temp/`` pro OOM import."""
    work_dir = Path(work_dir)
    cliff_symbol = str((options or {}).get("kp_cliff_symbol") or "auto").strip().lower()
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
    sens = resolve_cliff_sensitivity(options)
    if log:
        log("=== Fáze: srázy z DEM ===")
        log(
            f"Srázy DEM: citlivost={sens} → cliff1={c1:g} m, cliff2={c2:g} m "
            f"(méně citlivé = méně falešných zemních srázů)"
        )
    return generate_cliffs_from_dem(
        dem,
        work_dir / "temp",
        min_drop_m=c1,
        major_drop_m=c2,
        log=log,
    )
