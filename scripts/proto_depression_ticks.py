"""Lokální prototyp: deprese→101.1, vyvýšeniny→103 na existujícím jobu.

Nepouští celou pipeline ani NAS. Kreslí diagnostické PNG (jámy vs 103).

Usage (QGIS Python – má osgeo):
  $env:GDAL_DATA='C:\\QGIS\\share\\gdal'
  $env:PROJ_DATA='C:\\QGIS\\share\\proj'
  $env:PYTHONPATH='C:\\Users\\PetrJanecek\\.cursor\\projects\\podkladarna-agent'
  & 'C:\\QGIS\\apps\\Python312\\python.exe' scripts/proto_depression_ticks.py
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pipeline.cliff_height import classify_closed_ring_relief
from app.pipeline.contours_gdal import (
    chaikin,
    contour_dem_params,
    contour_line_params,
    contour_simplify_tol_m,
    contour_tick_thresholds,
    is_closed_polyline,
    place_closed_contour_ticks,
    polyline_length_m,
    refine_contour_polylines,
    simplify_contour_polyline,
    _inset_ring,
    _iter_contour_rows,
)
from app.pipeline.oom_import import _load_cliff_dem, _wkb_parts


def _collect_closed_rings(work: Path, *, interval_m: float = 5.0, scale: int = 10000):
    shp = work / "contours" / "contours.shp"
    sf = float(scale) / 10000.0
    _cell, _window, chaikin_iters = contour_dem_params(sf, interval_m)
    min_len, stitch_gap = contour_line_params(sf, interval_m)
    simplify_tol = contour_simplify_tol_m(scale)
    lines: list[list[tuple[float, float]]] = []
    for props, wkb in _iter_contour_rows(shp):
        geom_parts, _ = _wkb_parts(wkb)
        for part in geom_parts:
            if part[0] == "line" and len(part[1]) >= 2:
                lines.append(list(part[1]))  # type: ignore[arg-type]
    refined = refine_contour_polylines(
        lines, min_length_m=min_len, stitch_gap_m=stitch_gap
    )
    closed: list[list[tuple[float, float]]] = []
    for pts in refined:
        pts = chaikin(pts, iterations=chaikin_iters)
        pts = simplify_contour_polyline(pts, simplify_tol)
        if is_closed_polyline(pts):
            closed.append(pts)
    return closed


def _bbox(rings: list[list[tuple[float, float]]], pad: float = 40.0):
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad


def _draw_overview(
    dest: Path,
    *,
    closed: list,
    depressions: list,
    elevations: list,
    ticks: list,
    formlines: list,
    hillshade: Path | None,
    title: str,
):
    from PIL import Image, ImageDraw, ImageFont

    all_rings = closed or [r for r, _ in depressions + elevations]
    if not all_rings:
        raise SystemExit("Žádné uzavřené vrstevnice")
    xmin, ymin, xmax, ymax = _bbox(all_rings, pad=80.0)
    span = max(xmax - xmin, ymax - ymin, 1.0)
    size = 1600
    scale = (size - 40) / span
    w = int((xmax - xmin) * scale) + 40
    h = int((ymax - ymin) * scale) + 40

    def to_px(x: float, y: float) -> tuple[int, int]:
        # S-JTSK Y roste na sever; PNG Y dolů
        return (
            int(20 + (x - xmin) * scale),
            int(20 + (ymax - y) * scale),
        )

    img = Image.new("RGB", (w, h), (248, 246, 240))
    if hillshade and hillshade.is_file():
        try:
            from osgeo import gdal

            ds = gdal.Open(str(hillshade))
            if ds is not None:
                arr = ds.ReadAsArray()
                gt = ds.GetGeoTransform()
                # rough paste of AOI crop
                import numpy as np

                if arr.ndim == 3:
                    arr = arr[0]
                hs = Image.fromarray(arr.astype("uint8")).convert("RGB")
                # world → image for hillshade full extent; resize to canvas
                hs = hs.resize((w, h))
                img = Image.blend(img, hs, 0.55)
                del gt, np
        except Exception:
            pass

    draw = ImageDraw.Draw(img)

    for ring in closed:
        pts = [to_px(x, y) for x, y in ring]
        draw.line(pts, fill=(180, 180, 180), width=1)

    for ring, relief in elevations:
        inset = _inset_ring(ring, factor=0.55)
        pts = [to_px(x, y) for x, y in ring]
        draw.line(pts, fill=(40, 120, 60), width=2)
        ipts = [to_px(x, y) for x, y in inset]
        # dashed formline 103
        for i in range(0, max(len(ipts) - 1, 0), 2):
            draw.line([ipts[i], ipts[min(i + 1, len(ipts) - 1)]], fill=(20, 90, 40), width=2)
        formlines.append(inset)
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        draw.text(to_px(cx, cy), f"103\n{relief.relief_m:.1f}m", fill=(10, 70, 30))

    for ring, relief in depressions:
        pts = [to_px(x, y) for x, y in ring]
        draw.line(pts, fill=(180, 40, 40), width=2)
        for x, y, tx, ty in place_closed_contour_ticks(ring, toward_centroid=True):
            p0 = to_px(x, y)
            # krátký háček směrem dovnitř
            dx, dy = tx - x, ty - y
            L = math.hypot(dx, dy) or 1.0
            tick_m = 8.0
            p1 = to_px(x + dx / L * tick_m, y + dy / L * tick_m)
            draw.line([p0, p1], fill=(200, 20, 20), width=2)
            ticks.append((x, y))
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        draw.text(to_px(cx, cy), f"101.1\n{relief.relief_m:.1f}m", fill=(140, 0, 0))

    draw.rectangle([8, 8, w - 8, 52], fill=(255, 255, 255))
    draw.text(
        (16, 14),
        f"{title}  |  red=deprese 101.1  green=vyvýšenina 103  gray=ostatní closed",
        fill=(20, 20, 20),
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG")
    print(f"Wrote {dest} ({w}x{h})")


def _zoom_crop(
    dest: Path,
    *,
    focus_rings: list,
    closed: list,
    depressions: list,
    elevations: list,
    label: str,
    pad: float = 60.0,
):
    from PIL import Image, ImageDraw

    if not focus_rings:
        return
    xmin, ymin, xmax, ymax = _bbox(focus_rings, pad=pad)
    span = max(xmax - xmin, ymax - ymin, 1.0)
    size = 900
    scale = (size - 40) / span
    w = int((xmax - xmin) * scale) + 40
    h = int((ymax - ymin) * scale) + 40

    def to_px(x: float, y: float) -> tuple[int, int]:
        return (
            int(20 + (x - xmin) * scale),
            int(20 + (ymax - y) * scale),
        )

    img = Image.new("RGB", (w, h), (250, 248, 242))
    draw = ImageDraw.Draw(img)
    for ring in closed:
        pts = [to_px(x, y) for x, y in ring]
        # only draw if intersects bbox
        if max(p[0] for p in pts) < 0 or min(p[0] for p in pts) > w:
            continue
        draw.line(pts, fill=(200, 200, 200), width=1)
    for ring, relief in elevations:
        pts = [to_px(x, y) for x, y in ring]
        draw.line(pts, fill=(40, 120, 60), width=3)
        inset = _inset_ring(ring, factor=0.55)
        ipts = [to_px(x, y) for x, y in inset]
        for i in range(0, max(len(ipts) - 1, 0), 2):
            draw.line([ipts[i], ipts[min(i + 1, len(ipts) - 1)]], fill=(20, 90, 40), width=3)
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        draw.text(to_px(cx, cy), f"103 {relief.relief_m:.1f}m", fill=(10, 70, 30))
    for ring, relief in depressions:
        pts = [to_px(x, y) for x, y in ring]
        draw.line(pts, fill=(180, 40, 40), width=3)
        for x, y, tx, ty in place_closed_contour_ticks(ring, toward_centroid=True):
            p0 = to_px(x, y)
            dx, dy = tx - x, ty - y
            L = math.hypot(dx, dy) or 1.0
            p1 = to_px(x + dx / L * 10.0, y + dy / L * 10.0)
            draw.line([p0, p1], fill=(220, 20, 20), width=3)
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        draw.text(to_px(cx, cy), f"101.1 {relief.relief_m:.1f}m", fill=(140, 0, 0))
    draw.text((12, 10), label, fill=(0, 0, 0))
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG")
    print(f"Wrote {dest}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--job",
        type=Path,
        default=ROOT / "data" / "jobs" / "0822b22a8d62",
        help="Job s dem_filled + contours (default: roky)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path(
            r"C:\Users\PetrJanecek\AppData\Local\Cursor\AgentStores"
            r"\cursor_agent_stores\bc-46a0148d-9aac-4db3-a0e8-bee3ed81630b"
            r"\files\media\depression-ticks"
        ),
    )
    ap.add_argument("--interval", type=float, default=5.0)
    args = ap.parse_args()
    work = args.job / "work"
    if not (work / "dem" / "dem_filled.tif").is_file():
        raise SystemExit(f"Chybí dem_filled: {work}")
    if not (work / "contours" / "contours.shp").is_file():
        raise SystemExit(f"Chybí contours.shp: {work}")

    elev_at = _load_cliff_dem(work)
    if elev_at is None:
        raise SystemExit("Nelze otevřít dem_filled (osgeo/GDAL?)")

    closed = _collect_closed_rings(work, interval_m=args.interval)
    min_dep, min_elev, min_dep_len, min_elev_len = contour_tick_thresholds(
        args.interval
    )
    depressions = []
    elevations = []
    skipped = 0
    for ring in closed:
        perim = polyline_length_m(ring)
        relief = classify_closed_ring_relief(
            ring,
            elev_at,
            min_depression_m=min_dep,
            min_elevation_m=min_elev,
        )
        if relief is None:
            skipped += 1
            continue
        if relief.kind == "depression":
            if perim < min_dep_len:
                skipped += 1
                continue
            depressions.append((ring, relief))
        else:
            if perim < min_elev_len:
                skipped += 1
                continue
            elevations.append((ring, relief))

    summary = {
        "job": str(args.job),
        "aoi": "roky (VRCH31/41)",
        "closed_rings": len(closed),
        "depressions_101_1": len(depressions),
        "elevations_103": len(elevations),
        "skipped_small_or_flat": skipped,
        "thresholds": {
            "min_depression_m": min_dep,
            "min_elevation_m": min_elev,
            "min_depression_perimeter_m": min_dep_len,
            "min_elevation_perimeter_m": min_elev_len,
        },
        "depression_depths_m": sorted(
            [round(r.relief_m, 2) for _, r in depressions], reverse=True
        )[:20],
        "elevation_heights_m": sorted(
            [round(r.relief_m, 2) for _, r in elevations], reverse=True
        )[:20],
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    ticks: list = []
    formlines: list = []
    hs = args.job / "output" / "shade" / "hillshade.png"
    if not hs.is_file():
        hs = work / "shade" / "hillshade.png"
    _draw_overview(
        args.out / "overview-roky.png",
        closed=closed,
        depressions=depressions,
        elevations=elevations,
        ticks=ticks,
        formlines=formlines,
        hillshade=hs if hs.is_file() else None,
        title="Roky closed relief prototype",
    )
    if depressions:
        top = sorted(depressions, key=lambda t: t[1].relief_m, reverse=True)[:3]
        _zoom_crop(
            args.out / "zoom-depressions-101.1.png",
            focus_rings=[r for r, _ in top],
            closed=closed,
            depressions=top,
            elevations=[],
            label="Deprese -> hacky 101.1 (dovnitr)",
        )
    if elevations:
        top_e = sorted(elevations, key=lambda t: t[1].relief_m, reverse=True)[:3]
        _zoom_crop(
            args.out / "zoom-elevations-103.png",
            focus_rings=[r for r, _ in top_e],
            closed=closed,
            depressions=[],
            elevations=top_e,
            label="Vyvyseniny -> formline 103 (ne knoll, ne tick)",
        )
    # combined zoom if both exist near each other – pick deepest + tallest
    if depressions and elevations:
        focus = [depressions[0][0], elevations[0][0]]
        _zoom_crop(
            args.out / "zoom-both-split.png",
            focus_rings=focus,
            closed=closed,
            depressions=depressions[:2],
            elevations=elevations[:2],
            label="Oddelene: cervena 101.1 jamy | zelena 103 vyvyseniny",
            pad=120.0,
        )
    print(f"Media -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
