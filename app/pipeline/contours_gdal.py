from __future__ import annotations

from pathlib import Path

from app.pipeline.georef import png_pixel_size, read_pgw
from app.pipeline.oom_import import (
    OomObjectPart,
    _geom_parts_to_objects,
    _pyogrio_layer_rows,
    _wkb_parts,
)
from app.pipeline.oom_symbol_map import symbol_index_for_code
from app.pipeline.prepare_lidar import find_tool, run_cmd
from app.pipeline.reference_layers import _fill_dem_nodata, _pdal_dem_from_laz

# Vyladěno na job forest 1:10000 (srovnání KP vs GDAL).
_CELL_M_AT_SF1 = 2.0
_SMOOTH_WINDOW_M_AT_SF1 = 4.0
_CHAIKIN_ITERS = 1


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


def _extent_from_pullautus(png: Path, pgw: Path) -> tuple[float, float, float, float]:
    georef = read_pgw(pgw)
    width, height = png_pixel_size(png)
    xmin = georef.origin_x
    ymax = georef.origin_y
    xmax = xmin + width * georef.pixel_x
    ymin = ymax + height * georef.pixel_y
    return min(xmin, xmax), min(ymin, ymax), max(xmin, xmax), max(ymin, ymax)


def _smooth_dem(
    src: Path,
    dest: Path,
    *,
    cell_m: float,
    window_m: float,
    log,
) -> Path:
    gdalwarp = find_tool("gdalwarp")
    coarse_m = max(cell_m * 2.0, float(window_m))
    coarse = dest.with_name(dest.stem + "_coarse.tif")
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
            "cubicspline",
            "-tr",
            str(cell_m),
            str(cell_m),
            "-of",
            "GTiff",
            "-overwrite",
            str(coarse),
            str(dest),
        ],
        log=log,
    )
    return dest


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
    sf = float(scalefactor) if scalefactor else 1.0
    cell_m = _CELL_M_AT_SF1 * sf
    window_m = _SMOOTH_WINDOW_M_AT_SF1 * sf
    # OOM: jen plná ekvidistance (+ index). Formline (poloviční krok) necháváme KP PNG.
    del formline
    step = float(interval_m)
    work = dest_shp.parent
    work.mkdir(parents=True, exist_ok=True)
    dem_raw = work / "dem_raw.tif"
    dem_filled = work / "dem_filled.tif"
    dem_smooth = work / "dem_smooth.tif"
    _pdal_dem_from_laz(
        laz, bounds, dem_raw, resolution_m=cell_m, log=log
    )
    _fill_dem_nodata(dem_raw, dem_filled, log=log)
    _smooth_dem(
        dem_filled, dem_smooth, cell_m=cell_m, window_m=window_m, log=log
    )
    gdal_contour = find_tool("gdal_contour")
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        dest_shp.with_suffix(suffix).unlink(missing_ok=True)
    run_cmd(
        [
            gdal_contour,
            "-a",
            "elev",
            "-i",
            str(step),
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
    png = work_dir / "pullautus.png"
    pgw = work_dir / "pullautus.pgw"
    if png.is_file() and pgw.is_file():
        bounds = _extent_from_pullautus(png, pgw)
    elif crop_bounds:
        bounds = crop_bounds
    else:
        raise RuntimeError("Chybí extent pro GDAL vrstevnice (PNG/PGW nebo crop)")
    dest = work_dir / "contours" / "contours.shp"
    del formline
    if log:
        log(
            f"Vrstevnice GDAL: interval {interval_m:g} m "
            f"(bez formline do OOM), DEM {_CELL_M_AT_SF1 * scalefactor:g} m"
        )
    return generate_contours_shapefile(
        laz,
        bounds,
        dest,
        interval_m=interval_m,
        formline=0,
        scalefactor=scalefactor,
        log=log,
    )


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
        symbol_index = symbol_index_for_code(preset_id, scale, code)
        if symbol_index is None:
            symbol_index = symbol_index_for_code(preset_id, scale, "101")
        if symbol_index is None:
            continue
        geom_parts, _ = _wkb_parts(wkb)
        smoothed: list = []
        for part in geom_parts:
            if part[0] == "line":
                pts = chaikin(list(part[1]))  # type: ignore[arg-type]
                smoothed.append(("line", pts, part[2]))
            else:
                smoothed.append(part)
        grouped[code if code in grouped else "101"].extend(
            _geom_parts_to_objects(
                smoothed,
                symbol_index,
                ref_x=ref_x,
                ref_y=ref_y,
                scale=scale,
                grivation_deg=grivation_deg,
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
