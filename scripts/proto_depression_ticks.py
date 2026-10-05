"""Demo closed relief: 101.1 (velke jamy) + konzervativni 103 mid-interval.

Usage (QGIS Python):
  $env:GDAL_DATA='C:\\QGIS\\share\\gdal'
  $env:PROJ_DATA='C:\\QGIS\\share\\proj'
  $env:PYTHONPATH=...\\podkladarna-agent
  python scripts/proto_depression_ticks.py --demo
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
    closed_relief_thresholds,
    contour_dem_params,
    contour_line_params,
    contour_simplify_tol_m,
    is_closed_polyline,
    outermost_depression_rings,
    place_closed_contour_ticks,
    polyline_length_m,
    refine_contour_polylines,
    simplify_contour_polyline,
    _inset_ring,
    _iter_contour_rows,
)
from app.pipeline.oom_import import _load_cliff_dem, _wkb_parts

MEDIA_DEMO = Path(
    r"C:\Users\PetrJanecek\AppData\Local\Cursor\AgentStores"
    r"\cursor_agent_stores\bc-46a0148d-9aac-4db3-a0e8-bee3ed81630b"
    r"\files\media\depression-ticks\demo"
)


def _collect_closed_rings(
    work: Path, *, interval_m: float, scale: int, refine: bool = True
):
    shp = work / "contours" / "contours.shp"
    sf = float(scale) / 10000.0
    _cell, _window, chaikin_iters = contour_dem_params(sf, interval_m)
    min_len, stitch_gap = contour_line_params(sf, interval_m)
    simplify_tol = contour_simplify_tol_m(scale)
    lines: list[list[tuple[float, float]]] = []
    for _props, wkb in _iter_contour_rows(shp):
        geom_parts, _ = _wkb_parts(wkb)
        for part in geom_parts:
            if part[0] == "line" and len(part[1]) >= 2:
                lines.append(list(part[1]))  # type: ignore[arg-type]
    if refine:
        lines = refine_contour_polylines(
            lines, min_length_m=min_len, stitch_gap_m=stitch_gap
        )
    closed: list[list[tuple[float, float]]] = []
    for pts in lines:
        if refine:
            pts = chaikin(pts, iterations=chaikin_iters)
            pts = simplify_contour_polyline(pts, simplify_tol)
        if is_closed_polyline(pts) or (
            len(pts) >= 4 and pts[0] == pts[-1]
        ):
            closed.append(pts)
    return closed


def _classify_rings(closed, elev_at, interval_m: float):
    min_dep, max_dep, min_dep_len, form_lo, form_hi, min_form_len = (
        closed_relief_thresholds(interval_m)
    )
    dep_candidates = []
    formlines = []
    skipped = {
        "small_dep": 0,
        "deep_dep": 0,
        "small_elev": 0,
        "equidist_elev": 0,
        "flat": 0,
        "inner_dep": 0,
    }
    for ring in closed:
        perim = polyline_length_m(ring)
        relief = classify_closed_ring_relief(
            ring,
            elev_at,
            min_depression_m=min_dep,
            min_elevation_m=form_lo,
        )
        if relief is None:
            skipped["flat"] += 1
            continue
        if relief.kind == "depression":
            if relief.relief_m > max_dep:
                skipped["deep_dep"] += 1
                continue
            if perim < min_dep_len:
                skipped["small_dep"] += 1
                continue
            dep_candidates.append((ring, relief))
            continue
        # elevation
        if relief.relief_m > form_hi:
            skipped["equidist_elev"] += 1
            continue
        if perim < min_form_len or relief.relief_m < form_lo:
            skipped["small_elev"] += 1
            continue
        formlines.append((ring, relief))
    depressions = outermost_depression_rings(dep_candidates)
    skipped["inner_dep"] = len(dep_candidates) - len(depressions)
    thresholds = {
        "min_depression_m": min_dep,
        "max_depression_for_101_1_m": max_dep,
        "min_depression_perimeter_m": min_dep_len,
        "formline_height_lo_m": form_lo,
        "formline_height_hi_m": form_hi,
        "min_formline_perimeter_m": min_form_len,
        "ticks": "1-2 opposite; outer ring only",
    }
    return depressions, formlines, skipped, thresholds


def _bbox(rings, pad: float = 40.0):
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad


def _draw(
    dest: Path,
    *,
    closed,
    depressions,
    formlines,
    title: str,
    zoom_rings=None,
    pad: float = 80.0,
):
    from PIL import Image, ImageDraw

    focus = zoom_rings or closed or [r for r, _ in depressions + formlines]
    if not focus:
        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (640, 360), (245, 245, 240)).save(dest)
        print(f"Wrote empty {dest}")
        return
    xmin, ymin, xmax, ymax = _bbox(focus, pad=pad)
    span = max(xmax - xmin, ymax - ymin, 1.0)
    size = 1400 if zoom_rings is None else 900
    scale = (size - 40) / span
    w = max(400, int((xmax - xmin) * scale) + 40)
    h = max(300, int((ymax - ymin) * scale) + 40)

    def to_px(x: float, y: float):
        return (int(20 + (x - xmin) * scale), int(20 + (ymax - y) * scale))

    img = Image.new("RGB", (w, h), (248, 246, 240))
    draw = ImageDraw.Draw(img)
    for ring in closed:
        pts = [to_px(x, y) for x, y in ring]
        draw.line(pts, fill=(190, 190, 190), width=1)
    for ring, relief in formlines:
        pts = [to_px(x, y) for x, y in ring]
        draw.line(pts, fill=(40, 120, 60), width=2)
        inset = _inset_ring(ring, factor=0.55)
        ipts = [to_px(x, y) for x, y in inset]
        for i in range(0, max(len(ipts) - 1, 0), 2):
            draw.line(
                [ipts[i], ipts[min(i + 1, len(ipts) - 1)]],
                fill=(20, 90, 40),
                width=2,
            )
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        draw.text(to_px(cx, cy), f"103 {relief.relief_m:.1f}m", fill=(10, 70, 30))
    for ring, relief in depressions:
        pts = [to_px(x, y) for x, y in ring]
        draw.line(pts, fill=(180, 40, 40), width=2)
        for x, y, tx, ty in place_closed_contour_ticks(ring, toward_centroid=True):
            p0 = to_px(x, y)
            dx, dy = tx - x, ty - y
            L = math.hypot(dx, dy) or 1.0
            # Kratke hacky (~0.75 mm @10k ≈ 7.5 m), ne diametr jamy.
            tick_len = 7.5
            p1 = to_px(x + dx / L * tick_len, y + dy / L * tick_len)
            draw.line([p0, p1], fill=(200, 20, 20), width=3)
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        draw.text(to_px(cx, cy), f"101.1 {relief.relief_m:.1f}m", fill=(140, 0, 0))
    draw.rectangle([6, 6, w - 6, 44], fill=(255, 255, 255))
    draw.text((12, 12), title, fill=(20, 20, 20))
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG")
    print(f"Wrote {dest} ({w}x{h})")


def run_aoi(
    *,
    slug: str,
    work: Path,
    interval_m: float,
    scale: int,
    out: Path,
    label: str,
    refine: bool = True,
):
    import app.pipeline.oom_import as oi

    oi._dem_cache.clear()
    oi._dem_cache_dir = None
    elev_at = _load_cliff_dem(work)
    if elev_at is None:
        raise SystemExit(f"{slug}: no dem_filled in {work}")
    closed = _collect_closed_rings(
        work, interval_m=interval_m, scale=scale, refine=refine
    )
    deps, forms, skipped, thresholds = _classify_rings(
        closed, elev_at, interval_m
    )
    summary = {
        "slug": slug,
        "label": label,
        "work": str(work),
        "interval_m": interval_m,
        "scale": scale,
        "closed_rings": len(closed),
        "depressions_101_1": len(deps),
        "formlines_103": len(forms),
        "skipped": skipped,
        "thresholds": thresholds,
        "depression_depths_m": sorted(
            [round(r.relief_m, 2) for _, r in deps], reverse=True
        )[:20],
        "formline_heights_m": sorted(
            [round(r.relief_m, 2) for _, r in forms], reverse=True
        )[:20],
    }
    _draw(
        out / f"{slug}-overview.png",
        closed=closed,
        depressions=deps,
        formlines=forms,
        title=f"{label} | red=101.1 large pit | green=103 mid-interval hi-conf",
    )
    if deps:
        top = sorted(deps, key=lambda t: polyline_length_m(t[0]), reverse=True)[:3]
        _draw(
            out / f"{slug}-101.1.png",
            closed=closed,
            depressions=top,
            formlines=[],
            title=f"{label}: 101.1 (large area, moderate depth)",
            zoom_rings=[r for r, _ in top],
            pad=50.0,
        )
    if forms:
        top_f = sorted(forms, key=lambda t: t[1].relief_m, reverse=True)[:3]
        _draw(
            out / f"{slug}-103.png",
            closed=closed,
            depressions=[],
            formlines=top_f,
            title=f"{label}: 103 mid-interval (high confidence)",
            zoom_rings=[r for r, _ in top_f],
            pad=50.0,
        )
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="Roky + test vrstevnic")
    ap.add_argument("--out", type=Path, default=MEDIA_DEMO)
    args = ap.parse_args()
    if not args.demo:
        ap.error("Use --demo")

    summaries = []
    # Roky forest 5 m
    summaries.append(
        run_aoi(
            slug="roky",
            work=ROOT / "data" / "jobs" / "0822b22a8d62" / "work",
            interval_m=5.0,
            scale=10000,
            out=args.out,
            label="Roky forest 5m",
            refine=True,
        )
    )
    # Ostrý test vrstevnic – sprint 2 m (contours already from prod ZIP)
    summaries.append(
        run_aoi(
            slug="test-vrstevnic",
            work=ROOT / "data" / "_demo_test_vrstevnic" / "work",
            interval_m=2.0,
            scale=4000,
            out=args.out,
            label="test vrstevnic sprint 2m (prod 19d6d2e756af)",
            refine=False,
        )
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(
        json.dumps(
            {
                "rules": "prefs 2026-10-05: 101.1 large moderate pits; 103 mid-interval hi-conf only",
                "aois": summaries,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(json.dumps(summaries, indent=2, ensure_ascii=False))
    print(f"Media -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
