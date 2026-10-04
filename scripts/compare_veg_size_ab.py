#!/usr/bin/env python3
"""A/B filtr vegetace: legacy soft-only vs default (tvar) vs strict.

Vstup: ``.omap`` (forest) nebo ``vegetation.shp``. Spočítá plochy/počty
401/406/408/410 před (legacy soft) a po default/strict tvarovém filtru.

Příklad::

  python scripts/compare_veg_size_ab.py \\
    --omap path/to/Roky-les.omap --json out.json
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shapely.geometry import Polygon

from app.pipeline.veg_size_filter import (
    keep_veg_polygon,
    mrr_width_length,
    soft_min_for_code,
)

_VEG = ("401", "406", "408", "410")


def _ring_area(coords: list[tuple[float, float]]) -> float:
    if len(coords) < 3:
        return 0.0
    a = 0.0
    for i in range(len(coords) - 1):
        x1, y1 = coords[i]
        x2, y2 = coords[i + 1]
        a += x1 * y2 - x2 * y1
    return abs(a) * 0.5


def _parse_coords(raw: str) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for token in raw.split(";"):
        token = token.strip()
        if not token:
            continue
        parts = token.split()
        if len(parts) < 2:
            continue
        # OOM může mít flags na konci ("18")
        try:
            x = float(parts[0])
            y = float(parts[1])
        except ValueError:
            continue
        pts.append((x, y))
    return pts


def load_veg_from_omap(path: Path) -> list[tuple[str, Polygon]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(
        r'<georeferencing[^>]*\bscale="(\d+)"',
        text,
    )
    scale = int(m.group(1)) if m else 10000
    # map units → metres: 1 m = scale/1000 map units? 
    # projected_to_map: fac = 1000 / (scale/1000) = 1e6/scale
    # scale 10000 → fac=100 → 1 m = 100 mu → mu_to_m = scale/1e6? No: /100 = scale/1e6
    mu_to_m = float(scale) / 1_000_000.0
    # Wait: fac = 1000/s, s=scale/1000, fac=1e6/scale. 1m → fac map units.
    # map_to_m = 1/fac = scale/1e6. Yes for 10000: 0.01 m per map unit. 100 mu = 1 m. OK.

    parts_idx = text.find('<parts count=')
    if parts_idx < 0:
        raise ValueError("omap nemá <parts>")
    sym_text = text[:parts_idx]
    id_to_code: dict[str, str] = {}
    for mm in re.finditer(
        r'<symbol[^>]*\bid="(\d+)"[^>]*\bcode="([^"]+)"', sym_text
    ):
        id_to_code[mm.group(1)] = mm.group(2)
    for mm in re.finditer(
        r'<symbol[^>]*\bcode="([^"]+)"[^>]*\bid="(\d+)"', sym_text
    ):
        id_to_code[mm.group(2)] = mm.group(1)

    part_text = text[parts_idx:]
    out: list[tuple[str, Polygon]] = []
    for mm in re.finditer(
        r'<object type="1" symbol="(\d+)">\s*<coords[^>]*>([^<]+)</coords>',
        part_text,
    ):
        code = id_to_code.get(mm.group(1))
        if code not in _VEG:
            continue
        pts_mu = _parse_coords(mm.group(2))
        if len(pts_mu) < 3:
            continue
        pts = [(x * mu_to_m, y * mu_to_m) for x, y in pts_mu]
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        try:
            poly = Polygon(pts)
            if poly.is_empty or not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or float(poly.area) <= 0:
                continue
            if poly.geom_type == "MultiPolygon":
                poly = max(poly.geoms, key=lambda g: float(g.area))
            out.append((code, poly))
        except Exception:
            continue
    return out


def load_veg_from_shp(path: Path) -> list[tuple[str, Polygon]]:
    from shapely import from_wkb
    from app.pipeline.oom_import import _pyogrio_layer_rows

    out: list[tuple[str, Polygon]] = []
    for props, wkb in _pyogrio_layer_rows(path):
        code = str(props.get("code") or "")
        if code not in _VEG or not wkb:
            continue
        geom = from_wkb(bytes(wkb))
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "MultiPolygon":
            for g in geom.geoms:
                if not g.is_empty:
                    out.append((code, g))
        elif geom.geom_type == "Polygon":
            out.append((code, geom))
    return out


def legacy_keep(code: str, geom: Polygon) -> bool:
    return float(geom.area) >= soft_min_for_code(code, "default")


def summarize(
    features: list[tuple[str, Polygon]],
    *,
    mode: str,
) -> dict[str, Any]:
    by_code: dict[str, dict[str, Any]] = {
        c: {"count": 0, "area_m2": 0.0, "bands_kept": 0, "dropped": 0}
        for c in _VEG
    }
    kept_feats: list[tuple[str, Polygon]] = []
    for code, geom in features:
        area = float(geom.area)
        if mode == "legacy":
            ok = legacy_keep(code, geom)
            reason = None if ok else "soft"
        else:
            ok, reason = keep_veg_polygon(code, geom, profile=mode)
        bucket = by_code[code]
        if not ok:
            bucket["dropped"] += 1
            continue
        bucket["count"] += 1
        bucket["area_m2"] += area
        kept_feats.append((code, geom))
        # pás = aspect ≥ 4 a šířka < 4 m (úzký)
        w, length = mrr_width_length(geom)
        aspect = length / max(w, 1e-9)
        if w <= 4.0 and length >= 20.0 and aspect >= 4.0:
            bucket["bands_kept"] += 1
    for c in _VEG:
        by_code[c]["area_m2"] = round(by_code[c]["area_m2"], 1)
    return {
        "mode": mode,
        "total_count": sum(by_code[c]["count"] for c in _VEG),
        "total_area_m2": round(sum(by_code[c]["area_m2"] for c in _VEG), 1),
        "by_code": by_code,
        "narrow_bands_kept": sum(by_code[c]["bands_kept"] for c in _VEG),
    }


def synthetic_examples() -> list[dict[str, Any]]:
    """Příklady z návrhu — ověření pásů."""
    from shapely.geometry import box

    cases = [
        ("3x3 kompakt", "401", box(0, 0, 3, 3)),
        ("3x3 kompakt", "408", box(0, 0, 3, 3)),
        ("2x12 pás", "401", box(0, 0, 2, 12)),
        ("2x12 pás", "408", box(0, 0, 2, 12)),
        ("2x30 pás", "401", box(0, 0, 2, 30)),
        ("2x30 pás", "408", box(0, 0, 2, 30)),
        ("4x6 ovál", "401", box(0, 0, 4, 6)),
        ("4x6 ovál", "408", box(0, 0, 4, 6)),
        ("5x5", "401", box(0, 0, 5, 5)),
        ("5x5", "408", box(0, 0, 5, 5)),
        ("1.5x8 krátký", "401", box(0, 0, 1.5, 8)),
        ("0.5x22 úzký pás", "401", box(0, 0, 0.5, 22)),
    ]
    rows = []
    for name, code, geom in cases:
        w, length = mrr_width_length(geom)
        rows.append(
            {
                "name": name,
                "code": code,
                "area_m2": round(float(geom.area), 2),
                "length_m": round(length, 2),
                "aspect": round(length / max(w, 1e-9), 2),
                "legacy": legacy_keep(code, geom),
                "default": keep_veg_polygon(code, geom, profile="default")[0],
                "strict": keep_veg_polygon(code, geom, profile="strict")[0],
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--omap", type=Path, help="Forest .omap se vegetací")
    ap.add_argument("--shp", type=Path, help="vegetation.shp (cls/code)")
    ap.add_argument("--json", type=Path, help="Zápis reportu JSON")
    ap.add_argument("--label", default="", help="Popisek vzorku")
    args = ap.parse_args(argv)

    features: list[tuple[str, Polygon]] = []
    source = ""
    if args.shp and args.shp.is_file():
        features = load_veg_from_shp(args.shp)
        source = str(args.shp)
    elif args.omap and args.omap.is_file():
        features = load_veg_from_omap(args.omap)
        source = str(args.omap)
    else:
        print("Chybí --omap nebo --shp", file=sys.stderr)
        return 2

    report = {
        "label": args.label or Path(source).stem,
        "source": source,
        "input_count": len(features),
        "input_area_m2": round(sum(float(g.area) for _, g in features), 1),
        "legacy": summarize(features, mode="legacy"),
        "default": summarize(features, mode="default"),
        "strict": summarize(features, mode="strict"),
        "synthetic_examples": synthetic_examples(),
        "note": (
            "Vstup z .omap už prošel soft_min (12/25) v pipeline — "
            "default ≈ legacy na tomto vstupu; strict maže víc. "
            "Pásová výjimka defaultu se projeví až na pre-filter polygonech."
        ),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
