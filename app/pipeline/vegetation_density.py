"""Vegetace z hustoty LiDAR odrazů (vlastní implementace; volně inspirováno KP).

Proč ne CHM: běh lesem neurčuje výška koruny, ale **co je v nízkém patře**.
Počítáme body:

* **žlutá 401** – buňky ``yellow_cell_m`` (3 m), poměr nízkých odrazů
  (ground nebo < ``yellow_height_m``) v okně ``yellow_window_cells``×…
  (2×2 → 6 m) > ``yellow_threshold``. Louka = skoro všechny body u země.
  ČÚZK DMP má husté first-return koruny a DMR řídký ground — stromy v louce
  (CHM ~14–20 m) by jinak „vybílily“ 401. Proto po detekci žluté **fill
  malých uzavřených děr** (``yellow_canopy_close_m`` → max plocha) a reclaim
  bílých pixelů uvnitř žlutého okolí (low-veg under tree / canopy mask).
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

Defaulty (``greendetectsize``/zóny/prahy) jsou kalibrované historicky proti
starším rastrovým výstupům; kód je napsaný od nuly v numpy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.pipeline.prepare_lidar import log_step

# Výstupní třídy (stejné jako vegetation_chm / vegetation_gdal):
# 0 bílý les, 1 = 401, 2 = 406, 3 = 408, 4 = 410.
WHITE, OPEN, LIGHT, MID, DENSE = 0, 1, 2, 3, 4

_NO_ZONE_ROOF = 1e9


@dataclass(frozen=True)
class DensityVegeParams:
    # --- žlutá (open land) ---
    yellow_height_m: float = 0.9
    yellow_threshold: float = 0.9
    # KP: hit-bin 3 m, poměr v 2×2 buňkách (= 6 m). ``yellow_window_m`` je
    # odvozené (cell × window_cells) – drženo kvůli testům / starším voláním.
    yellow_cell_m: float = 3.0
    yellow_window_cells: int = 2
    yellow_window_m: float = 6.0
    # first+last (single) return mimo zem: váha do „vysokých“ pro žlutou
    yellow_single_return_weight: float = 1.0
    # Max. „poloměr“ díry po DMP koruně ve žluté (m); plocha díry ≤ ~okno².
    yellow_canopy_close_m: float = 6.0
    # Reclaim bílého pixelu uvnitř žluté (podíl OPEN v 3×3), když chybí zeleň.
    yellow_under_canopy_frac: float = 0.45
    # Úzký pás louky mezi stromy: žluté okno nabere koruny → CHM prior.
    # Jen WHITE→OPEN, když je okolí taky nízké (ne DMP díra v koruně).
    chm_open_max_m: float = 2.0
    chm_open_neighbor_px: int = 5
    chm_open_neighbor_frac: float = 0.4
    # Zlom louka↔vysoké stromy: 401 až k hraně (CHM pod expand u louky).
    # Zelená z hustoty se nepřepisuje. Vysoké CHM u louky zůstane bílé.
    # Více průchodů = posun o 1 px za průchod (3×3 touch), ne do koruny.
    meadow_edge_expand_chm_m: float = 8.0
    meadow_edge_open_frac: float = 0.12
    meadow_edge_passes: int = 2
    # Legacy / testy: invent 410 na zlomu je vypnuté (0 = nepoužít).
    meadow_edge_green_chm_max_m: float = 0.0
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


def _neighbor_fraction(mask):
    """Podíl True v 3×3 (včetně self)."""
    import numpy as np

    m = np.asarray(mask, dtype=np.float32)
    padded = np.pad(m, 1, mode="edge")
    acc = np.zeros_like(m, dtype=np.float32)
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            acc += padded[1 + di : 1 + di + m.shape[0], 1 + dj : 1 + dj + m.shape[1]]
    return acc / 9.0


def fill_small_holes(mask, *, max_hole_px: int):
    """Vyplní uzavřené False díry o ploše ≤ ``max_hole_px`` (stromy v louce).

    Velké ne-žluté ostrovy (les uvnitř luk) nechá být — flood z okraje +
    size filter na zbytkových komponentách.
    """
    import numpy as np

    m = np.asarray(mask, dtype=bool)
    if max_hole_px <= 0 or not np.any(m) or np.all(m):
        return m.copy()
    h, w = m.shape
    # False dosažitelné z okraje rastru = „vnější“ ne-žlutá (les / okraj AOI).
    exterior = np.zeros((h, w), dtype=bool)
    stack = []
    for i in range(h):
        for j in (0, w - 1):
            if not m[i, j] and not exterior[i, j]:
                exterior[i, j] = True
                stack.append((i, j))
    for j in range(w):
        for i in (0, h - 1):
            if not m[i, j] and not exterior[i, j]:
                exterior[i, j] = True
                stack.append((i, j))
    while stack:
        i, j = stack.pop()
        if i > 0 and not m[i - 1, j] and not exterior[i - 1, j]:
            exterior[i - 1, j] = True
            stack.append((i - 1, j))
        if i + 1 < h and not m[i + 1, j] and not exterior[i + 1, j]:
            exterior[i + 1, j] = True
            stack.append((i + 1, j))
        if j > 0 and not m[i, j - 1] and not exterior[i, j - 1]:
            exterior[i, j - 1] = True
            stack.append((i, j - 1))
        if j + 1 < w and not m[i, j + 1] and not exterior[i, j + 1]:
            exterior[i, j + 1] = True
            stack.append((i, j + 1))
    holes = ~m & ~exterior
    if not np.any(holes):
        return m.copy()
    # Komponenty děr — vyplň jen malé (koruna stromu), ne celý lesní ostrov.
    out = m.copy()
    unseen = holes.copy()
    for i in range(h):
        for j in range(w):
            if not unseen[i, j]:
                continue
            comp = [(i, j)]
            unseen[i, j] = False
            idx = 0
            while idx < len(comp):
                ci, cj = comp[idx]
                idx += 1
                for ni, nj in (
                    (ci - 1, cj),
                    (ci + 1, cj),
                    (ci, cj - 1),
                    (ci, cj + 1),
                ):
                    if 0 <= ni < h and 0 <= nj < w and unseen[ni, nj]:
                        unseen[ni, nj] = False
                        comp.append((ni, nj))
            if len(comp) <= max_hole_px:
                for ci, cj in comp:
                    out[ci, cj] = True
    return out


def yellow_mask_from_hits(
    yhit,
    noyhit,
    *,
    shape: tuple[int, int],
    res: float,
    params: DensityVegeParams = DEFAULT_PARAMS,
):
    """Žlutá 401: 3 m buňky, poměr v 2×2, upsample na DEM, canopy holes.

    ``yhit`` / ``noyhit`` jsou hit-count rastry ve výstupním rozlišení DEM
    (stejný tvar jako ``shape``).
    """
    import numpy as np

    h, w = shape
    yhit = np.asarray(yhit, dtype=np.float64)
    noyhit = np.asarray(noyhit, dtype=np.float64)
    cell_m = float(params.yellow_cell_m) if params.yellow_cell_m > 0 else float(params.yellow_window_m)
    # Odvození cell z legacy yellow_window_m, když window_cells sedí.
    win_cells = max(1, int(params.yellow_window_cells))
    if cell_m <= 0:
        cell_m = float(params.yellow_window_m) / win_cells
    cell_px = max(1, int(round(cell_m / max(res, 1e-6))))
    bh = -(-h // cell_px)
    bw = -(-w // cell_px)
    pad_h, pad_w = bh * cell_px, bw * cell_px
    y_pad = np.zeros((pad_h, pad_w), dtype=np.float64)
    n_pad = np.zeros((pad_h, pad_w), dtype=np.float64)
    y_pad[:h, :w] = yhit
    n_pad[:h, :w] = noyhit
    y_c = y_pad.reshape(bh, cell_px, bw, cell_px).sum(axis=(1, 3))
    n_c = n_pad.reshape(bh, cell_px, bw, cell_px).sum(axis=(1, 3))

    if win_cells <= 1:
        yellow_c = y_c / (y_c + n_c + 0.01) > params.yellow_threshold
    else:
        # KP: součet [iy, iy+win)×[ix, ix+win) → poměr na startovací buňce.
        ii_y = np.pad(y_c.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
        ii_n = np.pad(n_c.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
        ys = (
            ii_y[win_cells : bh + 1, win_cells : bw + 1]
            - ii_y[0 : bh - win_cells + 1, win_cells : bw + 1]
            - ii_y[win_cells : bh + 1, 0 : bw - win_cells + 1]
            + ii_y[0 : bh - win_cells + 1, 0 : bw - win_cells + 1]
        )
        ns = (
            ii_n[win_cells : bh + 1, win_cells : bw + 1]
            - ii_n[0 : bh - win_cells + 1, win_cells : bw + 1]
            - ii_n[win_cells : bh + 1, 0 : bw - win_cells + 1]
            + ii_n[0 : bh - win_cells + 1, 0 : bw - win_cells + 1]
        )
        yellow_c = np.zeros((bh, bw), dtype=bool)
        interior = ys / (ys + ns + 0.01) > params.yellow_threshold
        yellow_c[: interior.shape[0], : interior.shape[1]] = interior
        # Okraj bez plného okna: vlastní poměr buňky.
        edge = np.zeros((bh, bw), dtype=bool)
        edge[-(win_cells - 1) :, :] = True
        edge[:, -(win_cells - 1) :] = True
        own = y_c / (y_c + n_c + 0.01) > params.yellow_threshold
        yellow_c[edge & own] = True

    yellow = np.repeat(np.repeat(yellow_c, cell_px, 0), cell_px, 1)[:h, :w]

    # Díry po korunách: 2×2 okno kolem stromu ≈ (window+cell)² px (často ~9×9).
    close_m = float(params.yellow_canopy_close_m)
    if close_m > 0:
        px = max(res, 1e-6)
        side_m = float(params.yellow_window_m) + close_m
        max_hole_px = max(1, int(round((side_m * side_m) / (px * px))))
        yellow = fill_small_holes(yellow, max_hole_px=max_hole_px)
    return yellow


def reclaim_yellow_under_canopy(
    classified,
    *,
    frac_min: float = 0.55,
):
    """Bílý pixel v převážně žlutém okolí → 401 (strom v louce, ne bílý les).

    Nezasahuje do zeleně (406/408/410): jen WHITE→OPEN. Solidní bílý les
    nemá dost OPEN sousedů, takže zůstane bílý (Rokytnice).
    """
    import numpy as np

    out = np.asarray(classified, dtype=np.uint8).copy()
    open_m = out == OPEN
    if not np.any(open_m):
        return out
    frac = _neighbor_fraction(open_m)
    reclaim = (out == WHITE) & (frac >= frac_min)
    out[reclaim] = OPEN
    return out


def reinforce_open_from_chm(
    classified,
    chm,
    *,
    open_max_m: float = 1.5,
    neighbor_px: int = 5,
    neighbor_frac: float = 0.5,
):
    """Bílý pixel s nízkým CHM v převážně nízkém okolí → 401.

    Úzký/dlouhý pás louky v lese: žluté okno (~6 m) nabere koruny ze stran
    a poměr nízkých odrazů klesne pod práh, i když DMP−DMR (CHM) otevřený
    terén vidí. Min. plocha polygonu 401 je 12 m² – to pásy neshazuje;
    problém je klasifikace, ne filtr velikosti.

    Jen WHITE→OPEN (zelení 406+ nechá). Izolované CHM díry v koruně (DMP
    bez bodu) bez nízkého okolí neprojdou ``neighbor_frac``.
    """
    import numpy as np

    out = np.asarray(classified, dtype=np.uint8).copy()
    if chm is None:
        return out
    h = np.asarray(chm, dtype=np.float32)
    if h.shape != out.shape:
        return out
    if open_max_m <= 0 or neighbor_px < 1 or neighbor_frac <= 0:
        return out

    low = np.isfinite(h) & (h < float(open_max_m))
    if not np.any(low):
        return out
    n = int(neighbor_px)
    if n % 2 == 0:
        n += 1
    # Podíl nízkého CHM v okně (stejná logika jako žluté box sum / n²).
    low_f = low.astype(np.float64)
    summed = _box_sum(low_f, n)
    frac = summed / float(n * n)
    force = (out == WHITE) & low & (frac >= float(neighbor_frac))
    out[force] = OPEN
    return out


def soften_meadow_forest_edge(
    classified,
    chm,
    *,
    expand_chm_m: float = 8.0,
    open_frac_min: float = 0.12,
    green_chm_max_m: float = 0.0,
    passes: int = 2,
):
    """U zlomu louka↔vysoké stromy: 401 až k hraně; zelená podle hustoty.

    * CHM &lt; expand a bílý u louky → 401 (louka až k koruně).
    * Existující 406/408/410 se **nepřepisují** (přechod do zeleně = realita
      z hustoty odrazů).
    * CHM ≥ expand u louky: nechá bílou (vzrostlá koruna).
    * ``passes`` &gt; 1: opakuje expand (každý průchod max ~1 px), pořád jen
      do bílých pixelů s CHM &lt; expand — ne do vysoké koruny.
    * ``green_chm_max_m`` &gt; expand: volitelně bílý se středním CHM → 410
      (starší chování); default 0 = vypnuto, ať se na zlomu nevymýšlí zeleň.
    """
    import numpy as np

    out = np.asarray(classified, dtype=np.uint8).copy()
    if chm is None:
        return out
    h = np.asarray(chm, dtype=np.float32)
    if h.shape != out.shape:
        return out

    n_pass = max(1, int(passes))
    green_max = float(green_chm_max_m)
    for _ in range(n_pass):
        open_frac = _neighbor_fraction(out == OPEN)
        touches = open_frac >= float(open_frac_min)
        white = out == WHITE
        finite = np.isfinite(h)

        # Jen WHITE→OPEN; zelené třídy z density nechat.
        expand = white & touches & finite & (h < float(expand_chm_m))
        if not np.any(expand):
            break
        out[expand] = OPEN

    if green_max > float(expand_chm_m):
        open_frac = _neighbor_fraction(out == OPEN)
        touches = open_frac >= float(open_frac_min)
        white2 = out == WHITE
        finite = np.isfinite(h)
        edge_green = (
            white2
            & touches
            & finite
            & (h >= float(expand_chm_m))
            & (h < green_max)
        )
        out[edge_green] = DENSE
    return out


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

    # ---------------- žlutá (KP 3 m buňky + canopy close) ----------------
    pix = row * w + col
    low = is_ground | (hh < params.yellow_height_m)
    yhit = np.bincount(pix[low], minlength=h * w).reshape(h, w)
    nohit_w = np.where(single, params.yellow_single_return_weight, 1.0)[~low]
    noyhit = np.bincount(pix[~low], weights=nohit_w, minlength=h * w).reshape(h, w)
    yellow = yellow_mask_from_hits(
        yhit, noyhit, shape=(h, w), res=res, params=params
    )
    ys = _box_sum(yhit, max(1, int(round(params.yellow_window_m / res))))
    ns = _box_sum(noyhit, max(1, int(round(params.yellow_window_m / res))))

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
    # Stromy v louce: hustá DMP koruna → díra v žluté / bílý flek; reclaim 401.
    out = reclaim_yellow_under_canopy(
        out, frac_min=params.yellow_under_canopy_frac
    )
    if return_debug:
        return out, {
            "yellow_ratio": ys / (ys + ns + 0.01),
            "yellow": yellow,
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
    veg_size_profile: str = "default",
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
        log("=== Fáze: vegetace z hustoty LiDAR bodů ===")
    log_step(log, "Klasifikuji vegetaci z hustoty LiDAR odrazů (náhrada KP)")
    try:
        dem, gt, nodata = read_float32_geotiff(dem_tif, log=log)
        dem = np.asarray(dem, dtype=np.float32)
        if nodata is not None:
            bad = dem == float(nodata)
            if np.any(bad):
                dem = np.where(bad, np.nanmedian(np.where(bad, np.nan, dem)), dem)
        pts = read_las_points(files)
        classified = classify_points(pts, dem, gt, params)
        chm_tif = work_dir / DEM_DIR_NAME / "chm.tif"
        if chm_tif.is_file():
            try:
                chm_arr, chm_gt, chm_nodata = read_float32_geotiff(chm_tif, log=log)
                chm_arr = np.asarray(chm_arr, dtype=np.float32)
                if chm_arr.shape == classified.shape:
                    if chm_nodata is not None:
                        chm_arr = np.where(
                            chm_arr == float(chm_nodata), np.nan, chm_arr
                        )
                    before = int(np.count_nonzero(classified == OPEN))
                    classified = reinforce_open_from_chm(
                        classified,
                        chm_arr,
                        open_max_m=params.chm_open_max_m,
                        neighbor_px=params.chm_open_neighbor_px,
                        neighbor_frac=params.chm_open_neighbor_frac,
                    )
                    # Po CHM prioru znovu reclaim stromů uvnitř nově žlutého pásu.
                    classified = reclaim_yellow_under_canopy(
                        classified, frac_min=params.yellow_under_canopy_frac
                    )
                    classified = soften_meadow_forest_edge(
                        classified,
                        chm_arr,
                        expand_chm_m=params.meadow_edge_expand_chm_m,
                        open_frac_min=params.meadow_edge_open_frac,
                        green_chm_max_m=params.meadow_edge_green_chm_max_m,
                        passes=params.meadow_edge_passes,
                    )
                    # Ještě jednou reclaim po expandu žluté na zlomu.
                    classified = reclaim_yellow_under_canopy(
                        classified, frac_min=params.yellow_under_canopy_frac
                    )
                    gained = int(np.count_nonzero(classified == OPEN)) - before
                    if log and gained > 0:
                        log(
                            f"Vegetace (hustota bodů): CHM open/edge prior "
                            f"+{gained} px 401 (úzké louky / zlom)"
                        )
            except Exception as chm_exc:
                if log:
                    log(f"Vegetace CHM open prior: přeskočeno ({chm_exc})")
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
    return polygonize_vegetation_classes(
        classified,
        gt,
        dest,
        log=log,
        label="hustota bodů",
        veg_size_profile=veg_size_profile,
    )
