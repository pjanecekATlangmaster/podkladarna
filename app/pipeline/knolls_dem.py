"""Malé kupky (ISOM 109) ze sdíleného DEM.

Vlastní detektor. Parametr ``knolls`` (0–1, výš = přísnější): výrazný lokální
vrchol, který už není kopec na vrstevnici.

Výstup ``work/temp/dotknolls.dxf`` (POINT) — OOM ho bere jako 109.
"""

from __future__ import annotations

import math
from pathlib import Path

from app.pipeline.cliffs_dem import dem_filled_path
from app.pipeline.dem_prep import DEM_DIR_NAME
from app.pipeline.prepare_lidar import log_step

# Poloměr, ve kterém hledáme uzavřený vrchol (m). ISOM 109 je malá kupka.
KNOLL_RADIUS_M = 8.0
# Minimální rozestup dvou teček.
KNOLL_MIN_SPACING_M = 14.0
# Vyšší prominence už patří vrstevnici, ne tečce.
KNOLL_MAX_PROMINENCE_M = 2.8
KNOLL_FACTOR_DEFAULT = 0.6


def knoll_min_prominence(factor: float = KNOLL_FACTOR_DEFAULT) -> float:
    """Výška nad prstencem (m). 0 → ~0,45 m, 0,6 → ~1,1 m, 1 → ~1,55 m."""
    f = min(1.0, max(0.0, float(factor)))
    return 0.45 + f * 1.1


def _pixel_to_world(gt, col: float, row: float) -> tuple[float, float]:
    x = gt[0] + col * gt[1] + row * gt[2]
    y = gt[3] + col * gt[4] + row * gt[5]
    return x, y


def detect_knolls(
    elev,
    gt,
    *,
    factor: float = KNOLL_FACTOR_DEFAULT,
    radius_m: float = KNOLL_RADIUS_M,
    min_spacing_m: float = KNOLL_MIN_SPACING_M,
    max_prominence_m: float = KNOLL_MAX_PROMINENCE_M,
    nodata=None,
    max_points: int = 8_000,
) -> list[tuple[float, float]]:
    """Body (x, y) lokálních kupek. Prázdný seznam na plochém DEM."""
    import numpy as np

    arr = np.asarray(elev, dtype=np.float32)
    h, w = arr.shape
    if h < 8 or w < 8:
        return []
    valid = np.isfinite(arr)
    if nodata is not None:
        valid &= arr != float(nodata)
    px = abs(float(gt[1])) or 1.0
    py = abs(float(gt[5])) or 1.0
    rad = max(2, int(round(radius_m / min(px, py))))
    if h <= 2 * rad + 2 or w <= 2 * rad + 2:
        return []
    min_prom = knoll_min_prominence(factor)
    stride = max(1, rad // 2)
    try:
        candidates = _knoll_candidates_np(
            arr, valid, gt, rad=rad, stride=stride, min_prom=min_prom,
            max_prominence_m=max_prominence_m, limit=max_points * 4,
        )
    except ImportError:
        candidates = _knoll_candidates_loop(
            arr, valid, gt, rad=rad, stride=stride, min_prom=min_prom,
            max_prominence_m=max_prominence_m, limit=max_points * 4,
        )
    return _pick_spaced(candidates, min_spacing_m=min_spacing_m, max_points=max_points)


def _knoll_candidates_np(
    arr, valid, gt, *, rad: int, stride: int, min_prom: float,
    max_prominence_m: float, limit: int,
) -> list[tuple[float, float, float]]:
    """Vektorově (posuvná maxima scipy) – stejné kandidáty i pořadí jako smyčka."""
    import numpy as np
    from scipy import ndimage

    h, w = arr.shape
    size = 2 * rad + 1
    a64 = arr.astype(np.float64)
    win_max = ndimage.maximum_filter(arr, size=size, mode="nearest")
    all_valid = ndimage.minimum_filter(valid.astype(np.uint8), size=size, mode="nearest")
    hmax = ndimage.maximum_filter1d(arr, size=size, axis=1, mode="nearest")
    vmax = ndimage.maximum_filter1d(arr, size=size, axis=0, mode="nearest")

    rows = np.arange(rad, h - rad, stride)
    cols = np.arange(rad, w - rad, stride)
    R, C = np.meshgrid(rows, cols, indexing="ij")
    R = R.ravel()
    C = C.ravel()
    ok = valid[R, C] & (all_valid[R, C] == 1)
    R, C = R[ok], C[ok]
    z0 = a64[R, C]
    ok = ~(win_max[R, C].astype(np.float64) > z0 + 1e-3)
    ring_max = np.maximum.reduce(
        [
            hmax[R - rad, C].astype(np.float64),
            hmax[R + rad, C].astype(np.float64),
            vmax[R, C - rad].astype(np.float64),
            vmax[R, C + rad].astype(np.float64),
        ]
    )
    prom = z0 - ring_max
    ok &= ~((prom < min_prom) | (prom > max_prominence_m))
    thr = np.maximum(prom + 0.4, 2.2)
    for dr, dc in ((-2, 0), (2, 0), (0, -2), (0, 2)):
        rr, cc = R + dr, C + dc
        inb = (rr >= 0) & (rr < h) & (cc >= 0) & (cc < w)
        ri = np.where(inb, rr, 0)
        ci = np.where(inb, cc, 0)
        nb_ok = inb & valid[ri, ci]
        ok &= ~(nb_ok & (z0 - a64[ri, ci] > thr))
    idx = np.nonzero(ok)[0][:limit]
    col_c = C[idx] + 0.5
    row_c = R[idx] + 0.5
    xs = (gt[0] + col_c * gt[1] + row_c * gt[2]).tolist()
    ys = (gt[3] + col_c * gt[4] + row_c * gt[5]).tolist()
    return list(zip(prom[idx].tolist(), xs, ys))


def _pick_spaced(
    candidates: list[tuple[float, float, float]],
    *,
    min_spacing_m: float,
    max_points: int,
) -> list[tuple[float, float]]:
    """Nejvýraznější první; zahodí body blíž než ``min_spacing_m`` k vybraným.

    Stejný výsledek jako porovnání se všemi vybranými, jen přes mřížku buněk.
    """
    candidates = sorted(candidates, key=lambda item: item[0], reverse=True)
    kept: list[tuple[float, float]] = []
    cell = max(float(min_spacing_m), 1e-6)
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for _prom, x, y in candidates:
        gx, gy = int(math.floor(x / cell)), int(math.floor(y / cell))
        near = False
        for i in (gx - 1, gx, gx + 1):
            for j in (gy - 1, gy, gy + 1):
                for kx, ky in grid.get((i, j), ()):
                    if math.hypot(x - kx, y - ky) < min_spacing_m:
                        near = True
                        break
                if near:
                    break
            if near:
                break
        if near:
            continue
        kept.append((x, y))
        grid.setdefault((gx, gy), []).append((x, y))
        if len(kept) >= max_points:
            break
    return kept


def _knoll_candidates_loop(
    arr, valid, gt, *, rad: int, stride: int, min_prom: float,
    max_prominence_m: float, limit: int,
) -> list[tuple[float, float, float]]:
    """Původní smyčka (bez scipy; reference pro testy)."""
    h, w = arr.shape
    candidates: list[tuple[float, float, float]] = []
    max_points4 = limit

    for r in range(rad, h - rad, stride):
        for c in range(rad, w - rad, stride):
            if not valid[r, c]:
                continue
            z0 = float(arr[r, c])
            window = arr[r - rad : r + rad + 1, c - rad : c + rad + 1]
            mask = valid[r - rad : r + rad + 1, c - rad : c + rad + 1]
            if not mask.all():
                continue
            if float(window.max()) > z0 + 1e-3:
                continue
            # Prstenec = okraj okna (sedlo ven z kupky).
            ring_max = max(
                float(window[0, :].max()),
                float(window[-1, :].max()),
                float(window[:, 0].max()),
                float(window[:, -1].max()),
            )
            prom = z0 - ring_max
            if prom < min_prom or prom > max_prominence_m:
                continue
            # Hrana srázu není kupka: soused o 2 buňky níž o víc než prominence.
            cliffish = False
            for dr, dc in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                rr, cc = r + dr, c + dc
                if 0 <= rr < h and 0 <= cc < w and valid[rr, cc]:
                    if z0 - float(arr[rr, cc]) > max(prom + 0.4, 2.2):
                        cliffish = True
                        break
            if cliffish:
                continue
            x, y = _pixel_to_world(gt, c + 0.5, r + 0.5)
            candidates.append((prom, x, y))
            if len(candidates) >= max_points4:
                break
        if len(candidates) >= max_points4:
            break
    return candidates


def write_knoll_points_dxf(
    points: list[tuple[float, float]],
    dest: Path,
) -> Path | None:
    """ASCII DXF POINT — čte OGR / pyogrio jako tečky 109."""
    if not points:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = [
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
    for x, y in points:
        lines.extend(
            [
                "0",
                "POINT",
                "8",
                "KNOLL",
                "10",
                f"{x:.3f}",
                "20",
                f"{y:.3f}",
                "30",
                "0.0",
            ]
        )
    lines.extend(["0", "ENDSEC", "0", "EOF", ""])
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest if dest.is_file() else None


def generate_knolls_from_dem(
    dem_tif: Path,
    temp_dir: Path,
    *,
    factor: float = KNOLL_FACTOR_DEFAULT,
    log=None,
) -> Path | None:
    from app.pipeline.gdal_cli_raster import read_float32_geotiff

    log_step(log, "Hledám knolly na DEM (drobné kopečky, náhrada KP)")
    try:
        arr, gt, nodata = read_float32_geotiff(dem_tif, log=log)
    except Exception as exc:
        if log:
            log(f"Knolly DEM: nelze číst {dem_tif.name} ({exc}) – přeskočeno")
        return None
    pts = detect_knolls(arr, gt, factor=factor, nodata=nodata)
    dest = write_knoll_points_dxf(pts, Path(temp_dir) / "dotknolls.dxf")
    if log:
        log(
            f"Knolly DEM: {len(pts)} teček "
            f"(prominence≥{knoll_min_prominence(factor):.2f} m) → temp/dotknolls.dxf"
        )
    return dest


def generate_job_knolls(
    work_dir: Path,
    *,
    options: dict | None = None,
    log=None,
) -> Path | None:
    """Kupky do ``work/temp/dotknolls.dxf``. ``include_knolls=false`` = nic."""
    opts = options or {}
    if opts.get("include_knolls", True) is False:
        if log:
            log("Knolly DEM: vypnuto (include_knolls=false)")
        return None
    if str(opts.get("kp_cliff_symbol") or "").strip().lower() == "off":
        # Vypnutí srázů neruší kupky — jen explicitní include_knolls.
        pass
    work_dir = Path(work_dir)
    dem = dem_filled_path(work_dir)
    if dem is None:
        alt = work_dir / DEM_DIR_NAME / "dem_filled.tif"
        if not (alt.is_file() and alt.stat().st_size > 500):
            if log:
                log("Knolly DEM: chybí work/dem/dem_filled.tif – přeskočeno")
            return None
        dem = alt
    try:
        factor = float(opts.get("knoll_factor", KNOLL_FACTOR_DEFAULT))
    except (TypeError, ValueError):
        factor = KNOLL_FACTOR_DEFAULT
    if log:
        log("=== Fáze: knolly z DEM ===")
    return generate_knolls_from_dem(dem, work_dir / "temp", factor=factor, log=log)
