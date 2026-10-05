"""Demo closed relief + before/after proof for 101.1 tick rules.

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
import time
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
    _ring_centroid,
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
        if is_closed_polyline(pts) or (len(pts) >= 4 and pts[0] == pts[-1]):
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
    return dep_candidates, depressions, formlines, skipped, thresholds


def _dense_ticks_legacy(
    ring: list[tuple[float, float]], *, spacing_m: float = 45.0
) -> list[tuple[float, float, float, float]]:
    """Stary styl: rada hacku podel kontury (pro before-srovnani)."""
    pts = list(ring)
    if pts and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return []
    cx, cy = _ring_centroid(pts)
    total = polyline_length_m(pts + [pts[0]])
    if total < 1.0:
        return []
    n = max(1, int(total / max(spacing_m, 1.0)))
    out = []
    for i in range(n):
        target = (i + 0.5) * total / n
        walked = 0.0
        loop = pts + [pts[0]]
        for (x0, y0), (x1, y1) in zip(loop, loop[1:]):
            seg = math.hypot(x1 - x0, y1 - y0)
            if seg < 1e-6:
                continue
            if walked + seg >= target:
                t = (target - walked) / seg
                x = x0 + t * (x1 - x0)
                y = y0 + t * (y1 - y0)
                out.append((x, y, cx, cy))
                break
            walked += seg
    return out


def _bbox(rings, pad: float = 40.0):
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad


def _draw_scene(
    *,
    closed,
    ring_styles: list[tuple],
    title: str,
    subtitle: str = "",
    zoom_rings=None,
    pad: float = 50.0,
    size: int = 900,
):
    """ring_styles: (ring, outline_rgb, ticks, tick_rgb, label)."""
    from PIL import Image, ImageDraw

    focus = zoom_rings or [r for r, *_ in ring_styles] or closed
    if not focus:
        img = Image.new("RGB", (640, 360), (245, 245, 240))
        return img, 0
    xmin, ymin, xmax, ymax = _bbox(focus, pad=pad)
    span = max(xmax - xmin, ymax - ymin, 1.0)
    scale = (size - 40) / span
    w = max(400, int((xmax - xmin) * scale) + 40)
    h = max(300, int((ymax - ymin) * scale) + 80)

    def to_px(x: float, y: float):
        return (int(20 + (x - xmin) * scale), int(20 + (ymax - y) * scale))

    img = Image.new("RGB", (w, h), (248, 246, 240))
    draw = ImageDraw.Draw(img)
    for ring in closed:
        pts = [to_px(x, y) for x, y in ring]
        draw.line(pts, fill=(200, 200, 200), width=1)

    tick_total = 0
    for ring, outline, ticks, tick_rgb, label in ring_styles:
        pts = [to_px(x, y) for x, y in ring]
        draw.line(pts, fill=outline, width=3)
        for x, y, tx, ty in ticks:
            p0 = to_px(x, y)
            dx, dy = tx - x, ty - y
            L = math.hypot(dx, dy) or 1.0
            p1 = to_px(x + dx / L * 7.5, y + dy / L * 7.5)
            draw.line([p0, p1], fill=tick_rgb, width=4)
            tick_total += 1
        if label:
            cx, cy = _ring_centroid(ring)
            draw.text(to_px(cx, cy), label, fill=outline)

    banner_h = 56 if subtitle else 36
    draw.rectangle([0, 0, w, banner_h], fill=(30, 30, 30))
    draw.text((10, 8), title, fill=(255, 255, 255))
    if subtitle:
        draw.text((10, 30), subtitle, fill=(255, 220, 120))
    return img, tick_total


def _save(img, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Force new mtime even if content similar
    img.save(dest, "PNG")
    now = time.time()
    try:
        import os

        os.utime(dest, (now, now))
    except OSError:
        pass
    print(f"Wrote {dest} ({img.size[0]}x{img.size[1]}) mtime-bump")


def _side_by_side(left, right, dest: Path, caption: str):
    from PIL import Image, ImageDraw

    gap = 12
    header = 40
    w = left.width + right.width + gap
    h = max(left.height, right.height) + header
    canvas = Image.new("RGB", (w, h), (40, 40, 40))
    draw = ImageDraw.Draw(canvas)
    draw.text((10, 10), caption, fill=(255, 255, 255))
    canvas.paste(left, (0, header))
    canvas.paste(right, (left.width + gap, header))
    _save(canvas, dest)


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
    candidates, deps, forms, skipped, thresholds = _classify_rings(
        closed, elev_at, interval_m
    )

    # Per-feature tick counts (AFTER rules)
    features = []
    after_styles = []
    for ring, relief in deps:
        ticks = place_closed_contour_ticks(ring, toward_centroid=True)
        features.append(
            {
                "role": "outer",
                "depth_m": round(relief.relief_m, 2),
                "perimeter_m": round(polyline_length_m(ring), 1),
                "tick_count": len(ticks),
            }
        )
        after_styles.append(
            (
                ring,
                (180, 40, 40),
                ticks,
                (220, 20, 20),
                f"101.1 {relief.relief_m:.1f}m n={len(ticks)}",
            )
        )
    # Inner candidates: outline gray, no ticks
    outer_ids = {id(r) for r, _ in deps}
    for ring, relief in candidates:
        if id(ring) in outer_ids:
            continue
        features.append(
            {
                "role": "inner_no_ticks",
                "depth_m": round(relief.relief_m, 2),
                "perimeter_m": round(polyline_length_m(ring), 1),
                "tick_count": 0,
            }
        )
        after_styles.append(
            (
                ring,
                (120, 120, 120),
                [],
                (120, 120, 120),
                f"inner NO ticks {relief.relief_m:.1f}m",
            )
        )

    after_tick_total = sum(f["tick_count"] for f in features)

    # BEFORE: dense ticks on ALL candidates (outer+inner)
    before_styles = []
    before_tick_total = 0
    for ring, relief in candidates:
        ticks = _dense_ticks_legacy(ring, spacing_m=45.0)
        before_tick_total += len(ticks)
        before_styles.append(
            (
                ring,
                (180, 40, 40),
                ticks,
                (220, 20, 20),
                f"OLD dense {relief.relief_m:.1f}m n={len(ticks)}",
            )
        )

    focus = [r for r, _ in candidates] or [r for r, _ in deps]
    after_img, n_after = _draw_scene(
        closed=closed,
        ring_styles=after_styles,
        title=f"PO / AFTER: {label}",
        subtitle=f"ticks TOTAL={after_tick_total} (max 1-2 opposite, OUTER only)",
        zoom_rings=focus,
    )
    before_img, n_before = _draw_scene(
        closed=closed,
        ring_styles=before_styles,
        title=f"PRED / BEFORE: {label}",
        subtitle=f"ticks TOTAL={before_tick_total} (dense along ALL nested rings)",
        zoom_rings=focus,
    )

    _save(before_img, out / f"{slug}-before-dense.png")
    _save(after_img, out / f"{slug}-101.1.png")
    _side_by_side(
        before_img,
        after_img,
        out / f"{slug}-before-after.png",
        f"{slug}: BEFORE dense={before_tick_total}  |  AFTER opposite-outer={after_tick_total}",
    )

    # Overview AFTER only
    ov_styles = list(after_styles)
    for ring, relief in forms:
        ov_styles.append(
            (
                ring,
                (40, 120, 60),
                [],
                (40, 120, 60),
                f"103 {relief.relief_m:.1f}m",
            )
        )
    ov_img, _ = _draw_scene(
        closed=closed,
        ring_styles=ov_styles,
        title=f"{label} overview AFTER",
        subtitle=f"101.1 ticks={after_tick_total}; inner rings without ticks",
        zoom_rings=None,
        pad=80.0,
        size=1400,
    )
    _save(ov_img, out / f"{slug}-overview.png")

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
        "tick_count_before_dense": before_tick_total,
        "tick_count_after": after_tick_total,
        "features": features,
        "depression_depths_m": [f["depth_m"] for f in features if f["role"] == "outer"],
        "formline_heights_m": sorted(
            [round(r.relief_m, 2) for _, r in forms], reverse=True
        )[:20],
    }
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--out", type=Path, default=MEDIA_DEMO)
    args = ap.parse_args()
    if not args.demo:
        ap.error("Use --demo")

    # Clear old PNGs so stale views cannot linger
    if args.out.is_dir():
        for p in args.out.glob("*.png"):
            p.unlink(missing_ok=True)

    summaries = [
        run_aoi(
            slug="roky",
            work=ROOT / "data" / "jobs" / "0822b22a8d62" / "work",
            interval_m=5.0,
            scale=10000,
            out=args.out,
            label="Roky forest 5m",
            refine=True,
        ),
        run_aoi(
            slug="test-vrstevnic",
            work=ROOT / "data" / "_demo_test_vrstevnic" / "work",
            interval_m=2.0,
            scale=4000,
            out=args.out,
            label="test vrstevnic sprint 2m",
            refine=False,
        ),
    ]
    args.out.mkdir(parents=True, exist_ok=True)
    payload = {
        "rules": (
            "101.1: 1-2 opposite ticks; nested pits -> outer contour only. "
            "103 mid-interval hi-conf. prefs 2026-10-05."
        ),
        "proof": "Compare *-before-dense.png vs *-101.1.png / *-before-after.png",
        "aois": summaries,
    }
    (args.out / "summary.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"Media -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
