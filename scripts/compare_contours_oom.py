#!/usr/bin/env python3
"""Srovnání vrstevnic KP vs PDAL/gdal_contour do dvou .omap.

Příklad:
  python scripts/compare_contours_oom.py ^
    --job-zip "C:\\Users\\PetrJanecek\\Downloads\\podkladarna_1d141a7b3721(1)" ^
    --laz "\\\\192.168.10.11\\docker\\podkladarna\\data\\jobs\\1d141a7b3721\\work\\lidar\\merged_crop.laz" ^
    --kp-dxf "\\\\192.168.10.11\\docker\\podkladarna\\data\\jobs\\1d141a7b3721\\work\\temp\\out2.dxf"
"""
from __future__ import annotations

import argparse
import json
import shutil
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pipeline.build_oom_map import write_oom_map
from app.pipeline.crs_5514 import projected_to_wgs84
from app.pipeline.georef import png_pixel_size, projected_center_from_raster, read_pgw
from app.pipeline.ini_builder import load_presets
from app.pipeline.oom_georef import oom_north_angles
from app.pipeline.oom_import import (
    OomObjectPart,
    _geom_objects,
    _geom_parts_to_objects,
    _pyogrio_layer_rows,
    _wkb_parts,
)
from app.pipeline.oom_layers import OomTemplate
from app.pipeline.oom_symbol_map import symbol_index_for_code
from app.pipeline.prepare_lidar import find_tool, run_cmd
from app.pipeline.reference_layers import _pdal_dem_from_laz, _fill_dem_nodata


def _extent_from_pgw(png: Path, pgw: Path) -> tuple[float, float, float, float]:
    georef = read_pgw(pgw)
    width, height = png_pixel_size(png)
    xmin = georef.origin_x
    ymax = georef.origin_y
    xmax = xmin + width * georef.pixel_x
    ymin = ymax + height * georef.pixel_y
    return min(xmin, xmax), min(ymin, ymax), max(xmin, xmax), max(ymin, ymax)


def _chaikin(
    pts: list[tuple[float, float]], iterations: int = 3
) -> list[tuple[float, float]]:
    """Vyhlazení polyline (Chaikin) – bližší KP než surový gdal_contour."""
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


def _layer_name(props: dict) -> str:
    raw = props.get("Layer") or props.get("layer") or ""
    if hasattr(raw, "decode"):
        raw = raw.decode("utf-8", "ignore")
    return str(raw).strip().lower()


def _elev_on_interval(elev: float | None, interval_m: float) -> bool:
    if elev is None or interval_m <= 0:
        return True
    q = round(elev / interval_m)
    return abs(elev - q * interval_m) < 0.05


def _wkb_line_xy_and_z(buf: bytes) -> tuple[list[tuple[float, float]], float | None]:
    """LineString / LineStringZ → XY + první Z (DXF POLYLINE)."""
    if not buf or len(buf) < 9:
        return [], None
    fmt = "<" if buf[0] == 1 else ">"
    geom_type = struct.unpack_from(fmt + "I", buf, 1)[0]
    has_z = bool(geom_type & 0x80000000) or 1000 <= (geom_type % 3000) < 2000
    n = struct.unpack_from(fmt + "I", buf, 5)[0]
    offset = 9
    pts: list[tuple[float, float]] = []
    z0: float | None = None
    step = 24 if has_z else 16
    for _ in range(n):
        if offset + step > len(buf):
            break
        if has_z:
            x, y, z = struct.unpack_from(fmt + "ddd", buf, offset)
            if z0 is None:
                z0 = z
        else:
            x, y = struct.unpack_from(fmt + "dd", buf, offset)
        pts.append((x, y))
        offset += step
    return pts, z0
    raw = props.get("Layer") or props.get("layer") or ""
    if hasattr(raw, "decode"):
        raw = raw.decode("utf-8", "ignore")
    return str(raw).strip().lower()


def _objects_from_vector(
    path: Path,
    *,
    symbol_index: int,
    ref_x: float,
    ref_y: float,
    scale: int,
    grivation_deg: float,
    chaikin_iters: int = 0,
    skip_layers: frozenset[str] | None = None,
    elev_interval_m: float | None = None,
) -> list[str]:
    objects: list[str] = []
    try:
        from osgeo import ogr
    except ImportError:
        ogr = None

    if ogr is not None:
        ds = ogr.Open(str(path))
        if not ds:
            raise RuntimeError(f"Nelze otevřít {path}")
        for i in range(ds.GetLayerCount()):
            layer = ds.GetLayerByIndex(i)
            if not layer:
                continue
            for feature in layer:
                geom = feature.GetGeometryRef()
                if geom is None:
                    continue
                objects.extend(
                    _geom_objects(
                        geom,
                        symbol_index,
                        ref_x=ref_x,
                        ref_y=ref_y,
                        scale=scale,
                        grivation_deg=grivation_deg,
                        ogr=ogr,
                    )
                )
        return objects

    import pyogrio

    skip = skip_layers or frozenset()
    need_z = elev_interval_m is not None and elev_interval_m > 0
    for layer_name, _layer_type in pyogrio.list_layers(path):
        for props, wkb in _pyogrio_layer_rows(
            path, layer=layer_name, force_2d=not need_z
        ):
            if skip and _layer_name(props) in skip:
                continue
            if need_z:
                pts, z0 = _wkb_line_xy_and_z(bytes(wkb) if wkb is not None else b"")
                if not _elev_on_interval(z0, float(elev_interval_m)):
                    continue
                geom_parts = [("line", pts, False)] if len(pts) >= 2 else []
            else:
                geom_parts, _ = _wkb_parts(wkb)
            if chaikin_iters:
                smoothed: list = []
                for part in geom_parts:
                    if part[0] == "line":
                        pts = _chaikin(list(part[1]), chaikin_iters)  # type: ignore[arg-type]
                        smoothed.append(("line", pts, part[2]))
                    else:
                        smoothed.append(part)
                geom_parts = smoothed
            objects.extend(
                _geom_parts_to_objects(
                    geom_parts,
                    symbol_index,
                    ref_x=ref_x,
                    ref_y=ref_y,
                    scale=scale,
                    grivation_deg=grivation_deg,
                )
            )
    return objects


def _write_contour_omap(
    dest: Path,
    *,
    map_name: str,
    vector_path: Path,
    part_name: str,
    png: Path,
    pgw: Path,
    preset_id: str,
    scale: int,
    chaikin_iters: int = 0,
    skip_layers: frozenset[str] | None = None,
    elev_interval_m: float | None = None,
) -> Path:
    ref_x, ref_y = projected_center_from_raster(png, pgw)
    ref_lat, ref_lon = projected_to_wgs84(ref_x, ref_y)
    _, grivation = oom_north_angles(ref_x, ref_y)
    symbol_index = symbol_index_for_code(preset_id, scale, "101")
    if symbol_index is None:
        raise RuntimeError(f"Preset {preset_id} nemá symbol 101")

    objects = _objects_from_vector(
        vector_path,
        symbol_index=symbol_index,
        ref_x=ref_x,
        ref_y=ref_y,
        scale=scale,
        grivation_deg=grivation,
        chaikin_iters=chaikin_iters,
        skip_layers=skip_layers,
        elev_interval_m=elev_interval_m,
    )
    if not objects:
        raise RuntimeError(f"Žádné objekty z {vector_path}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Relativní cesty v .omap – soubory vedle sebe / v basemap/
    basemap_dir = dest.parent / "basemap"
    basemap_dir.mkdir(exist_ok=True)
    shutil.copy2(png, basemap_dir / "pullautus.png")
    shutil.copy2(pgw, basemap_dir / "pullautus.pgw")

    templates = [
        OomTemplate(
            "image",
            "Karttapullautin PNG",
            "basemap/pullautus.png",
            visible=True,
            opacity=0.55,
        )
    ]
    part = OomObjectPart(
        name=part_name,
        objects_xml="\n".join(objects),
        count=len(objects),
    )
    write_oom_map(
        dest,
        map_name=map_name,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        ref_lat=ref_lat,
        ref_lon=ref_lon,
        templates=templates,
        preset_id=preset_id,
        object_parts=[part],
    )
    print(f"OK {dest.name}: {len(objects)} objektů")
    return dest


def _smooth_dem(
    src: Path,
    dest: Path,
    *,
    cell_m: float,
    window_m: float,
    log,
) -> Path:
    """Nízkofrekvenční vyhlazení DEM (KP má medianboxsize=6 na 2 m buňce ≈ 12 m)."""
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


def build_gdal_contours(
    laz: Path,
    bounds: tuple[float, float, float, float],
    out_shp: Path,
    *,
    interval_m: float,
    resolution_m: float,
    smooth_window_m: float,
    work: Path,
    log,
) -> Path:
    dem_raw = work / "dem_raw.tif"
    dem_filled = work / "dem_filled.tif"
    dem_smooth = work / "dem_smooth.tif"
    _pdal_dem_from_laz(
        laz,
        bounds,
        dem_raw,
        resolution_m=resolution_m,
        log=log,
    )
    _fill_dem_nodata(dem_raw, dem_filled, log=log)
    _smooth_dem(
        dem_filled,
        dem_smooth,
        cell_m=resolution_m,
        window_m=smooth_window_m,
        log=log,
    )

    gdal_contour = find_tool("gdal_contour")
    out_shp.parent.mkdir(parents=True, exist_ok=True)
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        p = out_shp.with_suffix(suffix)
        p.unlink(missing_ok=True)
    run_cmd(
        [
            gdal_contour,
            "-a",
            "elev",
            "-i",
            str(interval_m),
            "-nln",
            "contours",
            str(dem_smooth),
            str(out_shp),
        ],
        log=log,
    )
    if not out_shp.is_file():
        raise RuntimeError("gdal_contour nevytvořil shapefile")
    return out_shp


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--job-zip",
        type=Path,
        required=True,
        help="Rozbalený výstup jobu (basemap/, metadata.json, …)",
    )
    ap.add_argument("--laz", type=Path, required=True, help="merged_crop.laz / ground LAZ")
    ap.add_argument(
        "--kp-dxf",
        type=Path,
        help="KP out2.dxf / contours.dxf (default: job-zip/karttapullautin/contours.dxf)",
    )
    ap.add_argument(
        "-o",
        "--out-dir",
        type=Path,
        default=ROOT / "local_test" / "contour_compare",
    )
    ap.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Ekvidistance [m]. Default: contour_interval_m z jobu (lesní 5 m), bez formlines.",
    )
    ap.add_argument("--resolution", type=float, default=2.0, help="DEM buňka [m]")
    ap.add_argument(
        "--smooth-window",
        type=float,
        default=4.0,
        help="Vyhlazovací okno DEM [m] (menší = víc detailu)",
    )
    ap.add_argument("--chaikin", type=int, default=1, help="Iterace Chaikin na GDAL liniích")
    args = ap.parse_args()

    src = args.job_zip
    png = src / "basemap" / "pullautus.png"
    pgw = src / "basemap" / "pullautus.pgw"
    if not png.is_file() or not pgw.is_file():
        raise SystemExit(f"Chybí {png} nebo {pgw}")

    meta_path = src / "metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    preset_id = meta.get("preset_id") or "forest_10000"
    presets = load_presets()
    preset = presets.get(preset_id, {})
    scale = int(meta.get("scale") or round(float(preset.get("scalefactor", 1)) * 10000))
    interval = float(
        args.interval
        if args.interval is not None
        else meta.get("contour_interval_m") or preset.get("contour_interval") or 5
    )

    kp_dxf = args.kp_dxf
    if kp_dxf is None:
        kp_dxf = src / "karttapullautin" / "contours.dxf"
    if not kp_dxf.is_file():
        raise SystemExit(f"Chybí KP DXF: {kp_dxf}")
    if not args.laz.is_file():
        raise SystemExit(f"Chybí LAZ: {args.laz}")

    out_dir = args.out_dir
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    work = out_dir / "_work"
    work.mkdir()

    def log(msg: str) -> None:
        print(msg)

    bounds = _extent_from_pgw(png, pgw)
    log(f"Extent 5514: {bounds}")
    log(
        f"Preset {preset_id}, scale 1:{scale}, ekvidistance {interval} m "
        f"(KP: vrstva contour + index po {interval:g} m, bez formlines)"
    )

    # KP → omap (kopie DXF pro archiv)
    kp_copy = out_dir / "contours_kp.dxf"
    shutil.copy2(kp_dxf, kp_copy)
    _write_contour_omap(
        out_dir / "contours_kp.omap",
        map_name=f"{meta.get('name') or preset_id} – KP vrstevnice",
        vector_path=kp_copy,
        part_name="Vrstevnice – Karttapullautin",
        png=png,
        pgw=pgw,
        preset_id=preset_id,
        scale=scale,
        skip_layers=frozenset({"contour_intermed", "formline"}),
        elev_interval_m=interval,
    )

    # GDAL/PDAL
    gdal_shp = work / "contours_gdal.shp"
    build_gdal_contours(
        args.laz,
        bounds,
        gdal_shp,
        interval_m=interval,
        resolution_m=args.resolution,
        smooth_window_m=args.smooth_window,
        work=work,
        log=log,
    )
    # trvalá kopie shapefile vedle omap
    for p in work.glob("contours_gdal.*"):
        shutil.copy2(p, out_dir / p.name)

    _write_contour_omap(
        out_dir / "contours_gdal.omap",
        map_name=f"{meta.get('name') or preset_id} – GDAL vrstevnice",
        vector_path=out_dir / "contours_gdal.shp",
        part_name="Vrstevnice – PDAL/gdal_contour",
        png=png,
        pgw=pgw,
        preset_id=preset_id,
        scale=scale,
        chaikin_iters=args.chaikin,
    )

    log(f"Hotovo: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
