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
# Pásy jsou protisměrné jen se směry do tolika stupňů od sebe (po otočení); jinak by navazující úseky jedné čáry
# nebo zatáčka k druhému pásu dávaly falešnou osu (Lipského / Lamačova u kruhového objezdu, D25).
OPPOSITE_MAX_DEG = 30.0
DUAL_HIGHWAY = "road_dual"
# Pás kolem ostrůvku kruhového objezdu (m), ve kterém zóna osy neořízne prstenec.
ROUNDABOUT_KEEP_M = 1.0
# Obousměrné pokračování silnice za koncem osy (pásy se slily kousek před křižovatkou) do této délky (m) kreslí také
# 502.0 – jinak by mezi osou a křižovatkou zůstal úsek užší 502.1 (Lamačova u K Barrandovu, Petr 2026-10-27).
CONT_MAX_M = 60.0
# Pokračování se bere jen ve směru osy (do tolika stupňů).
CONT_MAX_DEG = 30.0
# Okolí uzlu křižovatky na konci prodloužení (m), kde zóna osy nic neořezává (příčná silnice zůstane celá).
CONT_NODE_KEEP_M = 3.0

_CACHE: dict[tuple, tuple] = {}


# Nejvyšší vzdálenost os pásů v mm mapy (kresba 502.1 0,96 mm + mezera čitelnosti 0,35 mm; Petr 2026-10-26 potvrdil 13 m v 1:10 000).
DUAL_MAX_AXIS_MM = 1.31


def max_axis_m(preset_id: str, scale: int) -> float:
    """Nejvyšší vzdálenost os pásů (m), při které se dvě 502.1 ještě nedají nakreslit vedle sebe."""
    from app.pipeline.osm_paths import mm_to_ground_m

    return mm_to_ground_m(DUAL_MAX_AXIS_MM, scale)


def _load(work_dir: Path, *, roundabouts: bool = False) -> list[dict]:
    """Jednosměrné silnice z přípravy OSM; prstence kruhových objezdů jen s ``roundabouts``."""
    from app.pipeline.osm_paths import ONEWAY_ROADS_NAME

    path = work_dir / "osm_paths" / ONEWAY_ROADS_NAME
    if not path.is_file():
        return []
    try:
        feats = json.loads(path.read_text(encoding="utf-8")).get("features") or []
    except (OSError, ValueError):
        return []
    return [f for f in feats if roundabouts or not (f.get("properties") or {}).get("roundabout")]


def _road_lines(work_dir: Path) -> list[list[tuple[float, float]]]:
    """Silnice z přípravy OSM (``paths_osm``) jako kandidáti pokračování osy za sloučením pásů."""
    path = work_dir / "osm_paths" / "paths_osm.geojson"
    try:
        feats = json.loads(path.read_text(encoding="utf-8")).get("features") or []
    except (OSError, ValueError):
        return []
    out = []
    for f in feats:
        g = f.get("geometry") or {}
        if g.get("type") == "LineString" and str((f.get("properties") or {}).get("highway") or "").startswith("road_"):
            coords = [(float(x), float(y)) for x, y in g.get("coordinates") or []]
            if len(coords) >= 2:
                out.append(coords)
    return out


def roundabout_islands(feats: list[dict]):
    """Plochy uvnitř uzavřených prstenců kruhových objezdů (shapely) nebo None. Osa rozdělené vozovky přes ně nevede
    (Lamačova: mezera osy se překlenula středem ostrůvku, Petr 2026-10-27)."""
    from shapely.geometry import LineString
    from shapely.ops import polygonize, unary_union

    rings = []
    for f in feats:
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if (f.get("properties") or {}).get("roundabout") and len(coords) >= 2:
            rings.append(LineString([(float(x), float(y)) for x, y in coords]))
    if not rings:
        return None
    polys = list(polygonize(unary_union(rings)))
    return unary_union(polys) if polys else None


def _tangent(line, d: float):
    """Jednotkový směr čáry v délce ``d`` (m)."""
    import math

    p0 = line.interpolate(max(0.0, d - 1.0))
    p1 = line.interpolate(min(line.length, d + 1.0))
    vx, vy = p1.x - p0.x, p1.y - p0.y
    n = math.hypot(vx, vy)
    return (vx / n, vy / n) if n > 1e-9 else (0.0, 0.0)


def _runs(a, b, max_m: float):
    """Úseky čáry ``a`` ``(od, do)`` v m, kde protisměrná ``b`` vede do ``max_m`` (a dál než ``DUAL_MIN_AXIS_M``)
    souběžně (směry proti sobě do ``OPPOSITE_MAX_DEG``; navazující kusy téže čáry nebo zatáčka k pásu nejsou dvojice)."""
    import math

    cos_lim = -math.cos(math.radians(OPPOSITE_MAX_DEG))
    n = max(2, int(a.length // STEP_M) + 1)
    ok = []
    for k in range(n):
        d = a.length * k / (n - 1)
        p = a.interpolate(d)
        t = b.project(p)
        q = b.interpolate(t)
        dist = p.distance(q)
        inner = 0.5 < t < b.length - 0.5
        ta, tb = _tangent(a, d), _tangent(b, t)
        opposite = ta[0] * tb[0] + ta[1] * tb[1] <= cos_lim
        ok.append((d, inner and opposite and DUAL_MIN_AXIS_M <= dist <= max_m))
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
    try:
        roads_stamp = (work_dir / "osm_paths" / "paths_osm.geojson").stat().st_mtime_ns
    except OSError:
        roads_stamp = None
    key = (str(path), stamp, roads_stamp, preset_id, scale)
    if key in _CACHE:
        return _CACHE[key]
    if len(_CACHE) > 16:
        _CACHE.clear()
    feats = _load(work_dir, roundabouts=True)
    out = _compute(
        [f for f in feats if not (f.get("properties") or {}).get("roundabout")],
        max_axis_m(preset_id, scale),
        zone_half_m(preset_id, scale),
        roundabout_islands(feats),
        _road_lines(work_dir),
    )
    _CACHE[key] = out
    return out


def zone_half_m(preset_id: str, scale: int) -> float | None:
    """Půlka pásu kolem osy 502.0, ve kterém se původní pásy a konce bočních ulic nekreslí: půlka výplně symbolu
    502.0 (boční ulice tak končí na okrajové lince 502.0, Petr 2026-10-26); bez symbolu None (= podle vzdálenosti os)."""
    from app.pipeline.oom_symbol_map import symbol_fill_width_mm
    from app.pipeline.osm_paths import mm_to_ground_m

    mm = symbol_fill_width_mm(preset_id, scale, "502.0")
    return mm_to_ground_m(mm, scale) / 2.0 if mm else None


def _compute(feats: list[dict], max_m: float, zone_half: float | None = None, islands=None, roads=None):
    from shapely.geometry import LineString, MultiLineString
    from shapely.ops import linemerge, substring, unary_union

    from app.pipeline.osm_paths import join_line_pieces

    by_name: dict[str, list] = {}
    lane_lines: list[list[tuple[float, float]]] = []
    for f in feats:
        p = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        name = str(p.get("name") or "")
        if name and len(coords) >= 2:
            ln = LineString([(float(x), float(y)) for x, y in coords])
            if ln.length > 0:
                by_name.setdefault(name, []).append(ln)
        if len(coords) >= 2:
            lane_lines.append([(float(x), float(y)) for x, y in coords])
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
    if islands is not None:
        union = union.difference(islands)  # osa končí na prstenci kruhového objezdu
        if union.is_empty:
            return [], None
    merged = linemerge(union) if union.geom_type == "MultiLineString" else union
    pieces = [[(float(x), float(y)) for x, y in g.coords] for g in getattr(merged, "geoms", [merged]) if g.geom_type == "LineString"]
    pieces = _bridge_gaps(join_line_pieces(pieces), DUAL_GAP_M, islands)
    kept = [LineString(c) for c in pieces if len(c) >= 2 and LineString(c).length >= DUAL_MIN_LEN_M]
    if not kept:
        return [], None
    r = max(max_m / 2.0 + 1.0, zone_half or 0.0)  # oba pásy leží do max_m/2 od osy; boční ulice končí na okraji výplně 502.0
    zones = [ln.buffer(r, cap_style=2) for ln in kept]
    if roads:
        oneway = unary_union([LineString(c) for c in lane_lines]).buffer(1.0)
        extended = []
        for ln in kept:
            ln, exts = _extend_to_continuation(ln, roads, oneway, max_m / 2.0 + 3.0, islands)
            extended.append(ln)
            zones += [_continuation_zone(ext, r) for ext in exts]
        kept = extended
    zone = unary_union(zones)
    if islands is not None:
        zone = zone.difference(islands.buffer(ROUNDABOUT_KEEP_M))  # prstenec objezdu se neořízne
    return [[(float(x), float(y)) for x, y in ln.coords] for ln in kept], zone


def _continuation_zone(ext, r: float):
    """Zóna prodloužení osy: v plné šířce ``r`` jen po bod ``r`` + 1 m před uzlem křižovatky, dál úzký pás 1 m kolem
    pokračování bez okolí uzlu – příčná silnice v křižovatce se tak neořízne (review PR 134)."""
    from shapely.geometry import Point
    from shapely.ops import substring

    wide_len = ext.length - r - 1.0
    parts = [ext.buffer(1.0, cap_style=2).difference(Point(ext.coords[-1]).buffer(CONT_NODE_KEEP_M))]
    if wide_len > 0.5:
        parts.append(substring(ext, 0.0, wide_len).buffer(r, cap_style=2))
    from shapely.ops import unary_union

    return unary_union(parts)


def _extend_to_continuation(axis, roads, oneway, reach_m: float, islands=None):
    """``(osa, [prodloužení])``: osa prodloužená na obou koncích o obousměrnou silnici, která ve směru osy navazuje na
    místo sloučení pásů (začátek do ``reach_m`` od konce osy, odbočení do ``CONT_MAX_DEG``, celkem do ``CONT_MAX_M``).
    Pokračování jde přes navazující kusy čáry až ke křižovatce (uzel s více silnicemi); jednosměrné pásy (z většiny
    v ``oneway``) pokračováním nejsou – osa nepůjde po jednom z rozbíhajících se pásů. Prodloužení vede od konce osy k uzlu."""
    import math

    from shapely.geometry import LineString, Point

    road_lines = [LineString(c) for c in roads]

    def _dir(seq):
        q = LineString(seq).interpolate(min(5.0, LineString(seq).length))
        return (q.x - seq[0][0], q.y - seq[0][1])

    def _cos(a, b):
        na, nb = math.hypot(*a), math.hypot(*b)
        return (a[0] * b[0] + a[1] * b[1]) / (na * nb) if na > 1e-6 and nb > 1e-6 else -1.0

    coords = [(float(x), float(y)) for x, y in axis.coords]
    cos_lim = math.cos(math.radians(CONT_MAX_DEG))
    exts = []
    for at_end in (False, True):
        e = coords[-1] if at_end else coords[0]
        pe0 = Point(e)
        near = [k for k, ln in enumerate(road_lines) if ln.distance(pe0) <= CONT_MAX_M + reach_m]
        cand = [
            k
            for k in near
            if road_lines[k].length <= CONT_MAX_M and road_lines[k].difference(oneway).length >= 0.5 * road_lines[k].length
        ]

        def _starting(p, tol):
            return [
                (math.hypot(seq[0][0] - p[0], seq[0][1] - p[1]), k, list(seq))
                for k in cand
                for seq in (roads[k], roads[k][::-1])
                if math.hypot(seq[0][0] - p[0], seq[0][1] - p[1]) <= tol
            ]

        inner = axis.interpolate(axis.length - 5.0 if at_end else 5.0)
        v = (e[0] - inner.x, e[1] - inner.y)
        ext: list[tuple[float, float]] = []
        used: set[int] = set()
        opts = [o for o in _starting(e, reach_m) if _cos(v, _dir(o[2])) >= cos_lim]
        at_junction = False
        while opts:
            _d, k, seq = min(opts, key=lambda o: o[0])
            ext_new = ext + seq
            if LineString([e, *ext_new]).length > CONT_MAX_M + reach_m:
                break
            if islands is not None and LineString([e, *ext_new]).intersection(islands).length > 0.5:
                break
            ext = ext_new
            used.add(k)
            end = seq[-1]
            # dál jen přes prostý uzel (navazující kus téže čáry); na křižovatce pokračování končí
            pe = Point(end)
            touching = [j for j in near if j != k and road_lines[j].distance(pe) <= 0.5]
            ends_here = [j for j in touching if Point(roads[j][0]).distance(pe) <= 0.5 or Point(roads[j][-1]).distance(pe) <= 0.5]
            simple = len(touching) == 1 and len(ends_here) == 1  # jen navazující kus téže silnice
            nxt = [o for o in _starting(end, 0.5) if o[1] not in used]
            seg = (end[0] - seq[-2][0], end[1] - seq[-2][1])
            if not simple or len(nxt) != 1 or _cos(seg, _dir(nxt[0][2])) < cos_lim:
                at_junction = not simple and bool(touching)
                break
            opts = nxt
        if at_junction and ext:
            exts.append(LineString([e, *ext]))
            coords = coords + ext if at_end else ext[::-1] + coords
    return LineString(coords), exts


def _bridge_gaps(pieces: list[list[tuple[float, float]]], gap_m: float, islands=None) -> list[list[tuple[float, float]]]:
    """Kusy osy, jejichž konce jsou do ``gap_m`` od sebe a míří na sebe (do 45°), se spojí rovnou spojkou;
    ne přes ostrůvek kruhového objezdu (``islands``)."""
    import math

    from shapely.geometry import LineString

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
                    if islands is not None and LineString([pa, pb]).intersection(islands).length > 0.5:
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


# Rozvětvení napojení: dvě krátká jednosměrná ramena téže ulice ze společného vrcholu k jiné silnici (Lipského u
# zastávky na Lamačově, Petr 2026-10-27: „v lese a MTBO jednou linií“). Ramena do této délky (m).
FORK_ARM_MAX_M = 60.0
# Ramena svírají ve vrcholu nejvýš tolik stupňů.
FORK_MAX_DEG = 60.0
# Okolí vrcholu a konců ramen (m), kde zóna ramen neořezává jiné silnice.
FORK_NODE_KEEP_M = 3.0
# Vlastnost čáry, která ramena nahrazuje (zóna ramen ji neořízne).
FORK_LINK_PROP = "fork_link"
_FORK_CACHE: dict[tuple, tuple] = {}


def fork_links(work_dir: Path, preset_id: str, scale: int):
    """``([(čára [(x, y), …], highway)], zóna nebo None)``: krátká rozvětvení ze dvou jednosměrných ramen nahrazená
    jednou čarou po středu ramen; v zóně se původní ramena nekreslí."""
    if preset_id.startswith("sprint"):
        return [], None
    from app.pipeline.osm_paths import ONEWAY_ROADS_NAME

    path = work_dir / "osm_paths" / ONEWAY_ROADS_NAME
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return [], None
    key = (str(path), stamp, preset_id, scale)
    if key not in _FORK_CACHE:
        if len(_FORK_CACHE) > 16:
            _FORK_CACHE.clear()
        _FORK_CACHE[key] = _forks(_load(work_dir), max_axis_m(preset_id, scale), dual_axes(work_dir, preset_id, scale)[1])
    return _FORK_CACHE[key]


def _forks(feats: list[dict], max_m: float, dual_zone=None):
    import math

    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union

    by_name: dict[str, list] = {}
    for f in feats:
        p = f.get("properties") or {}
        coords = [(float(x), float(y)) for x, y in (f.get("geometry") or {}).get("coordinates") or []]
        name = str(p.get("name") or "")
        if name and len(coords) >= 2 and LineString(coords).length <= FORK_ARM_MAX_M:
            by_name.setdefault(name, []).append((coords, str(p.get("highway") or "")))

    def _close(p, q, tol=0.5):
        return math.hypot(p[0] - q[0], p[1] - q[1]) <= tol

    links, zones, used = [], [], set()
    for arms in by_name.values():
        for i, (a, hw_a) in enumerate(arms):
            for j, (b, hw_b) in enumerate(arms):
                if i == j or i in used or j in used or not _close(a[-1], b[0]):
                    continue
                # a vede do vrcholu, b z něj: ramena ve vrcholu spojená protisměrně
                apex, fa, fb = b[0], a[0], b[-1]
                if math.hypot(fa[0] - fb[0], fa[1] - fb[1]) > max_m or _close(fa, fb, 1.0):
                    continue
                va, vb = (fa[0] - apex[0], fa[1] - apex[1]), (fb[0] - apex[0], fb[1] - apex[1])
                na, nb = math.hypot(*va), math.hypot(*vb)
                if na < 1.0 or nb < 1.0 or (va[0] * vb[0] + va[1] * vb[1]) / (na * nb) < math.cos(math.radians(FORK_MAX_DEG)):
                    continue
                la, lb = LineString(a[::-1]), LineString(b)  # obě od vrcholu
                if dual_zone is not None and (la.intersection(dual_zone).length + lb.intersection(dual_zone).length) > 0.5 * (la.length + lb.length):
                    continue  # pásy u konce rozdělené vozovky kreslí osa 502.0
                n = max(2, int(max(la.length, lb.length) // 2.0) + 1)
                mid = []
                for k in range(n):
                    t = k / (n - 1)
                    p, q = la.interpolate(t, normalized=True), lb.interpolate(t, normalized=True)
                    mid.append(((p.x + q.x) / 2.0, (p.y + q.y) / 2.0))
                links.append((mid, hw_a))
                keep = unary_union([Point(c).buffer(FORK_NODE_KEEP_M) for c in (apex, fa, fb)])
                zones.append(unary_union([la, lb]).buffer(1.0, cap_style=2).difference(keep))
                used.update((i, j))
    return links, (unary_union(zones) if zones else None)
