"""Kupky (109), ďolíky (111) a jámy (112) ze sdíleného DEM.

Vlastní detektor očištěný o sklon: prstencem kolem bodu se proloží rovina
a převýšení / hloubka se měří vůči ní – ve svahu tak horní strana prstence
kupku nezakryje. Radši méně a přesně:

- kupka jen velká (≥ 1,5 m) a široká – i v půlce poloměru ještě zvednutá,
  takže úzká hromada větví nebo shluk neprojde;
- ďolík / jáma ≥ 0,8 m; jáma = strmé stěny už v půlce poloměru;
- prstenec uzavřený ze všech stran (žlab, zářez cesty ani hřbet neprojde)
  a ne ve strmém svahu (tam hrany srázů a skal dělají falešné nálezy).

Výstup ``work/temp/dotknolls.dxf`` (109), ``dotdepressions.dxf`` (111)
a ``dotpits.dxf`` (112) jako POINT.
"""

from __future__ import annotations

import math
from pathlib import Path

from app.pipeline.cliffs_dem import dem_filled_path
from app.pipeline.dem_prep import DEM_DIR_NAME
from app.pipeline.prepare_lidar import log_step

# Poloměry prstence (m) – malý i širší tvar (ďolík ~20 m), který vrstevnice minou.
RELIEF_RADII_M = (7.0, 12.0)
RELIEF_RING_SAMPLES = 16
# Vyhlazení před detekcí (m) – tlumí šum TIN z DMR 5G.
RELIEF_SMOOTH_SIGMA_M = 1.0
# Prstenec strmější než tohle (m/m) = svah se srázy/skalami, ne kupka.
RELIEF_MAX_SLOPE = 0.3
# Celý prstenec musí být níž (kupka) / výš (ďolík) aspoň o tento podíl.
RELIEF_ENCLOSED_FRACTION = 0.5
RELIEF_MIN_SPACING_M = 12.0
KNOLL_MIN_PROMINENCE_M = 1.5
# Vyšší už patří vrstevnici, ne tečce.
KNOLL_MAX_PROMINENCE_M = 3.0
# Půlka poloměru: kupka tam musí být ještě zvednutá (široká, ne hromada větví).
KNOLL_MIN_HALF_RISE = 0.5
DEPRESSION_MIN_DEPTH_M = 0.8
DEPRESSION_MAX_DEPTH_M = 4.0
# Jáma: do PIT_WALL_RADIUS_M od středu už stěna vystoupala na tuto část hloubky.
PIT_MIN_HALF_RISE = 0.6
PIT_WALL_RADIUS_M = 3.5
# Koryto / zářez cesty: do PIT_WALL_RADIUS_M terén (bez odečtení sklonu)
# nestoupá ve dvou protilehlých směrech. Jáma ve svahu jen v jednom (dolů).
DEPRESSION_MIN_WALL_RISE = 0.25
DEPRESSION_MAX_LOW_SAMPLES = 5  # z 16 – víc už je otevřený žlab, ne miska
# Koryto / cesta v širším okolí: na vnějším prstenci (bez sklonu) jsou dva
# protilehlé směry jen málo nad dnem (podél koryta), boky vysoko. Miska je
# kolem dokola zhruba stejně vysoká.
DEPRESSION_OPPOSITE_MIN = 0.9
# Hluboká jáma (např. na hraně svahu) je spolehlivá i s mírnějšími tvarovými
# podmínkami; mělké „korálky“ v korytech a zářezech cest jsou do ~1,2 m.
DEEP_PIT_M = 1.5
DEEP_PIT_MAX_LOW_SAMPLES = 7
DEEP_PIT_OPPOSITE_MIN = 0.8
PIT_MIN_DEPTH_M = 1.0
RELIEF_MAX_POINTS = 8_000

DXF_KNOLLS = "dotknolls.dxf"
DXF_DEPRESSIONS = "dotdepressions.dxf"
DXF_PITS = "dotpits.dxf"


def _ring(radius_px: float, n: int):
    """Celočíselné posuny prstence a pseudoinverze roviny z = a + b·dx + c·dy."""
    import numpy as np

    ang = np.arange(n) * 2.0 * math.pi / n
    dc = np.rint(radius_px * np.cos(ang)).astype(int)
    dr = np.rint(radius_px * np.sin(ang)).astype(int)
    design = np.column_stack([np.ones(n), dc.astype(float), dr.astype(float)])
    return dr, dc, np.linalg.pinv(design)


def detect_relief(
    elev,
    gt,
    *,
    nodata=None,
    max_points: int = RELIEF_MAX_POINTS,
) -> dict[str, list[tuple[float, float]]]:
    """Body (x, y) – klíče ``knolls`` (109), ``depressions`` (111), ``pits`` (112)."""
    import numpy as np
    from scipy import ndimage

    arr = np.asarray(elev, dtype=np.float32)
    px = abs(float(gt[1])) or 1.0
    out: dict[str, list[tuple[float, float]]] = {"knolls": [], "depressions": [], "pits": []}
    valid = np.isfinite(arr)
    if nodata is not None:
        valid &= arr != float(nodata)
    if not valid.any():
        return out
    filled = np.where(valid, arr, np.float32(np.nanmedian(arr[valid])))
    zs = ndimage.gaussian_filter(filled, RELIEF_SMOOTH_SIGMA_M / px).astype(np.float32)
    candidates: dict[str, list[tuple[float, float, float]]] = {k: [] for k in out}
    for radius_m in RELIEF_RADII_M:
        _relief_candidates(zs, valid, gt, radius_m / px, candidates)
    out["knolls"] = _pick_spaced(
        candidates["knolls"], min_spacing_m=RELIEF_MIN_SPACING_M, max_points=max_points
    )
    # Ďolík i jáma jsou tentýž tvar z různých poloměrů → jeden rozestup, druh
    # podle nejhlubšího nálezu.
    kind_at = {(x, y): k for k in ("depressions", "pits") for _p, x, y in candidates[k]}
    for x, y in _pick_spaced(
        candidates["depressions"] + candidates["pits"],
        min_spacing_m=RELIEF_MIN_SPACING_M,
        max_points=max_points,
    ):
        out[kind_at[(x, y)]].append((x, y))
    return out


def _relief_candidates(zs, valid, gt, radius: float, candidates) -> None:
    """Kandidáti pro jeden poloměr prstence (v px) → ``candidates[kind]``."""
    import numpy as np
    from scipy import ndimage

    px = abs(float(gt[1])) or 1.0
    h, w = zs.shape
    pad = int(math.ceil(radius)) + 1
    if h <= 2 * pad + 2 or w <= 2 * pad + 2:
        return
    zp = np.pad(zs, pad, mode="edge")
    dr, dc, pinv = _ring(radius, RELIEF_RING_SAMPLES)
    hdr, hdc, _ = _ring(radius / 2.0, RELIEF_RING_SAMPLES)
    wdr, wdc, _ = _ring(PIT_WALL_RADIUS_M / px, RELIEF_RING_SAMPLES)
    ring_mean = np.zeros_like(zs)
    for r_off, c_off in zip(dr, dc):
        ring_mean += zp[pad + r_off : pad + r_off + h, pad + c_off : pad + c_off + w]
    ring_mean /= len(dr)
    rel = zs - ring_mean
    # Bez hran rastru (doplněný okraj by vypadal jako uzavřený) a NoData v okolí.
    ok_area = ndimage.minimum_filter(valid.astype(np.uint8), size=2 * pad + 1) == 1
    ok_area[:pad, :] = ok_area[-pad:, :] = False
    ok_area[:, :pad] = ok_area[:, -pad:] = False
    win = 2 * int(round(radius)) + 1

    for sign, lo, hi in (
        (1.0, KNOLL_MIN_PROMINENCE_M, KNOLL_MAX_PROMINENCE_M),
        (-1.0, DEPRESSION_MIN_DEPTH_M, DEPRESSION_MAX_DEPTH_M),
    ):
        r = sign * rel
        peak = (r == ndimage.maximum_filter(r, size=win)) & (r >= lo) & (r <= hi) & ok_area
        rows, cols = np.nonzero(peak)
        if rows.size == 0:
            continue
        samp = zp[rows[:, None] + pad + dr[None, :], cols[:, None] + pad + dc[None, :]]
        half = zp[rows[:, None] + pad + hdr[None, :], cols[:, None] + pad + hdc[None, :]]
        coef = samp.astype(np.float64) @ pinv.T  # a, b, c (na pixel)
        a, b, c = coef[:, 0:1], coef[:, 1:2], coef[:, 2:3]
        center = zs[rows, cols].astype(np.float64)[:, None]
        prom = sign * (center - a)  # převýšení / hloubka vůči rovině
        detr = samp - (a + b * dc[None, :] + c * dr[None, :])
        enclosed = ((prom - sign * detr) >= RELIEF_ENCLOSED_FRACTION * prom).all(axis=1)
        flat_enough = np.hypot(b[:, 0], c[:, 0]) / px <= RELIEF_MAX_SLOPE
        # Výška půlkruhu nad rovinou (kupka) / nad dnem (jáma), podíl z převýšení.
        half_plane = a + b * hdc[None, :] + c * hdr[None, :]
        p = prom[:, 0]
        keep = enclosed & flat_enough & (lo <= p) & (p <= hi)
        if sign > 0:
            half_rise = (half - half_plane).mean(axis=1) / np.maximum(p, 1e-6)
            keep &= half_rise >= KNOLL_MIN_HALF_RISE
            kinds = np.full(p.shape, "knolls", dtype=object)
        else:
            near = zp[rows[:, None] + pad + wdr[None, :], cols[:, None] + pad + wdc[None, :]]
            rise = (near - center) / np.maximum(p, 1e-6)[:, None]
            low = rise < DEPRESSION_MIN_WALL_RISE
            n = low.shape[1]
            opposite = np.zeros(low.shape[0], dtype=bool)
            for shift in (n // 2 - 1, n // 2, n // 2 + 1):
                opposite |= (low & np.roll(low, -shift, axis=1)).any(axis=1)
            deep = p >= DEEP_PIT_M
            max_low = np.where(deep, DEEP_PIT_MAX_LOW_SAMPLES, DEPRESSION_MAX_LOW_SAMPLES)
            keep &= ~opposite & (low.sum(axis=1) <= max_low)
            # Výška vnějšího prstence nad dnem (bez sklonu) jako podíl hloubky.
            outer = (prom + detr) / np.maximum(prom, 1e-6)
            half_n = outer.shape[1] // 2
            pair = np.maximum(outer[:, :half_n], outer[:, half_n:])
            min_pair = np.where(deep, DEEP_PIT_OPPOSITE_MIN, DEPRESSION_OPPOSITE_MIN)
            keep &= pair.min(axis=1) >= min_pair
            wall = rise.mean(axis=1)
            steep = (wall >= PIT_MIN_HALF_RISE) & (p >= PIT_MIN_DEPTH_M)
            kinds = np.where(steep, "pits", "depressions").astype(object)
        for i in np.nonzero(keep)[0]:
            x = gt[0] + (cols[i] + 0.5) * gt[1] + (rows[i] + 0.5) * gt[2]
            y = gt[3] + (cols[i] + 0.5) * gt[4] + (rows[i] + 0.5) * gt[5]
            candidates[kinds[i]].append((float(p[i]), float(x), float(y)))


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


def write_knoll_points_dxf(
    points: list[tuple[float, float]],
    dest: Path,
    *,
    layer: str = "KNOLL",
) -> Path | None:
    """ASCII DXF POINT — čte OGR / pyogrio jako bodové značky."""
    dest.unlink(missing_ok=True)
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
                layer,
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
    log=None,
) -> Path | None:
    from app.pipeline.gdal_cli_raster import read_float32_geotiff

    log_step(log, "Hledám kupky, ďolíky a jámy na DEM (jen výrazné)")
    try:
        arr, gt, nodata = read_float32_geotiff(dem_tif, log=log)
    except Exception as exc:
        if log:
            log(f"Kupky/ďolíky DEM: nelze číst {dem_tif.name} ({exc}) – přeskočeno")
        return None
    found = detect_relief(arr, gt, nodata=nodata)
    temp_dir = Path(temp_dir)
    dest = write_knoll_points_dxf(found["knolls"], temp_dir / DXF_KNOLLS)
    write_knoll_points_dxf(found["depressions"], temp_dir / DXF_DEPRESSIONS, layer="DEPRESSION")
    write_knoll_points_dxf(found["pits"], temp_dir / DXF_PITS, layer="PIT")
    if log:
        log(
            f"Kupky/ďolíky DEM: {len(found['knolls'])} kupek 109 "
            f"(≥{KNOLL_MIN_PROMINENCE_M:g} m), {len(found['depressions'])} ďolíků 111, "
            f"{len(found['pits'])} jam 112 (≥{DEPRESSION_MIN_DEPTH_M:g} m) → temp/"
        )
    return dest


def generate_job_knolls(
    work_dir: Path,
    *,
    options: dict | None = None,
    log=None,
) -> Path | None:
    """Kupky + ďolíky do ``work/temp/``. ``include_knolls=false`` = nic."""
    opts = options or {}
    work_dir = Path(work_dir)
    if opts.get("include_knolls", True) is False:
        for name in (DXF_KNOLLS, DXF_DEPRESSIONS, DXF_PITS):
            (work_dir / "temp" / name).unlink(missing_ok=True)
        if log:
            log("Kupky/ďolíky DEM: vypnuto (include_knolls=false)")
        return None
    dem = dem_filled_path(work_dir)
    if dem is None:
        alt = work_dir / DEM_DIR_NAME / "dem_filled.tif"
        if not (alt.is_file() and alt.stat().st_size > 500):
            if log:
                log("Kupky/ďolíky DEM: chybí work/dem/dem_filled.tif – přeskočeno")
            return None
        dem = alt
    if log:
        log("=== Fáze: kupky a ďolíky z DEM ===")
    return generate_knolls_from_dem(dem, work_dir / "temp", log=log)
