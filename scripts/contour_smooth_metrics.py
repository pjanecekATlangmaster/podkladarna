"""Metriky vrstevnic „jako dosud“ × „adaptivní“ nad hotovým work/ jobu.

Z ``contours/dem_smooth.tif`` spočítá obě varianty (gdal_contour + Chaikin + DP
jako při sestavení mapy) a porovná je po třídách váhy w (0 / 0–0,5 / > 0,5):
délka, počet uzlů po DP, zubatost (součet |změn směru| na km), odchylka od
původní (průměr a 95. percentil vzdálenosti bodů nové k původní téže výšky),
uzavřené prstence a průsečíky čar různých výšek.

    python scripts/contour_smooth_metrics.py <work_dir> --scale 10000 --interval 5 [--out metrics.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _lines(shp: Path, *, scale: int, interval: float, sf: float):
    from app.pipeline.contours_gdal import (
        _elev_from_props,
        _iter_contour_rows,
        chaikin,
        contour_dem_params,
        contour_line_params,
        contour_simplify_tol_m,
        refine_contour_polylines,
        simplify_contour_polyline,
    )
    from app.pipeline.oom_import import _wkb_parts

    _c, _w, iters = contour_dem_params(sf, interval)
    min_len, gap = contour_line_params(sf, interval)
    tol = contour_simplify_tol_m(scale)
    by_elev: dict[float, list] = {}
    for props, wkb in _iter_contour_rows(shp):
        elev = _elev_from_props(props)
        parts, _ = _wkb_parts(wkb)
        for part in parts:
            if part[0] == "line" and len(part[1]) >= 2:
                by_elev.setdefault(elev, []).append(list(part[1]))
    out = []
    for elev, lines in by_elev.items():
        for pts in refine_contour_polylines(lines, min_length_m=min_len, stitch_gap_m=gap):
            pts = simplify_contour_polyline(chaikin(pts, iterations=iters), tol)
            if len(pts) >= 2:
                out.append((elev, pts))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work_dir", type=Path)
    ap.add_argument("--scale", type=int, default=10000)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    import numpy as np
    from osgeo import gdal
    from shapely.geometry import LineString
    from shapely.strtree import STRtree

    from app.pipeline.contours_adaptive import (
        adaptive_contour_array,
        adaptive_params,
        write_adaptive_contour_dem,
    )
    from app.pipeline.contours_gdal import _run_gdal_contour

    sf = args.scale / 10000.0
    cont = args.work_dir / "contours"
    base_dem = cont / "dem_smooth.tif"
    tmp = Path(tempfile.mkdtemp(prefix="cmetr_"))
    adapt_dem = tmp / "dem_contour.tif"
    write_adaptive_contour_dem(base_dem, adapt_dem, interval_m=args.interval, scale=args.scale, scalefactor=sf, log=print)
    shp0 = _run_gdal_contour(base_dem, tmp / "c0.shp", interval_m=args.interval)
    shp1 = _run_gdal_contour(adapt_dem, tmp / "c1.shp", interval_m=args.interval)

    ds = gdal.Open(str(base_dem))
    gt = ds.GetGeoTransform()
    band = ds.GetRasterBand(1)
    z = band.ReadAsArray().astype(np.float32)
    nd = band.GetNoDataValue()
    valid = np.isfinite(z) & ((z != np.float32(nd)) if nd is not None else True)
    _out, w = adaptive_contour_array(
        z, valid, cell_m=abs(gt[1]), interval_m=args.interval, scale=args.scale, params=adaptive_params(sf)
    )

    def w_at(x, y):
        c = int((x - gt[0]) / gt[1])
        r = int((y - gt[3]) / gt[5])
        if 0 <= r < w.shape[0] and 0 <= c < w.shape[1]:
            return float(w[r, c])
        return 0.0

    def cls(v):
        return "w0" if v <= 0 else ("w_lo" if v <= 0.5 else "w_hi")

    lines0 = _lines(shp0, scale=args.scale, interval=args.interval, sf=sf)
    lines1 = _lines(shp1, scale=args.scale, interval=args.interval, sf=sf)
    by_elev0: dict[float, list] = {}
    for e, pts in lines0:
        by_elev0.setdefault(e, []).append(LineString(pts))

    def stats(lines, ref=None):
        acc = {k: {"len_m": 0.0, "vertices": 0, "turn_deg": 0.0, "dev": []} for k in ("w0", "w_lo", "w_hi")}
        closed = 0
        for e, pts in lines:
            if pts[0] == pts[-1]:
                closed += 1
            refs = ref.get(e, []) if ref is not None else None
            for i in range(1, len(pts)):
                (x0, y0), (x1, y1) = pts[i - 1], pts[i]
                k = cls(w_at((x0 + x1) / 2, (y0 + y1) / 2))
                acc[k]["len_m"] += math.hypot(x1 - x0, y1 - y0)
                acc[k]["vertices"] += 1
                if 1 < i:
                    xa, ya = pts[i - 2]
                    a1 = math.atan2(y0 - ya, x0 - xa)
                    a2 = math.atan2(y1 - y0, x1 - x0)
                    d = abs((a2 - a1 + math.pi) % (2 * math.pi) - math.pi)
                    acc[k]["turn_deg"] += math.degrees(d)
                if refs:
                    from shapely.geometry import Point

                    p = Point(x1, y1)
                    acc[k]["dev"].append(min(r.distance(p) for r in refs))
        res = {"closed_rings": closed, "objects": len(lines)}
        for k, a in acc.items():
            km = a["len_m"] / 1000.0
            dev = sorted(a["dev"])
            res[k] = {
                "len_km": round(km, 3),
                "vertices": a["vertices"],
                "turn_deg_per_km": round(a["turn_deg"] / km, 1) if km else None,
            }
            if dev:
                res[k]["dev_mean_m"] = round(sum(dev) / len(dev), 2)
                res[k]["dev_p95_m"] = round(dev[int(0.95 * (len(dev) - 1))], 2)
        return res

    def crossings(lines):
        geoms = [LineString(p) for _e, p in lines]
        elevs = [e for e, _p in lines]
        tree = STRtree(geoms)
        n = 0
        for i, g in enumerate(geoms):
            for j in tree.query(g):
                j = int(j)
                if j > i and elevs[j] != elevs[i] and g.intersects(geoms[j]):
                    n += 1
        return n

    report = {
        "share_w_pos": round(float((w > 0).sum()) / max(int(valid.sum()), 1), 3),
        "jako_dosud": stats(lines0),
        "adaptivni": stats(lines1, by_elev0),
        "crossings_jako_dosud": crossings(lines0),
        "crossings_adaptivni": crossings(lines1),
    }
    txt = json.dumps(report, ensure_ascii=False, indent=2)
    print(txt)
    if args.out:
        args.out.write_text(txt + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
