from __future__ import annotations

import json
import math
from pathlib import Path

from app.pipeline.dem_prep import DEM_DIR_NAME, _pick_ground_laz
from app.pipeline.job_grid import resolve_job_extent
from app.pipeline.oom_import import (
    OomObjectPart,
    _geom_parts_to_objects,
    _pyogrio_layer_rows,
    _wkb_parts,
)
from app.pipeline.oom_symbol_map import symbol_index_for_code
from app.pipeline.prepare_lidar import find_tool, log_step, run_cmd
from app.pipeline.reference_layers import _fill_dem_nodata, _pdal_dem_from_laz

CONTOUR_META_NAME = "contour_meta.json"

# Les: jemnější DEM (1 m) + blur 6 m (spojuje útržky) + Chaikin 1
# (dřív ×2 densifikovalo před Bézier → bloat; ×1 stačí před DP).
# Pipeline OOM: Chaikin → DP simplify (~0,08 mm papíru) → convertToCurves.
# + post: stitch blízkých konců / drop krátkých zbytků.
_CELL_M_AT_SF1 = 1.0
_SMOOTH_WINDOW_M_AT_SF1 = 6.0
_CHAIKIN_ITERS = 1
# Sprint (sf≈0.4): dřív window ~1,6 m → zubaté křivky; držet vyhlazení.
_SPRINT_SF_MAX = 0.55
_SPRINT_CELL_MIN_M = 1.0
_SPRINT_SMOOTH_MIN_M = 4.0
_SPRINT_CHAIKIN_ITERS = 1
# Post-processing linií (metry v terénu).
_MIN_CONTOUR_LEN_M = 15.0
_STITCH_GAP_FACTOR = 1.1  # × smooth window
# Mapper N používá 0,08 mm papíru po křivkách; u nás DP *před* křivkami
# (stejný řád jako Ctrl+M 0,1 mm) → málo handlů, ne 3× Chaikin bloat.
_SIMPLIFY_PAPER_MM = 0.08


def contour_dem_params(
    scalefactor: float,
    interval_m: float,
) -> tuple[float, float, int]:
    """(cell_m, smooth_window_m, chaikin_iters) podle měřítka / ekvidistance."""
    sf = float(scalefactor) if scalefactor else 1.0
    interval = max(float(interval_m), 0.1)
    if sf <= _SPRINT_SF_MAX:
        cell_m = max(_SPRINT_CELL_MIN_M, _CELL_M_AT_SF1 * sf)
        window_m = max(_SPRINT_SMOOTH_MIN_M, 2.0 * interval)
        return cell_m, window_m, _SPRINT_CHAIKIN_ITERS
    cell_m = max(_SPRINT_CELL_MIN_M, _CELL_M_AT_SF1 * sf)
    window_m = max(_SMOOTH_WINDOW_M_AT_SF1 * sf, _SPRINT_SMOOTH_MIN_M)
    return cell_m, window_m, _CHAIKIN_ITERS


def contour_line_params(
    scalefactor: float,
    interval_m: float,
) -> tuple[float, float]:
    """(min_length_m, stitch_gap_m) – čištění linií po gdal_contour."""
    _cell, window_m, _ = contour_dem_params(scalefactor, interval_m)
    interval = max(float(interval_m), 0.1)
    min_len = max(_MIN_CONTOUR_LEN_M, 2.5 * interval)
    gap = max(6.0, _STITCH_GAP_FACTOR * window_m)
    return min_len, gap


def contour_oom_code(
    elev: float,
    *,
    interval_m: float,
    formline: float = 0,
    index_m: float | None = None,
) -> str:
    """101 běžná plná, 102 index (typicky každá 5.).

    Pomocné (103, přerušované) do OOM neexportujeme – KP formline zůstává
    jen pro rastrový náhled. Parametr ``formline`` je ignorován (kompatibilita).
    """
    del formline

    def on_step(step: float) -> bool:
        if step <= 0:
            return False
        return abs(elev - round(elev / step) * step) < 0.05

    if index_m and on_step(index_m) and on_step(interval_m):
        return "102"
    return "101"


def chaikin(
    pts: list[tuple[float, float]], iterations: int = _CHAIKIN_ITERS
) -> list[tuple[float, float]]:
    if iterations <= 0 or len(pts) < 3:
        return pts
    for _ in range(iterations):
        out: list[tuple[float, float]] = [pts[0]]
        for i in range(len(pts) - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            out.append((0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1))
            out.append((0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1))
        out.append(pts[-1])
        pts = out
    return pts


def paper_mm_to_ground_m(threshold_mm: float, scale: int) -> float:
    """Tolerance na papíře (mm) → metry v terénu při daném mapovém měřítku."""
    return float(threshold_mm) * float(scale) / 1000.0


def contour_simplify_tol_m(
    scale: int, *, threshold_mm: float = _SIMPLIFY_PAPER_MM
) -> float:
    """Douglas–Peucker práh před převodem na Bézier (~Mapper 0,08 mm)."""
    return paper_mm_to_ground_m(threshold_mm, scale)


def _point_seg_dist(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    dx = bx - ax
    dy = by - ay
    denom = dx * dx + dy * dy
    if denom < 1e-18:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify_polyline_dp(
    pts: list[tuple[float, float]], tol_m: float
) -> list[tuple[float, float]]:
    """Douglas–Peucker na otevřené polylinii (metry)."""
    if len(pts) <= 2 or tol_m <= 0:
        return pts
    max_d = -1.0
    idx = 0
    ax, ay = pts[0]
    bx, by = pts[-1]
    for i in range(1, len(pts) - 1):
        d = _point_seg_dist(pts[i][0], pts[i][1], ax, ay, bx, by)
        if d > max_d:
            max_d = d
            idx = i
    if max_d > tol_m:
        left = simplify_polyline_dp(pts[: idx + 1], tol_m)
        right = simplify_polyline_dp(pts[idx:], tol_m)
        return left[:-1] + right
    return [pts[0], pts[-1]]


def simplify_contour_polyline(
    pts: list[tuple[float, float]], tol_m: float
) -> list[tuple[float, float]]:
    """DP simplify; uzavřené prstence rozdělí na dvě půlky (jako cliff merge)."""
    if len(pts) < 3 or tol_m <= 0:
        return pts
    closed = is_closed_polyline(pts) or (
        len(pts) >= 4 and pts[0][0] == pts[-1][0] and pts[0][1] == pts[-1][1]
    )
    if not closed:
        return simplify_polyline_dp(pts, tol_m)

    ring = list(pts[:-1]) if pts[0] == pts[-1] else list(pts)
    if len(ring) <= 4:
        out = ring
    else:
        ax, ay = ring[0]
        far = max(
            range(1, len(ring)),
            key=lambda i: (ring[i][0] - ax) ** 2 + (ring[i][1] - ay) ** 2,
        )
        first = simplify_polyline_dp(ring[: far + 1], tol_m)
        second = simplify_polyline_dp(ring[far:] + [ring[0]], tol_m)
        out = first[:-1] + second[:-1]
        if len(out) < 3:
            out = ring
    if out[0] != out[-1]:
        out = out + [out[0]]
    return out


def polyline_length_m(pts: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        total += math.hypot(x1 - x0, y1 - y0)
    return total


def _endpoints_close(
    a: tuple[float, float], b: tuple[float, float], gap_m: float
) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= gap_m


def is_closed_polyline(
    pts: list[tuple[float, float]], *, tol_m: float = 0.75
) -> bool:
    if len(pts) < 4:
        return False
    return _endpoints_close(pts[0], pts[-1], tol_m)


def stitch_open_polylines(
    lines: list[list[tuple[float, float]]],
    *,
    gap_m: float,
) -> list[list[tuple[float, float]]]:
    """Spojí otevřené linie stejné výšky, jejichž konce jsou do ``gap_m``.

    Uzavřené prstence nechá beze změny. Po spojení uzavře linii, pokud
    její vlastní konce padnou do mezery.
    """
    if gap_m <= 0 or not lines:
        return [list(p) for p in lines if len(p) >= 2]

    closed: list[list[tuple[float, float]]] = []
    open_lines: list[list[tuple[float, float]]] = []
    for raw in lines:
        pts = list(raw)
        if len(pts) < 2:
            continue
        if is_closed_polyline(pts):
            if pts[0] != pts[-1]:
                pts = pts + [pts[0]]
            closed.append(pts)
        else:
            open_lines.append(pts)

    changed = True
    while changed and len(open_lines) > 1:
        changed = False
        best: tuple[float, int, int, str, str] | None = None
        for i in range(len(open_lines)):
            for j in range(i + 1, len(open_lines)):
                a, b = open_lines[i], open_lines[j]
                candidates = (
                    (
                        math.hypot(a[0][0] - b[0][0], a[0][1] - b[0][1]),
                        "start",
                        "start",
                    ),
                    (
                        math.hypot(a[0][0] - b[-1][0], a[0][1] - b[-1][1]),
                        "start",
                        "end",
                    ),
                    (
                        math.hypot(a[-1][0] - b[0][0], a[-1][1] - b[0][1]),
                        "end",
                        "start",
                    ),
                    (
                        math.hypot(a[-1][0] - b[-1][0], a[-1][1] - b[-1][1]),
                        "end",
                        "end",
                    ),
                )
                for dist, ea, eb in candidates:
                    if dist > gap_m:
                        continue
                    if best is None or dist < best[0]:
                        best = (dist, i, j, ea, eb)
        if best is None:
            break
        _dist, i, j, ea, eb = best
        a = open_lines[i]
        b = open_lines[j]
        if ea == "start":
            a = list(reversed(a))
        if eb == "end":
            b = list(reversed(b))
        merged = a + b[1:] if _endpoints_close(a[-1], b[0], gap_m) else a + b
        open_lines.pop(j)
        open_lines.pop(i)
        open_lines.append(merged)
        changed = True

    out = closed
    for pts in open_lines:
        if len(pts) >= 3 and _endpoints_close(pts[0], pts[-1], gap_m):
            if pts[0] != pts[-1]:
                pts = pts + [pts[0]]
        out.append(pts)
    return out


def filter_short_polylines(
    lines: list[list[tuple[float, float]]],
    *,
    min_length_m: float,
) -> list[list[tuple[float, float]]]:
    if min_length_m <= 0:
        return lines
    kept: list[list[tuple[float, float]]] = []
    for pts in lines:
        if len(pts) < 2:
            continue
        if polyline_length_m(pts) < min_length_m:
            continue
        kept.append(pts)
    return kept


def refine_contour_polylines(
    lines: list[list[tuple[float, float]]],
    *,
    min_length_m: float,
    stitch_gap_m: float,
) -> list[list[tuple[float, float]]]:
    """Stitch → drop krátkých (před Chaikinem)."""
    stitched = stitch_open_polylines(lines, gap_m=stitch_gap_m)
    return filter_short_polylines(stitched, min_length_m=min_length_m)


def _smooth_dem(
    src: Path,
    dest: Path,
    *,
    cell_m: float,
    window_m: float,
    log,
) -> Path:
    """Average na okno, lehký mid-average, cubicspline na jemnou buňku."""
    gdalwarp = find_tool("gdalwarp")
    coarse_m = max(cell_m * 2.0, float(window_m))
    mid_m = max(cell_m * 1.5, float(window_m) * 0.55)
    coarse = dest.with_name(dest.stem + "_coarse.tif")
    mid = dest.with_name(dest.stem + "_mid.tif")
    log_step(log, "Vyhlazuji DEM (plynulejší vrstevnice)")
    run_cmd(
        [
            gdalwarp,
            "-r",
            "average",
            "-tr",
            str(coarse_m),
            str(coarse_m),
            "-of",
            "GTiff",
            "-overwrite",
            str(src),
            str(coarse),
        ],
        log=log,
    )
    run_cmd(
        [
            gdalwarp,
            "-r",
            "average",
            "-tr",
            str(mid_m),
            str(mid_m),
            "-of",
            "GTiff",
            "-overwrite",
            str(coarse),
            str(mid),
        ],
        log=log,
    )
    run_cmd(
        [
            gdalwarp,
            "-r",
            "cubicspline",
            "-tr",
            str(cell_m),
            str(cell_m),
            "-of",
            "GTiff",
            "-overwrite",
            str(mid),
            str(dest),
        ],
        log=log,
    )
    return dest


def shared_dem_filled(work_dir: Path) -> Path | None:
    """Kanonický filled DEM z ``dem_prep`` (DMR) – stejný zdroj jako hillshade."""
    path = Path(work_dir) / DEM_DIR_NAME / "dem_filled.tif"
    if path.is_file() and path.stat().st_size > 500:
        return path
    return None


def _write_contour_meta(
    contours_dir: Path,
    *,
    dem_source: str,
    dem_path: Path | None,
    interval_m: float,
    scalefactor: float,
) -> Path:
    meta = {
        "dem_source": dem_source,
        "dem_path": str(dem_path) if dem_path else None,
        "surface": "DMR",
        "interval_m": float(interval_m),
        "scalefactor": float(scalefactor),
        "single_truth": True,
    }
    path = contours_dir / CONTOUR_META_NAME
    path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_contour_meta(work_dir: Path) -> dict | None:
    path = Path(work_dir) / "contours" / CONTOUR_META_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def qa_contours_vs_shared_dem(work_dir: Path, *, log=None) -> bool:
    """Levná QA: vrstevnice sedí na stejném dem_filled jako shade (ne DMP)."""
    dem = shared_dem_filled(work_dir)
    meta = load_contour_meta(work_dir)
    if dem is None:
        if log:
            log("QA vrstevnice↔shade: chybí dem/dem_filled.tif")
        return False
    if not meta:
        if log:
            log("QA vrstevnice↔shade: chybí contour_meta.json")
        return False
    src = str(meta.get("dem_source") or "")
    path_txt = str(meta.get("dem_path") or "")
    ok = src == "shared_dem" and (
        not path_txt or Path(path_txt).resolve() == dem.resolve()
    )
    if log:
        if ok:
            log(
                "QA vrstevnice↔hillshade: OK – společný DMR dem_filled "
                f"({dem.name}), ne DMP"
            )
        else:
            log(
                f"QA vrstevnice↔hillshade: VAROVÁNÍ – dem_source={src!r}, "
                f"očekáván shared_dem / {dem.name}"
            )
    return ok


def _run_gdal_contour(
    dem_smooth: Path,
    dest_shp: Path,
    *,
    interval_m: float,
    log=None,
) -> Path:
    gdal_contour = find_tool("gdal_contour")
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        dest_shp.with_suffix(suffix).unlink(missing_ok=True)
    log_step(log, "Kreslím vrstevnice z DEM (ekvidistance do mapy)")
    run_cmd(
        [
            gdal_contour,
            "-a",
            "elev",
            "-i",
            str(float(interval_m)),
            "-nln",
            "contours",
            str(dem_smooth),
            str(dest_shp),
        ],
        log=log,
    )
    if not dest_shp.is_file():
        raise RuntimeError("gdal_contour nevytvořil shapefile")
    return dest_shp


def generate_contours_from_dem(
    dem_filled: Path,
    dest_shp: Path,
    *,
    interval_m: float,
    scalefactor: float,
    log=None,
) -> Path:
    """Vrstevnice z filled DMR DEM (sdílený dem_prep / shade) – nikdy z DMP."""
    cell_m, window_m, _ = contour_dem_params(scalefactor, interval_m)
    work = dest_shp.parent
    work.mkdir(parents=True, exist_ok=True)
    dem_smooth = work / "dem_smooth.tif"
    _smooth_dem(
        dem_filled, dem_smooth, cell_m=cell_m, window_m=window_m, log=log
    )
    return _run_gdal_contour(
        dem_smooth, dest_shp, interval_m=interval_m, log=log
    )


def generate_contours_shapefile(
    laz: Path,
    bounds: tuple[float, float, float, float],
    dest_shp: Path,
    *,
    interval_m: float,
    formline: float,
    scalefactor: float,
    log=None,
) -> Path:
    """Fallback: DEM z ground LAZ (DMR Classification), ne z vegetace/DMP."""
    cell_m, window_m, _ = contour_dem_params(scalefactor, interval_m)
    # OOM: jen plná ekvidistance (+ index). Formline (poloviční krok) necháváme KP PNG.
    del formline
    work = dest_shp.parent
    work.mkdir(parents=True, exist_ok=True)
    dem_raw = work / "dem_raw.tif"
    dem_filled = work / "dem_filled.tif"
    dem_smooth = work / "dem_smooth.tif"
    log_step(log, "Připravuji DEM z ground LAZ (záložní terén pro vrstevnice)")
    _pdal_dem_from_laz(laz, bounds, dem_raw, resolution_m=cell_m, log=log)
    _fill_dem_nodata(dem_raw, dem_filled, log=log)
    _smooth_dem(
        dem_filled, dem_smooth, cell_m=cell_m, window_m=window_m, log=log
    )
    return _run_gdal_contour(
        dem_smooth, dest_shp, interval_m=interval_m, log=log
    )


def generate_job_contours(
    work_dir: Path,
    laz: Path,
    *,
    interval_m: float,
    formline: float,
    scalefactor: float,
    crop_bounds: tuple[float, float, float, float] | None,
    log=None,
) -> Path:
    """Jediná pravda vrstevnic: GDAL z DMR (sdílený dem_filled), ne KP DXF / DMP."""
    # Preferuj kanonickou job_grid mřížku; pullautus jen jako legacy fallback.
    bounds = resolve_job_extent(work_dir, crop_bounds=crop_bounds)
    dest = work_dir / "contours" / "contours.shp"
    del formline
    cell_m, window_m, iters = contour_dem_params(scalefactor, interval_m)
    min_len, gap = contour_line_params(scalefactor, interval_m)
    shared = shared_dem_filled(work_dir)
    if log:
        log(
            f"Vrstevnice GDAL: interval {interval_m:g} m "
            f"(bez formline do OOM), DEM {cell_m:g} m, smooth {window_m:g} m, "
            f"Chaikin {iters}, min_délka {min_len:g} m, stitch {gap:g} m"
        )
    if shared is not None:
        if log:
            log(
                f"Vrstevnice: sdílený DMR dem_filled ({shared.as_posix()}) "
                "– stejný zdroj jako hillshade (ne DMP)"
            )
        generate_contours_from_dem(
            shared,
            dest,
            interval_m=interval_m,
            scalefactor=scalefactor,
            log=log,
        )
        _write_contour_meta(
            dest.parent,
            dem_source="shared_dem",
            dem_path=shared,
            interval_m=interval_m,
            scalefactor=scalefactor,
        )
        return dest

    # Fallback jen ground LAZ (DMR); merged dostane Classification[2:2] filtr.
    ground = _pick_ground_laz(work_dir / "lidar") or laz
    if log:
        log(
            f"Vrstevnice: dem_filled chybí – fallback PDAL z {ground.name} "
            "(DMR ground, ne DMP)"
        )
    generate_contours_shapefile(
        ground,
        bounds,
        dest,
        interval_m=interval_m,
        formline=0,
        scalefactor=scalefactor,
        log=log,
    )
    _write_contour_meta(
        dest.parent,
        dem_source="laz_ground_fallback",
        dem_path=ground,
        interval_m=interval_m,
        scalefactor=scalefactor,
    )
    return dest


def _iter_contour_rows(shp: Path):
    """(props, wkb) – v Docker image je osgeo/GDAL, pyogrio tam není."""
    try:
        from osgeo import ogr
    except ImportError:
        ogr = None
    if ogr is not None:
        ds = ogr.Open(str(shp))
        if ds:
            layer = ds.GetLayer(0)
            if layer is not None:
                for feature in layer:
                    geom = feature.GetGeometryRef()
                    if geom is None:
                        continue
                    props: dict[str, object] = {}
                    for i in range(feature.GetFieldCount()):
                        defn = feature.GetFieldDefnRef(i)
                        if defn:
                            props[defn.GetName()] = feature.GetField(i)
                    yield props, bytes(geom.ExportToWkb())
                return
    try:
        import pyogrio
    except ImportError:
        return
    for layer_name, _t in pyogrio.list_layers(shp):
        yield from _pyogrio_layer_rows(shp, layer=layer_name)


def _elev_from_props(props: dict) -> float | None:
    for key in ("elev", "ELEV", "elevation", "HEIGHT"):
        if key not in props:
            continue
        try:
            return float(props[key])
        except (TypeError, ValueError):
            continue
    return None


def build_gdal_contour_parts(
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    interval_m: float,
    formline: float = 0,
    index_m: float | None = None,
    scalefactor: float | None = None,
    log=None,
) -> list[OomObjectPart]:
    del formline
    shp = work_dir / "contours" / "contours.shp"
    if not shp.is_file():
        return []

    grouped: dict[str, list[str]] = {"101": [], "102": []}
    names = {
        "101": "Vrstevnice (GDAL)",
        "102": "Indexové vrstevnice (GDAL)",
    }
    if scalefactor is None:
        scalefactor = float(scale) / 10000.0 if scale else 1.0
    _cell, _window, chaikin_iters = contour_dem_params(scalefactor, interval_m)
    min_len, stitch_gap = contour_line_params(scalefactor, interval_m)

    by_key: dict[tuple[str, float | None], list[list[tuple[float, float]]]] = {}
    for props, wkb in _iter_contour_rows(shp):
        elev = _elev_from_props(props)
        if elev is None:
            code = "101"
        else:
            code = contour_oom_code(
                elev,
                interval_m=interval_m,
                formline=0,
                index_m=index_m,
            )
        geom_parts, _ = _wkb_parts(wkb)
        bucket = by_key.setdefault((code if code in grouped else "101", elev), [])
        for part in geom_parts:
            if part[0] == "line" and len(part[1]) >= 2:
                bucket.append(list(part[1]))  # type: ignore[arg-type]

    simplify_tol = contour_simplify_tol_m(scale)
    log_step(
        log,
        "Importuji vrstevnice do OOM: Chaikin → DP simplify "
        f"({_SIMPLIFY_PAPER_MM:g} mm ≈ {simplify_tol:.2f} m) → Bézier",
    )
    for (code, _elev), lines in by_key.items():
        symbol_index = symbol_index_for_code(preset_id, scale, code)
        if symbol_index is None:
            symbol_index = symbol_index_for_code(preset_id, scale, "101")
        if symbol_index is None:
            continue
        refined = refine_contour_polylines(
            lines, min_length_m=min_len, stitch_gap_m=stitch_gap
        )
        smoothed: list = []
        for pts in refined:
            pts = chaikin(pts, iterations=chaikin_iters)
            pts = simplify_contour_polyline(pts, simplify_tol)
            if len(pts) < 2:
                continue
            closed = is_closed_polyline(pts)
            smoothed.append(("line", pts, closed))
        if not smoothed:
            continue
        grouped[code].extend(
            _geom_parts_to_objects(
                smoothed,
                symbol_index,
                ref_x=ref_x,
                ref_y=ref_y,
                scale=scale,
                grivation_deg=grivation_deg,
                # Křivky až po DP: handly jen na zjednodušených uzlech (ne Chaikin densita).
                as_curves=True,
            )
        )

    parts: list[OomObjectPart] = []
    for code in ("101", "102"):
        objects = grouped[code]
        if objects:
            parts.append(
                OomObjectPart(
                    name=names[code],
                    objects_xml="\n".join(objects),
                    count=len(objects),
                )
            )
    return parts
