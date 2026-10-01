"""Vegetace bez KP z hustoty LiDAR odrazů (vlastní implementace, inspirace KP).

Proč ne CHM: běh lesem neurčuje výška koruny, ale **co je v nízkém patře**.
Karttapullautin (``makevege``) proto nepracuje s DSM−DEM, ale počítá body:

* **žlutá 401** – v okně ~6 m je podíl „nízkých“ odrazů (ground nebo
  < ``yellow_height_m`` nad terénem) > ``yellow_threshold``. Louka = skoro
  všechny body u země; jednotlivý strom v louce okno nepřeklopí.
* **zeleně 406/408/410** – v bloku ``block_m`` vážený počet odrazů v nízkých
  zónách (1–2.65 m plná váha, výš jen zlomek, 3.4–5.5 m jen pod nízkou
  korunou) vůči odrazům od země; korekce podílem vysokých odrazů
  (``top_weight``) a hustotou first returnů (překryvy skenovacích pásů).
  Hodnota se škáluje prahem podle výšky koruny a stupni ``shade_steps``.
* **bílý les** – zbytek: vysoká koruna s prázdným spodním patrem.

DMP OK (ČÚZK) je jen first-return povrch (vegetace/budovy) a DMR 5G řídké
ground body; tím se pod korunou „podrost“ jeví přes poměr nízkých odrazů vůči
zemi, ne přes CHM. Proto CHM s výškovými pásy (``vegetation_chm``) dával
střídavě „vše les“ / „bílá v loukách“ – na loukách DMP body vůbec nejsou a
``fillnodata`` tam roztáhl výšky korun.

Defaulty = KP ``pullauta.base.ini`` Podkladárny (``greendetectsize=2``,
``zone1..3``, ``thresold*=0.1``, ``greenshades``, ``medianboxsize=6``,
``yellowheight=0.9``, ``yellowthresold=0.9``); kalibrováno proti KP
``vegetation.png`` na stejném LAZ (viz ``internal/vege-calibration-vs-kp.md``).
Kód je napsaný od nuly v numpy, KP (GPL) se nekopíruje ani nevolá.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Výstupní třídy (stejné jako vegetation_chm / vegetation_gdal):
# 0 bílý les, 1 = 401, 2 = 406, 3 = 408, 4 = 410.
WHITE, OPEN, LIGHT, MID, DENSE = 0, 1, 2, 3, 4

_NO_ZONE_ROOF = 1e9


@dataclass(frozen=True)
class DensityVegeParams:
    # --- žlutá (open land) ---
    yellow_height_m: float = 0.9
    yellow_threshold: float = 0.9
    yellow_window_m: float = 6.0
    # first+last (single) return mimo zem: váha do „vysokých“ pro žlutou
    yellow_single_return_weight: float = 1.0
    # --- zeleně ---
    block_m: float = 2.0
    green_ground_m: float = 0.9
    green_high_m: float = 2.0
    top_weight: float = 0.8
    # (low, high, roof_max, factor): bod v pásu výšky nad terénem, jen když
    # koruna bloku < roof_max; první vyhovující zóna vyhrává.
    zones: tuple[tuple[float, float, float, float], ...] = (
        (1.0, 2.65, 99.0, 1.0),
        (2.65, 3.4, 99.0, 0.1),
        (3.4, 5.5, 8.0, 0.2),
    )
    # (roof_low, roof_high, limit): práh hodnoty zeleně podle výšky koruny.
    roof_limits: tuple[tuple[float, float, float], ...] = (
        (0.2, 3.0, 0.1),
        (3.0, 4.0, 0.1),
        (4.0, 7.0, 0.1),
        (7.0, 20.0, 0.1),
        (20.0, 99.0, 0.1),
    )
    # Násobky limitu → stupně zeleně 1..N.
    shade_steps: tuple[float, ...] = (0.2, 0.35, 0.5, 0.7, 1.3, 2.6, 4.0)
    # Stupeň zeleně → ISOM třída: 1–3 → 406, 4–6 → 408, 7+ → 410
    # (stejné dělení jako barvy KP palety ve vegetation_gdal).
    shade_class_breaks: tuple[int, int] = (4, 7)
    point_volume_factor: float = 0.1
    point_volume_exponent: float = 1.0
    single_return_ground_weight: float = 3.0
    # single return mimo zem: váha pod / nad 5 m nad terénem
    single_return_low_factor: float = 1.0
    single_return_high_factor: float = 0.0
    single_return_split_m: float = 5.0
    # --- vyhlazení (liché okno v px výstupního rastru) ---
    median_size: int = 7
    yellow_median_size: int = 7


DEFAULT_PARAMS = DensityVegeParams()


@dataclass
class LidarPoints:
    x: "object"
    y: "object"
    z: "object"
    classification: "object"
    return_number: "object"
    number_of_returns: "object"

    def __len__(self) -> int:
        return int(len(self.x))


def read_las_points(paths: list[Path]) -> LidarPoints:
    """Načte body z LAZ/LAS (laspy + lazrs)."""
    import laspy
    import numpy as np

    parts: dict[str, list] = {k: [] for k in ("x", "y", "z", "c", "r", "n")}
    for path in paths:
        las = laspy.read(str(path))
        parts["x"].append(np.asarray(las.x, dtype=np.float64))
        parts["y"].append(np.asarray(las.y, dtype=np.float64))
        parts["z"].append(np.asarray(las.z, dtype=np.float32))
        parts["c"].append(np.asarray(las.classification, dtype=np.uint8))
        parts["r"].append(np.asarray(las.return_number, dtype=np.uint8))
        parts["n"].append(np.asarray(las.number_of_returns, dtype=np.uint8))
    cat = {k: np.concatenate(v) if v else np.zeros(0) for k, v in parts.items()}
    return LidarPoints(cat["x"], cat["y"], cat["z"], cat["c"], cat["r"], cat["n"])


def _bilinear(dem, gt, x, y):
    """Výška terénu v bodech (bilineárně mezi středy pixelů, okraje clamp)."""
    import numpy as np

    h, w = dem.shape
    fx = (x - gt[0]) / gt[1] - 0.5
    fy = (y - gt[3]) / gt[5] - 0.5
    fx = np.clip(fx, 0, w - 1)
    fy = np.clip(fy, 0, h - 1)
    x0 = np.minimum(np.floor(fx).astype(np.int64), max(w - 2, 0))
    y0 = np.minimum(np.floor(fy).astype(np.int64), max(h - 2, 0))
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    dx = (fx - x0).astype(np.float32)
    dy = (fy - y0).astype(np.float32)
    a = dem[y0, x0] * (1 - dx) + dem[y0, x1] * dx
    b = dem[y1, x0] * (1 - dx) + dem[y1, x1] * dx
    return a * (1 - dy) + b * dy


def _box_sum(arr, size: int):
    """Součet v okně size×size (integrální obraz), okraje = jen data uvnitř."""
    import numpy as np

    size = max(1, int(size))
    a = np.asarray(arr, dtype=np.float64)
    before = (size - 1) // 2
    after = size - 1 - before
    padded = np.pad(a, ((before, after), (before, after)), mode="constant")
    ii = np.zeros((padded.shape[0] + 1, padded.shape[1] + 1), dtype=np.float64)
    ii[1:, 1:] = padded.cumsum(0).cumsum(1)
    h, w = a.shape
    return (
        ii[size : size + h, size : size + w]
        - ii[0:h, size : size + w]
        - ii[size : size + h, 0:w]
        + ii[0:h, 0:w]
    )


def _min_filter(arr, size: int):
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    pad = size // 2
    big = np.iinfo(np.int64).max
    padded = np.pad(np.asarray(arr, dtype=np.int64), pad, constant_values=big)
    return sliding_window_view(padded, (size, size)).min(axis=(-2, -1))


def median_filter_uint8(arr, size: int, *, rows_per_chunk: int = 256):
    """Median filtr uint8 rastru po pruzích řádků (paměť O(chunk·size²))."""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    size = int(size)
    a = np.asarray(arr, dtype=np.uint8)
    if size < 3:
        return a.copy()
    if size % 2 == 0:
        size += 1
    pad = size // 2
    padded = np.pad(a, pad, mode="edge")
    out = np.empty_like(a)
    for r0 in range(0, a.shape[0], rows_per_chunk):
        r1 = min(a.shape[0], r0 + rows_per_chunk)
        win = sliding_window_view(padded[r0 : r1 + 2 * pad], (size, size))
        out[r0:r1] = np.median(win, axis=(-2, -1)).astype(np.uint8)
    return out


def classify_points(
    pts: LidarPoints,
    dem,
    gt,
    params: DensityVegeParams = DEFAULT_PARAMS,
    *,
    return_debug: bool = False,
):
    """Klasifikace na mřížce DEM (``gt`` = GDAL geotransform, sever nahoře).

    Vrací uint8 rastr tvaru ``dem`` (0 bílý les, 1 401, 2 406, 3 408, 4 410).
    """
    import numpy as np

    dem = np.asarray(dem, dtype=np.float32)
    h, w = dem.shape
    res = float(gt[1])
    x, y = pts.x, pts.y
    col = np.floor((x - gt[0]) / res).astype(np.int64)
    row = np.floor((y - gt[3]) / gt[5]).astype(np.int64)
    inside = (col >= 0) & (col < w) & (row >= 0) & (row < h)
    if not np.all(inside):
        x, y, col, row = x[inside], y[inside], col[inside], row[inside]
        z = pts.z[inside]
        cls = pts.classification[inside]
        rn = pts.return_number[inside]
        nr = pts.number_of_returns[inside]
    else:
        z, cls, rn, nr = pts.z, pts.classification, pts.return_number, pts.number_of_returns
    hh = (z - _bilinear(dem, gt, x, y)).astype(np.float32)
    is_ground = cls == 2
    single = (nr == 1) & (rn == 1)

    # ---------------- žlutá ----------------
    pix = row * w + col
    low = is_ground | (hh < params.yellow_height_m)
    yhit = np.bincount(pix[low], minlength=h * w).reshape(h, w)
    nohit_w = np.where(single, params.yellow_single_return_weight, 1.0)[~low]
    noyhit = np.bincount(pix[~low], weights=nohit_w, minlength=h * w).reshape(h, w)
    win = max(1, int(round(params.yellow_window_m / res)))
    ys = _box_sum(yhit, win)
    ns = _box_sum(noyhit, win)
    yellow = ys / (ys + ns + 0.01) > params.yellow_threshold

    # ---------------- zeleně (bloky) ----------------
    bpx = max(1, int(round(params.block_m / res)))
    bh, bw = -(-h // bpx), -(-w // bpx)
    bcol = col // bpx
    brow = row // bpx
    bidx = brow * bw + bcol
    nb = bh * bw

    top = np.full(nb, -np.inf, dtype=np.float32)
    np.maximum.at(top, bidx, z)
    # Terén bloku = průměr DEM v bloku.
    dem_pad = np.full((bh * bpx, bw * bpx), np.nan, dtype=np.float32)
    dem_pad[:h, :w] = dem
    dem_blk = np.nanmean(dem_pad.reshape(bh, bpx, bw, bpx), axis=(1, 3)).ravel()
    roof = np.where(np.isfinite(top), top - dem_blk, -np.inf)

    firsthit = np.bincount(bidx[rn == 1], minlength=nb).astype(np.float64)
    gmask = is_ground | (hh <= params.green_ground_m)
    gw = np.where(single, params.single_return_ground_weight, 1.0)
    ghit = np.bincount(bidx[gmask], weights=gw[gmask], minlength=nb)

    ng = ~gmask
    last = np.ones(hh.shape, dtype=np.float32)
    lastm = nr == rn
    last[lastm] = np.where(
        hh[lastm] < params.single_return_split_m,
        params.single_return_low_factor,
        params.single_return_high_factor,
    )
    roof_pt = roof[bidx]
    zone_w = np.zeros(hh.shape, dtype=np.float32)
    assigned = np.zeros(hh.shape, dtype=bool)
    for lo, hi, roof_max, factor in params.zones:
        m = ng & ~assigned & (hh >= lo) & (hh < hi) & (roof_pt < roof_max)
        zone_w[m] = factor
        assigned |= m
    greenhit = np.bincount(bidx, weights=zone_w * last, minlength=nb)
    highit = np.bincount(bidx[ng & (hh > params.green_high_m)], minlength=nb).astype(np.float64)

    fh2 = _min_filter(firsthit.reshape(bh, bw).astype(np.int64), 5).ravel().astype(np.float64)
    valid_g = ghit > 1
    aveg = float(firsthit[valid_g].mean()) if np.any(valid_g) else 1.0

    limit = np.full(nb, np.inf)
    for r_lo, r_hi, lim in reversed(params.roof_limits):
        limit[(roof >= r_lo) & (roof < r_hi)] = lim
    vol = np.clip(1.0 - params.point_volume_factor * fh2 / (aveg + 1e-5), 0.0, None)
    value = (
        greenhit / (ghit + greenhit + 1.0)
        * (1.0 - params.top_weight + params.top_weight * highit / (ghit + greenhit + highit + 1.0))
        * vol ** params.point_volume_exponent
    )
    shade = np.zeros(nb, dtype=np.uint8)
    pos = value > 0
    for step in params.shade_steps:
        shade += (pos & (value > limit * step)).astype(np.uint8)
    shade_px = np.repeat(np.repeat(shade.reshape(bh, bw), bpx, 0), bpx, 1)[:h, :w]

    if params.median_size >= 3:
        shade_px = median_filter_uint8(shade_px, params.median_size)
    yel = yellow.astype(np.uint8)
    if params.yellow_median_size >= 3:
        yel = median_filter_uint8(yel, params.yellow_median_size)

    b1, b2 = params.shade_class_breaks
    out = np.zeros((h, w), dtype=np.uint8)
    out[(shade_px >= 1) & (shade_px < b1)] = LIGHT
    out[(shade_px >= b1) & (shade_px < b2)] = MID
    out[shade_px >= b2] = DENSE
    out[yel.astype(bool)] = OPEN
    if return_debug:
        return out, {
            "yellow_ratio": ys / (ys + ns + 0.01),
            "value": value.reshape(bh, bw),
            "shade": shade.reshape(bh, bw),
            "roof": roof.reshape(bh, bw),
        }
    return out


def _pick_point_files(lidar_dir: Path) -> list[Path]:
    """Stejný vstup jako KP: merged_crop (DMR ground + DMP veg) nebo dvojice."""
    lidar_dir = Path(lidar_dir)
    ground = lidar_dir / "ground_merged.laz"
    veg = lidar_dir / "veg_merged.laz"
    if ground.is_file() and veg.is_file():
        return [ground, veg]
    for name in ("merged_crop.laz", "merged_crop_retry.laz", "merged.laz"):
        p = lidar_dir / name
        if p.is_file() and p.stat().st_size > 1000:
            return [p]
    return []


def generate_job_vegetation_density(
    work_dir: Path,
    *,
    params: DensityVegeParams = DEFAULT_PARAMS,
    log=None,
) -> Path | None:
    """Job fáze: LAZ hustota + ``dem/dem_filled.tif`` → ``vegetation/vegetation.shp``.

    ``None`` = nelze (chybí laspy / LAZ / DEM) → volající spadne na CHM.
    """
    import numpy as np

    from app.pipeline.dem_prep import DEM_DIR_NAME
    from app.pipeline.gdal_cli_raster import read_float32_geotiff
    from app.pipeline.vegetation_chm import polygonize_vegetation_classes, write_chm_tint_png

    work_dir = Path(work_dir)
    dem_tif = work_dir / DEM_DIR_NAME / "dem_filled.tif"
    files = _pick_point_files(work_dir / "lidar")
    if not dem_tif.is_file() or not files:
        if log:
            log("Vegetace (hustota bodů): chybí dem_filled.tif nebo LAZ – přeskočeno")
        return None
    try:
        import laspy  # noqa: F401
    except ImportError:
        if log:
            log("Vegetace (hustota bodů): chybí laspy – fallback CHM")
        return None
    if log:
        log("=== Fáze: vegetace z hustoty LiDAR bodů (bez KP) ===")
    try:
        dem, gt, nodata = read_float32_geotiff(dem_tif, log=log)
        dem = np.asarray(dem, dtype=np.float32)
        if nodata is not None:
            bad = dem == float(nodata)
            if np.any(bad):
                dem = np.where(bad, np.nanmedian(np.where(bad, np.nan, dem)), dem)
        pts = read_las_points(files)
        classified = classify_points(pts, dem, gt, params)
    except Exception as exc:
        if log:
            log(f"Vegetace (hustota bodů): selhalo ({exc}) – fallback CHM")
        return None
    shares = np.bincount(classified.ravel(), minlength=5) / classified.size * 100
    if log:
        log(
            "Vegetace (hustota bodů): "
            f"{len(pts)} bodů, bílý {shares[0]:.1f} %, 401 {shares[1]:.1f} %, "
            f"406 {shares[2]:.1f} %, 408 {shares[3]:.1f} %, 410 {shares[4]:.1f} %"
        )
    dest = work_dir / "vegetation" / "vegetation.shp"
    try:
        write_chm_tint_png(classified, work_dir / "vegetation" / "chm_tint.png")
    except Exception:
        pass
    return polygonize_vegetation_classes(classified, gt, dest, log=log, label="hustota bodů")
