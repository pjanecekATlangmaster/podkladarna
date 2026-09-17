#!/usr/bin/env python3
"""Lokální pokusy o hladší lesní vrstevnice (mimo Docker, C:\\QGIS).

  py -3 scripts/try_contour_smooth.py

Výstup: local_test/contour_smooth/*.omap (+ basemap/).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.tool_env import apply_local_gis_env

apply_local_gis_env()

from app.pipeline.build_oom_map import write_oom_map
from app.pipeline.contours_gdal import (
    _elev_from_props,
    _iter_contour_rows,
    _smooth_dem,
    chaikin,
    contour_dem_params,
    contour_line_params,
    contour_oom_code,
    refine_contour_polylines,
)
from app.pipeline.crs_5514 import projected_to_wgs84
from app.pipeline.georef import projected_center_from_raster
from app.pipeline.oom_georef import oom_north_angles
from app.pipeline.oom_import import OomObjectPart, _geom_parts_to_objects, _wkb_parts
from app.pipeline.oom_layers import OomTemplate
from app.pipeline.oom_symbol_map import symbol_index_for_code
from app.pipeline.prepare_lidar import find_tool, run_cmd

JOB = Path("//192.168.10.11/docker/podkladarna/data/jobs/b7bc29d542f1/work")
OUT = ROOT / "local_test" / "contour_smooth"
INTERVAL = 5.0
SCALE = 10000
PRESET = "forest_10000"
INDEX_M = 25.0

VARIANTS = [
    # E – líbí se spojování přes DEM blur 6 m
    ("E_cell15_win6_ch2", 1.5, 6.0, 2),
    # Nový default: 1 m buňka + 6 m blur (+ stitch/minlen až v OOM build)
    ("F_new_cell1_win6_ch2", *contour_dem_params(1.0, INTERVAL)[:3]),
]


def log(msg: str) -> None:
    print(msg)


def contour_from_dem(dem: Path, dest_shp: Path, *, cell_m: float, window_m: float) -> None:
    work = dest_shp.parent
    work.mkdir(parents=True, exist_ok=True)
    dem_smooth = work / "dem_smooth.tif"
    _smooth_dem(dem, dem_smooth, cell_m=cell_m, window_m=window_m, log=log)
    gdal_contour = find_tool("gdal_contour")
    for p in work.glob("contours.*"):
        p.unlink()
    run_cmd(
        [
            gdal_contour,
            "-a",
            "elev",
            "-i",
            str(INTERVAL),
            "-f",
            "ESRI Shapefile",
            "-nln",
            "contours",
            str(dem_smooth),
            str(dest_shp),
        ],
        log=log,
    )


def write_omap(
    shp: Path,
    dest: Path,
    *,
    png: Path,
    pgw: Path,
    chaikin_iters: int,
    label: str,
    refine: bool,
) -> None:
    ref_x, ref_y = projected_center_from_raster(png, pgw)
    ref_lat, ref_lon = projected_to_wgs84(ref_x, ref_y)
    _, grivation = oom_north_angles(ref_x, ref_y)
    idx101 = symbol_index_for_code(PRESET, SCALE, "101")
    idx102 = symbol_index_for_code(PRESET, SCALE, "102")
    if idx101 is None or idx102 is None:
        raise RuntimeError("Chybí symbol 101/102")

    min_len, gap = contour_line_params(1.0, INTERVAL)
    by_key: dict[tuple[str, float | None], list] = {}
    for props, wkb in _iter_contour_rows(shp):
        elev = _elev_from_props(props)
        code = (
            contour_oom_code(elev, interval_m=INTERVAL, index_m=INDEX_M)
            if elev is not None
            else "101"
        )
        geom_parts, _ = _wkb_parts(wkb)
        bucket = by_key.setdefault((code, elev), [])
        for part in geom_parts:
            if part[0] == "line" and len(part[1]) >= 2:
                bucket.append(list(part[1]))

    grouped: dict[str, list[str]] = {"101": [], "102": []}
    for (code, _elev), lines in by_key.items():
        symbol_index = idx102 if code == "102" else idx101
        use = (
            refine_contour_polylines(lines, min_length_m=min_len, stitch_gap_m=gap)
            if refine
            else lines
        )
        smoothed = []
        for pts in use:
            pts = chaikin(pts, iterations=chaikin_iters)
            if len(pts) >= 2:
                smoothed.append(("line", pts, False))
        grouped[code if code in grouped else "101"].extend(
            _geom_parts_to_objects(
                smoothed,
                symbol_index,
                ref_x=ref_x,
                ref_y=ref_y,
                scale=SCALE,
                grivation_deg=grivation,
            )
        )

    basemap = dest.parent / "basemap"
    basemap.mkdir(exist_ok=True)
    if not (basemap / "pullautus.png").is_file():
        shutil.copy2(png, basemap / "pullautus.png")
        shutil.copy2(pgw, basemap / "pullautus.pgw")

    parts = []
    for code, name in (("101", "Vrstevnice"), ("102", "Indexové")):
        objs = grouped[code]
        if objs:
            parts.append(
                OomObjectPart(
                    name=f"{name} {label}",
                    objects_xml="\n".join(objs),
                    count=len(objs),
                )
            )
    write_oom_map(
        dest,
        map_name=f"contour {label}",
        scale=SCALE,
        ref_x=ref_x,
        ref_y=ref_y,
        ref_lat=ref_lat,
        ref_lon=ref_lon,
        templates=[
            OomTemplate(
                "image",
                "Karttapullautin PNG",
                "basemap/pullautus.png",
                visible=True,
                opacity=0.55,
            )
        ],
        preset_id=PRESET,
        object_parts=parts,
        grivation=grivation,
    )
    n = sum(p.count for p in parts)
    log(f"OK {dest.name}: {n} objektů (refine={refine})")


def main() -> None:
    png = JOB / "pullautus.png"
    pgw = JOB / "pullautus.pgw"
    dem = JOB / "contours" / "dem_filled.tif"
    for p in (png, pgw, dem):
        if not p.is_file():
            raise SystemExit(f"Chybí {p}")

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    log(f"gdalwarp={find_tool('gdalwarp')}")
    log(f"gdal_contour={find_tool('gdal_contour')}")
    log(f"new default forest: {contour_dem_params(1.0, INTERVAL)}")

    for name, cell, window, ch in VARIANTS:
        log(f"=== {name}: cell={cell} window={window} chaikin={ch} ===")
        work = OUT / name
        shp = work / "contours.shp"
        contour_from_dem(dem, shp, cell_m=cell, window_m=window)
        write_omap(
            shp,
            OUT / f"{name}.omap",
            png=png,
            pgw=pgw,
            chaikin_iters=ch,
            label=name,
            refine=name.startswith("F_"),
        )
    log(f"Hotovo: {OUT}")


if __name__ == "__main__":
    main()
