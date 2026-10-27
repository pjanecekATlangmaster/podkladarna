"""Balvany (204) ze sdíleného DEM – jen jisté.

Balvan je ostrá vyvýšenina zvednutá ze všech stran: ve čtyřech směrech
(S–J, V–Z a obě úhlopříčky) je bod o ``BOULDER_MIN_HEIGHT_M`` výš než průměr
dvou protilehlých bodů ve vzdálenosti ``BOULDER_RADIUS_M``. Rovina (svah) tak
nic nedá, hrana srázu, hřbet ani násep (zvednuté jen napříč) neprojdou.

Měření proti ručním mapám (Harrachov, Vyšší Brod E1–E3, 2026-10-27): body
≥ 1,2 m mají do 6 m od balvanu / skalky reference 83–96 % (náhodně 21–33 %).
Nižší (0,8–1 m) jsou v DMR 5G už šum. Nad 3 m jde spíš o skalní stěnu nebo
hranu terasy, ty kreslí plochy skal. Výběr čistí až sestavení mapy
(``filter_boulder_points``): blízko skalní plochy, kupky nebo cesty, budovy
či vody balvan nekreslíme – u cest a staveb jsou to náspy a zídky
(Harrachov: přesnost bodů do 25 m od cest 0,19–0,55).

Výstup ``work/temp/dotboulders.dxf`` jako POINT.
"""

from __future__ import annotations

from pathlib import Path

from app.pipeline.cliffs_dem import dem_filled_path
from app.pipeline.job_progress import log_issue
from app.pipeline.knolls_dem import _pick_spaced, write_knoll_points_dxf
from app.pipeline.prepare_lidar import log_step

BOULDER_RADIUS_M = 3.0
BOULDER_MIN_HEIGHT_M = 1.2
BOULDER_MAX_HEIGHT_M = 3.0
# Vrchol = maximum v okně ± tolik metrů.
BOULDER_PEAK_WINDOW_M = 2.0
# Značka 204 má 0,4 mm (4 m na 1:10 000); hustší body by splývaly.
BOULDER_MIN_SPACING_M = 6.0
BOULDER_MAX_POINTS = 8_000
# Sestavení mapy: odstup od čar cest / vody / plotů a od ploch budov a vody
# (náspy, zářezy, zídky), od skalních ploch (skálu už kreslí plocha) a od kupek.
# Harrachov: body 15–20 m od osy cesty mají přesnost 0,43, od 25 m 0,88 (řady
# bodů podél hrany zářezu silnice v otočce 2026-10-27).
BOULDER_LINE_CLEAR_M = 25.0
BOULDER_AREA_CLEAR_M = 12.0
BOULDER_ROCK_CLEAR_M = 2.0
BOULDER_KNOLL_CLEAR_M = 6.0

DXF_BOULDERS = "dotboulders.dxf"


def detect_boulders(
    elev,
    gt,
    *,
    nodata=None,
    max_points: int = BOULDER_MAX_POINTS,
) -> list[tuple[float, float]]:
    """Body (x, y) balvanů, nejvyšší první, s rozestupem ``BOULDER_MIN_SPACING_M``."""
    import numpy as np
    from scipy import ndimage

    arr = np.asarray(elev, dtype=np.float32)
    h, w = arr.shape
    px = abs(float(gt[1])) or 1.0
    r = max(1, int(round(BOULDER_RADIUS_M / px)))
    if h <= 2 * r or w <= 2 * r:
        return []
    valid = np.isfinite(arr)
    if nodata is not None:
        valid &= arr != float(nodata)
    if not valid.any():
        return []
    zs = np.where(valid, arr, np.nan).astype(np.float32)

    def at(dy: int, dx: int):
        """Posun s NaN mimo rastr (žádné přetékání přes okraj)."""
        out = np.full_like(zs, np.nan)
        ys = slice(max(0, -dy), h - max(0, dy))
        yd = slice(max(0, dy), h - max(0, -dy))
        xs = slice(max(0, -dx), w - max(0, dx))
        xd = slice(max(0, dx), w - max(0, -dx))
        out[ys, xs] = zs[yd, xd]
        return out

    rise = None
    for dy, dx in ((0, r), (r, 0), (r, r), (r, -r)):
        cur = zs - (at(dy, dx) + at(-dy, -dx)) * 0.5
        # np.minimum nese NaN dál – okraj rastru / nodata bod zruší
        rise = cur if rise is None else np.minimum(rise, cur)
    rise = np.nan_to_num(rise, nan=-np.inf, posinf=-np.inf)
    win = 2 * max(1, int(round(BOULDER_PEAK_WINDOW_M / px))) + 1
    peak = rise == ndimage.maximum_filter(rise, size=win, mode="constant", cval=-np.inf)
    sel = peak & (rise >= BOULDER_MIN_HEIGHT_M) & (rise <= BOULDER_MAX_HEIGHT_M)
    rows, cols = np.nonzero(sel)
    candidates = []
    for row, col in zip(rows.tolist(), cols.tolist()):
        x = gt[0] + (col + 0.5) * gt[1] + (row + 0.5) * gt[2]
        y = gt[3] + (col + 0.5) * gt[4] + (row + 0.5) * gt[5]
        candidates.append((float(rise[row, col]), float(x), float(y)))
    return _pick_spaced(candidates, min_spacing_m=BOULDER_MIN_SPACING_M, max_points=max_points)


def parse_point_dxf(path: Path) -> list[tuple[float, float]]:
    """POINT z ASCII DXF (``write_knoll_points_dxf``) bez OGR."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[tuple[float, float]] = []
    i = 0
    n = len(lines)
    while i < n - 1:
        if lines[i].strip() == "0" and lines[i + 1].strip() == "POINT":
            x = y = None
            j = i + 2
            while j < n - 1 and lines[j].strip() != "0":
                code = lines[j].strip()
                if code == "10":
                    x = float(lines[j + 1])
                elif code == "20":
                    y = float(lines[j + 1])
                j += 2
            if x is not None and y is not None:
                out.append((x, y))
            i = j
            continue
        i += 1
    return out


def filter_boulder_points(
    points: list[tuple[float, float]],
    *,
    rock_rings: list[list[tuple[float, float]]] | None = None,
    blocker_rings: list[list[tuple[float, float]]] | None = None,
    blocker_lines: list[list[tuple[float, float]]] | None = None,
    knolls: list[tuple[float, float]] | None = None,
) -> tuple[list[tuple[float, float]], dict[str, int]]:
    """Zahodí balvany u skalní plochy, kupky, cesty / vody / plotu a budovy / vodní plochy."""
    stats = {"skala": 0, "kupka": 0, "objekt": 0}
    if not points:
        return [], stats
    try:
        from shapely import STRtree
        from shapely.geometry import LineString, Point, Polygon
    except ImportError:
        return list(points), stats

    def _polys(rings):
        out = []
        for ring in rings or []:
            if len(ring) < 3:
                continue
            try:
                poly = Polygon(ring)
                if not poly.is_valid:
                    poly = poly.buffer(0)
            except Exception:
                continue
            if not poly.is_empty:
                out.append(poly)
        return out

    lines = []
    for pts in blocker_lines or []:
        if len(pts) >= 2:
            try:
                lines.append(LineString(pts))
            except Exception:
                continue
    groups = (
        ("skala", _polys(rock_rings), BOULDER_ROCK_CLEAR_M),
        ("kupka", [Point(p) for p in knolls or []], BOULDER_KNOLL_CLEAR_M),
        ("objekt", lines, BOULDER_LINE_CLEAR_M),
        ("objekt", _polys(blocker_rings), BOULDER_AREA_CLEAR_M),
    )
    trees = [(key, STRtree(geoms), geoms, dist) for key, geoms, dist in groups if geoms]
    kept: list[tuple[float, float]] = []
    for x, y in points:
        pt = Point(x, y)
        reason = None
        for key, tree, geoms, dist in trees:
            hits = tree.query(pt, predicate="dwithin", distance=dist)
            if len(hits):
                reason = key
                break
        if reason:
            stats[reason] += 1
            continue
        kept.append((x, y))
    return kept, stats


def generate_boulders_from_dem(dem_tif: Path, temp_dir: Path, *, log=None) -> Path | None:
    from app.pipeline.gdal_cli_raster import read_float32_geotiff

    log_step(log, "Hledám balvany na DEM (jen výrazné)")
    try:
        arr, gt, nodata = read_float32_geotiff(dem_tif, log=log)
    except Exception as exc:
        if log:
            log(f"Balvany DEM: nelze číst {dem_tif.name} ({exc}) – přeskočeno")
        return None
    found = detect_boulders(arr, gt, nodata=nodata)
    dest = write_knoll_points_dxf(found, Path(temp_dir) / DXF_BOULDERS, layer="BOULDER")
    if log:
        log(
            f"Balvany DEM: {len(found)} kandidátů 204 "
            f"({BOULDER_MIN_HEIGHT_M:g}–{BOULDER_MAX_HEIGHT_M:g} m) → temp/"
        )
    return dest


def generate_job_boulders(work_dir: Path, *, options: dict | None = None, log=None) -> Path | None:
    """Balvany do ``work/temp/``. Srázy vypnuté ve formuláři (``kp_cliff_symbol=off``) = nic."""
    work_dir = Path(work_dir)
    cliff_symbol = str((options or {}).get("kp_cliff_symbol") or "auto").strip().lower()
    if cliff_symbol == "off":
        (work_dir / "temp" / DXF_BOULDERS).unlink(missing_ok=True)
        return None
    dem = dem_filled_path(work_dir)
    if dem is None:
        if log:
            log_issue(log, "Balvany: chybí DEM (dem_filled.tif) – přeskočeno")
        return None
    return generate_boulders_from_dem(dem, work_dir / "temp", log=log)
