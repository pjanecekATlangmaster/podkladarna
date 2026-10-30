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

from pathlib import Path

from app.pipeline.cliff_height import MAJOR_DROP_M, MIN_DROP_M
from app.pipeline.dem_prep import DEM_DIR_NAME
from app.pipeline.prepare_lidar import log_step
from app.pipeline.job_progress import log_issue
from app.pipeline.job_options import (
    KP_CLIFF_SENSITIVITY,
    KP_CLIFF_SENSITIVITY_DEFAULT,
    resolve_cliff_sensitivity,
)

# Délka ticku ~ 3 m buňka (merge_cliff_ticks očekává krátké úsečky).
TICK_HALF_LEN_M = 1.45
# Krok vzorkování kandidátů po rastru (m) – podmnožina buněk.
SAMPLE_STRIDE_CELLS = 2
# Max. počet ticků na AOI (ochrana RAM/OOM). Řádky jdou od severu, takže po
# dosažení stropu jih mapy srázy nedostane – škáluje s limitem plochy
# (80k pro 25 km² → 120k pro 36 km², ~3 300 ticků/km²; běžný terén ~300).
MAX_TICKS = 120_000
# drop / vodorovná délka sondy. Nad tím skála (krátká stěna), pod tím zemní sráz.
ROCK_MIN_GRADE = 0.55
# Skála potřebuje vyšší schod než zemní sráz při stejné citlivosti GUI
# (kp_cliff_sensitivity / cliff1). Slabé strmé kandidáty se zahodí – nepřelévají
# se do 104, aby srázy zůstaly beze změny.
# 1.23.2/1.25.0 = 0.7 (málo skal); 1.25.4 mírně níž – víc 201.2, ne flood.
ROCK_MIN_DROP_BONUS_M = 0.5
# Sprint: sráz jen jako skutečný skok – terén po obou stranách sondy (near…far) musí být výrazně méně strmý
# než samotný schod (sklon strany ≤ poměr × sklon schodu). Na souvislém strmém svahu (břehy řek) jinak vznikaly
# řady zemních srázů nad sebou; svah tam ukážou vrstevnice (Č. Krumlov, Petr 2026-10-26).
SPRINT_STEP_SIDE_RATIO = 0.5


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


def detect_cliff_ticks(
    elev,
    gt,
    *,
    min_drop_m: float = 1.8,
    major_drop_m: float = 3.4,
    nodata=None,
    stride: int = SAMPLE_STRIDE_CELLS,
    max_ticks: int = MAX_TICKS,
    step_side_ratio: float | None = None,
) -> tuple[list[tuple[tuple[float, float], tuple[float, float]]], list[tuple[tuple[float, float], tuple[float, float]]]]:
    """Vrátí (zemní_ticky, skalní_ticky) jako 2bodové úsečky kolmo na spád.

    Schod je výškový skok očištěný o hrubý trend okolí. Skála = strmý skok
    na krátké vzdálenosti (``ROCK_MIN_GRADE``) **a** vyšší drop
    (``min_drop_m + ROCK_MIN_DROP_BONUS_M``); nižší sklon při stejném prahu
    výšky je zemní sráz. Strmé ale nízké kandidáty se zahodí (ne → 104).
    ``major_drop_m`` zůstává v signatuře kvůli volajícím. ``step_side_ratio`` (sprint): sklon terénu po obou
    stranách schodu (mezi ``near`` a ``far``) smí být nejvýš tolikrát sklon schodu, jinak jde o svah, ne sráz.
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

    stride = max(1, int(stride))
    return _detect_cliff_ticks_np(
        arr,
        valid,
        gt,
        near=near,
        far=far,
        px=px,
        py=py,
        min_drop_m=float(min_drop_m),
        major_drop_m=float(major_drop_m),
        rock_min_drop=rock_min_drop,
        stride=stride,
        max_ticks=int(max_ticks),
        step_side_ratio=step_side_ratio,
    )


def _detect_cliff_ticks_np(
    arr,
    valid,
    gt,
    *,
    near: int,
    far: int,
    px: float,
    py: float,
    min_drop_m: float,
    major_drop_m: float,
    rock_min_drop: float,
    stride: int,
    max_ticks: int,
    rows_per_block: int = 256,
    step_side_ratio: float | None = None,
):
    """Vektorová verze referenční smyčky (viz ``tests/test_cliffs_dem.py``) – stejné ticky i pořadí.

    Stejné vzorkování (stride), stejné float64 výrazy ve stejném pořadí a
    ``np.rint`` = Python ``round`` (polovina k sudé). Po blocích řádků kvůli RAM.
    """
    import numpy as np

    h, w = arr.shape
    a64 = arr.astype(np.float64)
    small: list[tuple[tuple[float, float], tuple[float, float]]] = []
    large: list[tuple[tuple[float, float], tuple[float, float]]] = []
    step_m = min(px, py)
    span_m = (far - near) * step_m
    if span_m <= 0:
        return small, large
    run_m = 2.0 * near * step_m
    rows_all = np.arange(far, h - far, stride)
    cols = np.arange(far, w - far, stride)
    if rows_all.size == 0 or cols.size == 0:
        return small, large
    total = 0
    for b0 in range(0, rows_all.size, max(1, rows_per_block)):
        rows = rows_all[b0 : b0 + rows_per_block]
        R, C = np.meshgrid(rows, cols, indexing="ij")
        R = R.ravel()
        C = C.ravel()
        keep = valid[R, C]
        R, C = R[keep], C[keep]
        if R.size == 0:
            continue
        z0 = a64[R, C]

        def _nb(dr: int, dc: int):
            return np.where(valid[R + dr, C + dc], a64[R + dr, C + dc], z0)

        z_e = _nb(0, near)
        z_w = _nb(0, -near)
        z_s = _nb(near, 0)
        z_n = _nb(-near, 0)
        gx = (z_e - z_w) / (2.0 * near * px)
        gy = -(z_s - z_n) / (2.0 * near * py)
        gmag = np.hypot(gx, gy)
        ok = gmag >= 1e-6
        with np.errstate(divide="ignore", invalid="ignore"):
            ux = gx / gmag
            uy = gy / gmag
            dcol = ux
            drow = -uy if gt[5] < 0 else uy
            plen = np.hypot(dcol, drow)
            ok &= plen >= 1e-9
            dcol = dcol / plen
            drow = drow / plen
        Cf = C.astype(np.float64)
        Rf = R.astype(np.float64)

        def _at(dist_px: int):
            cc = np.rint(Cf + dcol * dist_px)
            rr = np.rint(Rf + drow * dist_px)
            inb = (rr >= 0) & (rr < h) & (cc >= 0) & (cc < w) & np.isfinite(cc) & np.isfinite(rr)
            ci = np.where(inb, cc, 0).astype(np.int64)
            ri = np.where(inb, rr, 0).astype(np.int64)
            good = inb & valid[ri, ci]
            return a64[ri, ci], good

        near_lo, g1 = _at(-near)
        near_hi, g2 = _at(near)
        far_lo, g3 = _at(-far)
        far_hi, g4 = _at(far)
        ok &= g1 & g2 & g3 & g4
        grade = ((near_lo - far_lo) + (far_hi - near_hi)) / (2.0 * span_m)
        expected = grade * 2.0 * near * step_m
        drop = np.abs((near_hi - near_lo) - expected)
        ok &= drop >= min_drop_m
        if step_side_ratio is not None:
            step_grade = np.abs(near_hi - near_lo) / run_m
            side = np.maximum(np.abs(near_lo - far_lo), np.abs(far_hi - near_hi)) / span_m
            ok &= side <= step_side_ratio * step_grade
        grade2 = drop / run_m if run_m > 0 else np.zeros_like(drop)
        steep = (grade2 >= ROCK_MIN_GRADE) | (
            (drop >= major_drop_m) & (grade2 >= ROCK_MIN_GRADE * 0.75)
        )
        nx = -uy
        ny = ux
        with np.errstate(divide="ignore", invalid="ignore"):
            nlen = np.hypot(nx, ny)
            ok &= nlen >= 1e-9
            nx = nx / nlen
            ny = ny / nlen
        appended = ok & ((steep & (drop >= rock_min_drop)) | ~steep)
        idx = np.nonzero(appended)[0]
        if idx.size == 0:
            continue
        idx = idx[: max(0, max_ticks - total)]
        col_c = C[idx] + 0.5
        row_c = R[idx] + 0.5
        cx = gt[0] + col_c * gt[1] + row_c * gt[2]
        cy = gt[3] + col_c * gt[4] + row_c * gt[5]
        tx = nx[idx] * TICK_HALF_LEN_M
        ty = ny[idx] * TICK_HALF_LEN_M
        ax, ay = (cx - tx).tolist(), (cy - ty).tolist()
        bx, by = (cx + tx).tolist(), (cy + ty).tolist()
        is_large = steep[idx].tolist()
        for i, lg in enumerate(is_large):
            seg = ((ax[i], ay[i]), (bx[i], by[i]))
            (large if lg else small).append(seg)
        total += idx.size
        if total >= max_ticks:
            break
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


# Sprint: okolí ticků z detekce (m), ve kterém cizí data (ZABAGED terénní stupeň) skok potvrzují.
STEP_CONFIRM_BUFFER_M = 3.0


def dem_step_zone(work_dir: Path, buffer_m: float = STEP_CONFIRM_BUFFER_M):
    """Okolí ticků srázů z DEM (``temp/c2g.dxf`` + ``temp/c_rock.dxf``) jako shapely plocha; None bez souborů
    detekce (job bez srázů), prázdná plocha, když detekce nic nenašla."""
    temp = Path(work_dir) / "temp"
    files = [temp / "c2g.dxf", temp / "c_rock.dxf"]
    if not any(f.is_file() for f in files):
        return None
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union

    segs = [LineString([a, b]) for f in files for a, b in parse_cliff_ticks_dxf(f)]
    return unary_union(segs).buffer(buffer_m) if segs else Polygon()


def generate_cliffs_from_dem(
    dem_tif: Path,
    temp_dir: Path,
    *,
    min_drop_m: float = 1.8,
    major_drop_m: float = 3.4,
    step_side_ratio: float | None = None,
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
        step_side_ratio=step_side_ratio,
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
    sprint: bool = False,
    log=None,
) -> dict[str, Path]:
    """Job fáze: kandidáti srázů do ``work/temp/`` pro OOM import. ``sprint``: jen skutečné skoky
    (``SPRINT_STEP_SIDE_RATIO``), souvislý strmý svah ukážou vrstevnice."""
    work_dir = Path(work_dir)
    cliff_symbol = str((options or {}).get("kp_cliff_symbol") or "auto").strip().lower()
    if cliff_symbol == "off":
        if log:
            log("Srázy: vypnuto ve formuláři")
        return {}
    dem = dem_filled_path(work_dir)
    if dem is None:
        if log:
            log_issue(log, "Srázy: chybí DEM (dem_filled.tif) – přeskočeno")
        return {}
    c1, c2 = resolve_drop_thresholds(options)
    sens = resolve_cliff_sensitivity(options)
    if log:
        log(f"Citlivost {sens}: cliff1={c1:g} m, cliff2={c2:g} m")
    return generate_cliffs_from_dem(
        dem,
        work_dir / "temp",
        min_drop_m=c1,
        major_drop_m=c2,
        step_side_ratio=SPRINT_STEP_SIDE_RATIO if sprint else None,
        log=log,
    )
