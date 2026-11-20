"""Uzavřené tvary vrstevnic: pomocná kolem kopečku / prohlubně a spádnice do prohlubní.

Podnět mapaře (Petr 2026-11-19, etapa 1):

* **Uzavřená pomocná v libovolné výšce** – kopeček nebo prohlubeň v rovině, kterou základní
  vrstevnice minou a půlová linie ji nezachytí (vrchol mezi ¼ a ½ ekvidistance). Kandidáti
  jsou kroužky ve ¼ a ¾ ekvidistance; z každého tvaru se vezme nejvýš jeden (ISOM / ISSprOM:
  mezi sousedními vrstevnicemi jen jedna pomocná), raději blíž půlce. Musí se vejít
  (odstup od vrstevnic, velikost na papíře) a uvnitř nesmí být žádná vrstevnice ani jiná 103.
* **Spádnice** (101.1 / 103.1) na uzavřené vrstevnici, která obepíná prohlubeň – bez ní se
  prohlubeň čte jako kopec. Prohlubeň = medián DEM uvnitř kroužku je pod jeho výškou.
"""

from __future__ import annotations

import math
from pathlib import Path

# mode → (min. plocha kroužku mm², min. odstup od vrstevnic a 103 mm)
CLOSED_FORMLINE_PARAMS: dict[str, tuple[float, float]] = {
    "sparse": (3.0, 0.35),
    "more": (2.0, 0.3),
}

# Spádnice: zapnuto vždy (ISOM 101.1: uzavřená vrstevnice v prohlubni). False = jako dřív.
SLOPE_LINES = True
SLOPE_MIN_PERIMETER_MM = 2.0  # menší kroužky (pod ~0,6 mm průměr) bez spádnice
SLOPE_PER_MM = 40.0  # každých 40 mm obvodu jedna spádnice navíc
SLOPE_MAX_PER_RING = 3
_DEPRESSION_MIN_DROP_M = 0.05  # medián uvnitř aspoň o tolik pod výškou kroužku
_DEM_MAX_SAMPLES = 4000


def _mm_to_m(mm: float, scale: int) -> float:
    return float(mm) * float(scale) / 1000.0


def _closed(pts) -> bool:
    return len(pts) >= 4 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-6


def select_closed_formlines(
    candidates: list[tuple[float, list[tuple[float, float]]]],
    contours: list[tuple[float, list[tuple[float, float]]]],
    selected: list[list[tuple[float, float]]],
    *,
    interval_m: float,
    scale: int,
    mode: str,
) -> list[tuple[float, list[tuple[float, float]], bool]]:
    """Z kroužků ve ¼ / ¾ ekvidistance vybere nejvýš jeden na tvar (viz modul)."""
    params = CLOSED_FORMLINE_PARAMS.get(mode)
    if not params or not candidates:
        return []
    from shapely import STRtree
    from shapely.geometry import LineString, Polygon

    area_min_mm2, gap_mm = params
    k = float(scale) / 1000.0
    area_min = area_min_mm2 * k * k
    gap_m = _mm_to_m(gap_mm, scale)
    interval = max(float(interval_m), 0.1)

    main = [LineString(pts) for _e, pts in contours if len(pts) >= 2]
    tree = STRtree(main) if main else None
    taken_geoms = [LineString(pts) for pts in selected if len(pts) >= 2]
    xs = [p[0] for _e, pts in contours for p in pts]
    ys = [p[1] for _e, pts in contours for p in pts]
    extent = (min(xs), min(ys), max(xs), max(ys)) if xs else None

    rings = []
    for elev, pts in candidates:
        if not _closed(pts) or len(pts) < 5:
            continue
        poly = Polygon(pts)
        if not poly.is_valid or poly.area < area_min:
            continue
        if extent is not None:
            x0, y0, x1, y1 = poly.bounds
            if x0 - extent[0] < gap_m or y0 - extent[1] < gap_m or extent[2] - x1 < gap_m or extent[3] - y1 < gap_m:
                continue  # u okraje výřezu nevíme, co je za ním
        frac = (elev / interval) % 1.0
        rings.append((abs(frac - 0.5), -poly.area, elev, pts, poly))
    # Raději blíž půlce intervalu, pak větší kroužek.
    rings.sort(key=lambda r: (r[0], r[1]))

    out: list[tuple[float, list[tuple[float, float]], bool]] = []
    accepted: list = list(taken_geoms)
    for _d, _a, elev, pts, poly in rings:
        zone = poly.buffer(gap_m)
        if tree is not None:
            hits = tree.query(zone, predicate="intersects")
            if len(hits):
                continue  # vrstevnice uvnitř nebo moc blízko
        if any(zone.intersects(g) for g in accepted):
            continue  # už jedna 103 ve stejném tvaru (nebo těsně vedle)
        accepted.append(poly)
        out.append((elev, list(pts), True))
    return out


class _Dem:
    def __init__(self, path: Path):
        from osgeo import gdal

        ds = gdal.Open(str(path))
        if ds is None:
            raise OSError(path)
        self.gt = ds.GetGeoTransform()
        band = ds.GetRasterBand(1)
        self.nodata = band.GetNoDataValue()
        self.arr = band.ReadAsArray().astype("float32")

    def inside_median(self, poly) -> float | None:
        import numpy as np
        from shapely import contains_xy

        gt = self.gt
        if gt[2] or gt[4]:
            return None
        x0, y0, x1, y1 = poly.bounds
        h, w = self.arr.shape
        c0 = max(0, int((x0 - gt[0]) / gt[1]))
        c1 = min(w, int((x1 - gt[0]) / gt[1]) + 1)
        r0 = max(0, int((y1 - gt[3]) / gt[5]))
        r1 = min(h, int((y0 - gt[3]) / gt[5]) + 1)
        if c1 <= c0 or r1 <= r0:
            return None
        n = (c1 - c0) * (r1 - r0)
        step = max(1, int(math.sqrt(n / _DEM_MAX_SAMPLES)))
        cols = np.arange(c0, c1, step)
        rows = np.arange(r0, r1, step)
        cc, rr = np.meshgrid(cols, rows)
        px = gt[0] + (cc + 0.5) * gt[1]
        py = gt[3] + (rr + 0.5) * gt[5]
        mask = contains_xy(poly, px, py)
        if not mask.any():
            # Kroužek menší než pixel: hodnota ve středu.
            c = poly.representative_point()
            col, row = int((c.x - gt[0]) / gt[1]), int((c.y - gt[3]) / gt[5])
            if not (0 <= col < w and 0 <= row < h):
                return None
            vals = np.asarray([self.arr[row, col]])
        else:
            vals = self.arr[rr[mask], cc[mask]]
        if self.nodata is not None:
            vals = vals[np.abs(vals - float(self.nodata)) > 1e-6]
        vals = vals[np.isfinite(vals)]
        if not len(vals):
            return None
        return float(np.median(vals))


def load_dem(contours_dir: Path) -> _Dem | None:
    """DEM, ze kterého vznikly vrstevnice (adaptivní, jinak hlazený)."""
    from app.pipeline.contours_adaptive import DEM_CONTOUR_NAME

    for name in (DEM_CONTOUR_NAME, "dem_smooth.tif"):
        path = contours_dir / name
        if path.is_file():
            try:
                return _Dem(path)
            except Exception:  # noqa: BLE001 – bez DEM jen bez spádnic
                return None
    return None


def is_depression(dem: _Dem, pts: list[tuple[float, float]], elev: float) -> bool:
    from shapely.geometry import Polygon

    poly = Polygon(pts)
    if not poly.is_valid or poly.area <= 0:
        return False
    med = dem.inside_median(poly)
    return med is not None and med < float(elev) - _DEPRESSION_MIN_DROP_M


def slope_ticks(
    pts: list[tuple[float, float]],
    *,
    scale: int,
) -> list[tuple[float, float, float, float]]:
    """Body spádnic na kroužku prohlubně: (x, y, dx, dy) se směrem dovnitř (z kopce)."""
    n_pts = len(pts)
    if n_pts < 4:
        return []
    seg = [math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) for i in range(n_pts - 1)]
    perimeter = sum(seg)
    perim_mm = perimeter * 1000.0 / float(scale)
    if perim_mm < SLOPE_MIN_PERIMETER_MM:
        return []
    count = min(SLOPE_MAX_PER_RING, 1 + int(perim_mm // SLOPE_PER_MM))
    # Orientace: kladná plocha = proti směru hodinových ručiček → vnitřek vlevo.
    area2 = sum(pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1] for i in range(n_pts - 1))
    left = 1.0 if area2 > 0 else -1.0
    out = []
    targets = [(i + 0.5) * perimeter / count for i in range(count)]
    acc = 0.0
    j = 0
    for i, length in enumerate(seg):
        while j < len(targets) and acc + length >= targets[j]:
            if length <= 1e-9:
                break
            t = (targets[j] - acc) / length
            x = pts[i][0] + t * (pts[i + 1][0] - pts[i][0])
            y = pts[i][1] + t * (pts[i + 1][1] - pts[i][1])
            tx = (pts[i + 1][0] - pts[i][0]) / length
            ty = (pts[i + 1][1] - pts[i][1]) / length
            out.append((x, y, -ty * left, tx * left))
            j += 1
        acc += length
    return out


def ocad_rotation(to_map, x: float, y: float, dx: float, dy: float) -> float:
    """Natočení bodového symbolu (rad, OOM: proti směru hodinových ručiček, 0 = čárka k severu mapy)."""
    mx0, my0 = to_map(x, y)
    mx1, my1 = to_map(x + dx * 10.0, y + dy * 10.0)
    vx, vy_up = mx1 - mx0, -(my1 - my0)  # mapa OOM má y dolů
    return math.atan2(-vx, vy_up)
