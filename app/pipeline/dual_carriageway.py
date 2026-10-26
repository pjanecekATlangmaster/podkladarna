"""Les a MTBO: rozdělená vozovka (dva souběžné jednosměrné pásy téže silnice) jedním symbolem 502.0 po střední ose.

Dvě čáry 502.1 vedle sebe jsou čitelné jen s mezerou ``LINE_MIN_GAP_MM``; jsou-li osy pásů blíž než šířka kresby 502.1
+ mezera (1,31 mm mapy, 13 m v 1:10 000; K Barrandovu 11,3–11,7 m), kreslí se úsek jedním 502.0 (Petr 2026-10-26).
Pásy se párují podle názvu z ``osm_paths/oneway_roads.geojson`` (příprava OSM), jen protisměrné a souvisle aspoň
``DUAL_MIN_LEN_M``; kratší rozplety u křižovatek zůstávají 502.x. Osy se po délce spojí přes hranice OSM ways.
"""

from __future__ import annotations

import json
from pathlib import Path

# Nejkratší souvislý úsek rozdělené vozovky (m); kratší dvojice (rozplet u kruhového objezdu) se nekreslí 502.0.
DUAL_MIN_LEN_M = 100.0
# Mezera mezi úseky téže osy, která se překlene (přerušení u křižovatky uprostřed).
DUAL_GAP_M = 25.0
# Pásy blíž než tolik m nejsou dva pásy, ale tatáž osa (chyba dat).
DUAL_MIN_AXIS_M = 3.0
STEP_M = 2.0
DUAL_HIGHWAY = "road_dual"

_CACHE: dict[tuple, tuple] = {}


# Nejvyšší vzdálenost os pásů v mm mapy (kresba 502.1 0,96 mm + mezera čitelnosti 0,35 mm; Petr 2026-10-26 potvrdil 13 m v 1:10 000).
DUAL_MAX_AXIS_MM = 1.31


def max_axis_m(preset_id: str, scale: int) -> float:
    """Nejvyšší vzdálenost os pásů (m), při které se dvě 502.1 ještě nedají nakreslit vedle sebe."""
    from app.pipeline.osm_paths import mm_to_ground_m

    return mm_to_ground_m(DUAL_MAX_AXIS_MM, scale)


def _load(work_dir: Path) -> list[dict]:
    from app.pipeline.osm_paths import ONEWAY_ROADS_NAME

    path = work_dir / "osm_paths" / ONEWAY_ROADS_NAME
    if not path.is_file():
        return []
    try:
        return [f for f in json.loads(path.read_text(encoding="utf-8")).get("features") or [] if not (f.get("properties") or {}).get("roundabout")]
    except (OSError, ValueError):
        return []


def _runs(a, b, max_m: float):
    """Úseky čáry ``a`` ``(od, do)`` v m, kde protisměrná ``b`` vede do ``max_m`` (a dál než ``DUAL_MIN_AXIS_M``)."""
    n = max(2, int(a.length // STEP_M) + 1)
    ok = []
    for k in range(n):
        d = a.length * k / (n - 1)
        p = a.interpolate(d)
        t = b.project(p)
        q = b.interpolate(t)
        dist = p.distance(q)
        inner = 0.5 < t < b.length - 0.5
        ok.append((d, inner and DUAL_MIN_AXIS_M <= dist <= max_m))
    runs = []
    start = None
    for d, good in ok:
        if good and start is None:
            start = d
        elif not good and start is not None:
            runs.append((start, d))
            start = None
    if start is not None:
        runs.append((start, a.length))
    return runs


def _opposite(a, b) -> bool:
    """Protisměrné čáry: směr ``a`` proti směru nejbližší části ``b``."""
    from shapely.geometry import Point

    pa0, pa1 = Point(a.coords[0]), Point(a.coords[-1])
    t0, t1 = b.project(pa0), b.project(pa1)
    return t0 > t1


def dual_axes(work_dir: Path, preset_id: str, scale: int):
    """``(osy [[(x, y), …]], zóna shapely nebo None)``: střední osy rozdělených vozovek a pás, ve kterém se původní pásy nekreslí."""
    if preset_id.startswith("sprint"):
        return [], None
    from app.pipeline.osm_paths import ONEWAY_ROADS_NAME

    path = work_dir / "osm_paths" / ONEWAY_ROADS_NAME
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return [], None
    key = (str(path), stamp, preset_id, scale)
    if key in _CACHE:
        return _CACHE[key]
    if len(_CACHE) > 16:
        _CACHE.clear()
    out = _compute(_load(work_dir), max_axis_m(preset_id, scale), zone_half_m(preset_id, scale))
    _CACHE[key] = out
    return out


def zone_half_m(preset_id: str, scale: int) -> float | None:
    """Půlka pásu kolem osy 502.0, ve kterém se původní pásy a konce bočních ulic nekreslí: půlka výplně symbolu
    502.0 (boční ulice tak končí na okrajové lince 502.0, Petr 2026-10-26); bez symbolu None (= podle vzdálenosti os)."""
    from app.pipeline.oom_symbol_map import symbol_fill_width_mm
    from app.pipeline.osm_paths import mm_to_ground_m

    mm = symbol_fill_width_mm(preset_id, scale, "502.0")
    return mm_to_ground_m(mm, scale) / 2.0 if mm else None


def _compute(feats: list[dict], max_m: float, zone_half: float | None = None):
    from shapely.geometry import LineString, MultiLineString
    from shapely.ops import linemerge, substring, unary_union

    from app.pipeline.osm_paths import join_line_pieces

    by_name: dict[str, list] = {}
    for f in feats:
        p = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        name = str(p.get("name") or "")
        if name and len(coords) >= 2:
            ln = LineString([(float(x), float(y)) for x, y in coords])
            if ln.length > 0:
                by_name.setdefault(name, []).append(ln)
    axes = []
    for name, lines in by_name.items():
        if len(lines) < 2:
            continue
        for i, a in enumerate(lines):
            for b in lines[i + 1:]:
                if a.distance(b) > max_m or not _opposite(a, b):
                    continue
                for d0, d1 in _runs(a, b, max_m):
                    if d1 - d0 < STEP_M:
                        continue
                    part = substring(a, d0, d1)
                    pts = []
                    n = max(2, int(part.length // STEP_M) + 1)
                    for k in range(n):
                        p = part.interpolate(part.length * k / (n - 1))
                        q = b.interpolate(b.project(p))
                        pts.append(((p.x + q.x) / 2.0, (p.y + q.y) / 2.0))
                    axes.append(pts)
    if not axes:
        return [], None
    # Osy z navazujících OSM ways se překrývají nebo na sebe navazují: sjednotit, spojit v uzlech, překlenout mezery.
    union = unary_union([LineString(c) for c in axes])
    merged = linemerge(union) if union.geom_type == "MultiLineString" else union
    pieces = [[(float(x), float(y)) for x, y in g.coords] for g in getattr(merged, "geoms", [merged]) if g.geom_type == "LineString"]
    pieces = _bridge_gaps(join_line_pieces(pieces), DUAL_GAP_M)
    kept = [LineString(c) for c in pieces if len(c) >= 2 and LineString(c).length >= DUAL_MIN_LEN_M]
    if not kept:
        return [], None
    r = max(max_m / 2.0 + 1.0, zone_half or 0.0)  # oba pásy leží do max_m/2 od osy; boční ulice končí na okraji výplně 502.0
    zone = unary_union([ln.buffer(r, cap_style=2) for ln in kept])
    return [[(float(x), float(y)) for x, y in ln.coords] for ln in kept], zone


def _bridge_gaps(pieces: list[list[tuple[float, float]]], gap_m: float) -> list[list[tuple[float, float]]]:
    """Kusy osy, jejichž konce jsou do ``gap_m`` od sebe a míří na sebe (do 45°), se spojí rovnou spojkou."""
    import math

    from shapely.geometry import Point

    lines = [list(c) for c in pieces if len(c) >= 2]
    changed = True
    while changed and len(lines) > 1:
        changed = False
        best = None
        for i, a in enumerate(lines):
            for j, b in enumerate(lines):
                if j <= i:
                    continue
                for ea, eb in ((-1, 0), (0, -1), (-1, -1), (0, 0)):
                    pa, pb = a[ea], b[eb]
                    d = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
                    if d > gap_m or d < 1e-6:
                        continue
                    # směr od konce ven musí mířit k druhému konci
                    ia = a[-2] if ea == -1 else a[1]
                    va = (pa[0] - ia[0], pa[1] - ia[1])
                    vb = (pb[0] - pa[0], pb[1] - pa[1])
                    na, nb = math.hypot(*va), math.hypot(*vb)
                    if na < 1e-6 or nb < 1e-6 or (va[0] * vb[0] + va[1] * vb[1]) / (na * nb) < math.cos(math.radians(45)):
                        continue
                    if best is None or d < best[0]:
                        best = (d, i, j, ea, eb)
        if best is not None:
            _d, i, j, ea, eb = best
            a, b = lines[i], lines[j]
            a = a if ea == -1 else a[::-1]  # a končí u mezery
            b = b if eb == 0 else b[::-1]  # b u mezery začíná
            lines[i] = a + b
            del lines[j]
            changed = True
    return lines
