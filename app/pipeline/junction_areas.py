"""Les a MTBO: plocha silnic z DTM jen u složitých křižovatek; jinde se silnice kreslí čarou podle šířky.

Kritérium (Petr 2026-10-26, ověřeno na Barrandově): shluk uzlů OSM silnic (stupeň ≥ 3, do ``CLUSTER_M`` m) je složitý,
má-li v ploše silnic z DTM uvnitř shluku ostrůvek (díra), je-li kruhovým objezdem, nebo vede-li jím aspoň
``MIN_ONEWAY`` jednosměrných pásů. Jednoduchá křižovatka (T, X, i o šesti ramenech) se kreslí čarami 502.x a o napojení
se postará OOMapper. Poměr plochy k čarám ani počet ramen složitost nerozlišují.
"""

from __future__ import annotations

import json
from pathlib import Path

JUNCTION_AREAS = True
# Petr 2026-10-28: les a MTBO kreslí kruhové objezdy jen čarou – přechod plochy objezdu na čáru 502.x dělal zuby
# a kousky jiné kresby (Lamačova, 2.45.0). Plocha složité křižovatky ale nese detail z DTM (Lamačova × K Barrandovu),
# proto zůstává (2.46.0). ``ROAD_AREAS = False`` kreslí silnice v lese a MTBO jen čarou; ``ROUNDABOUT_AREAS = True``
# vrátí plochy i u kruhových objezdů.
ROAD_AREAS = True
ROUNDABOUT_AREAS = False
# Třídy silnic, jejichž uzly tvoří křižovatky (bez účelových komunikací ``road_1``: vjezdy a parkoviště nejsou křižovatky).
JUNCTION_ROAD_BASES = frozenset({"road_2", "road_3", "road_4"})
# Uzly OSM silnic blíž než tolik m tvoří jednu křižovatku; obal shluku; dosah plochy silnic z DTM kolem obalu.
CLUSTER_M = 30.0
HULL_PAD_M = 12.0
AREA_PAD_M = 15.0
# Nejmenší kus plochy křižovatky (m²); menší odřezky u hranice obalu se nekreslí.
AREA_MIN_M2 = 300.0
# Nejmenší ostrůvek (díra v ploše silnic z DTM) v m², který znamená složitou křižovatku (menší jsou mísy stromů).
ISLAND_MIN_M2 = 20.0
# Počet jednosměrných pásů ve shluku, od kterého je křižovatka složitá (rozpletené pásy s ostrůvky bývají jednosměrné);
# počítají se jen silnice od třídy tertiary (``ONEWAY_BASES``) a ne pásy rozdělené vozovky, kterou kreslí 502.0.
MIN_ONEWAY = 4
ONEWAY_BASES = frozenset({"road_3", "road_4"})

_CACHE: dict[tuple, object] = {}


def _road_lines(work_dir: Path):
    from shapely.geometry import LineString

    from app.pipeline import osm_paths as op

    out = []
    for feat in op._paths_features_for_network(work_dir):
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        hw = str((feat.get("properties") or {}).get("highway") or "")
        if geom.get("type") != "LineString" or len(coords) < 2 or op.path_draw_highway(hw) not in JUNCTION_ROAD_BASES:
            continue
        if op.is_bridge_highway(hw) or op.is_tunnel_highway(hw):
            continue
        out.append(LineString([(float(x), float(y)) for x, y in coords]))
    return out


def _oneway(work_dir: Path):
    """``(jednosměrné čáry bez objezdu, čáry kruhových objezdů)`` z ``oneway_roads.geojson`` (shapely)."""
    from shapely.geometry import LineString

    from app.pipeline.osm_paths import ONEWAY_ROADS_NAME

    path = work_dir / "osm_paths" / ONEWAY_ROADS_NAME
    ones, rbs = [], []
    if not path.is_file():
        return ones, rbs
    try:
        feats = json.loads(path.read_text(encoding="utf-8")).get("features") or []
    except (OSError, ValueError):
        return ones, rbs
    from app.pipeline.osm_paths import path_draw_highway

    for f in feats:
        props = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        base = path_draw_highway(str(props.get("highway") or ""))
        if len(coords) < 2 or base not in JUNCTION_ROAD_BASES:
            continue
        ln = LineString([(float(x), float(y)) for x, y in coords])
        if props.get("roundabout"):
            rbs.append(ln)
        elif base in ONEWAY_BASES:
            ones.append(ln)
    return ones, rbs


def junction_clusters(work_dir: Path):
    """Shluky uzlů silniční sítě: ``[(obal shapely, body)]``; uzel = vrchol se stupněm ≥ 3 (konec čáry 1, vnitřní bod 2)."""
    import collections

    from shapely import STRtree
    from shapely.geometry import MultiPoint, Point

    deg: collections.Counter = collections.Counter()
    for ln in _road_lines(work_dir):
        pts = list(ln.coords)
        n = len(pts)
        for i, (x, y) in enumerate(pts):
            deg[(round(x, 2), round(y, 2))] += 1 if i in (0, n - 1) else 2
    nodes = [Point(k) for k, d in deg.items() if d >= 3]
    if not nodes:
        return []
    parent = list(range(len(nodes)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    tree = STRtree(nodes)
    for i, p in enumerate(nodes):
        for j in tree.query(p.buffer(CLUSTER_M)):
            j = int(j)
            if j != i and nodes[j].distance(p) <= CLUSTER_M:
                parent[find(i)] = find(j)
    groups: dict[int, list] = collections.defaultdict(list)
    for i in range(len(nodes)):
        groups[find(i)].append(nodes[i])
    return [(MultiPoint(pts).convex_hull.buffer(HULL_PAD_M), pts) for pts in groups.values()]


def complex_junction_hulls(work_dir: Path, path_source: str = "mixed", preset_id: str = "forest_10000", scale: int = 10000):
    """Obaly složitých křižovatek (shapely polygony); u kruhového objezdu i celý prstenec.

    Dělicí pás rozdělené vozovky (kreslí ji 502.0, ``dual_carriageway``) není ostrůvek a její pásy se nepočítají
    mezi jednosměrky: jinak by každá křižovatka na K Barrandovu byla plochou a 502.0 by se střídalo s plochou.
    """
    from shapely import STRtree
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    from app.pipeline import dtm_import as di
    from app.pipeline.dual_carriageway import dual_axes

    roads_dtm = di.road_geometry(work_dir, path_source)
    ones, rbs = _oneway(work_dir)
    _axes, dual_zone = dual_axes(work_dir, preset_id, scale)
    if dual_zone is not None:
        ones = [ln for ln in ones if ln.difference(dual_zone).length > 0.5 * ln.length]
    ot = STRtree(ones) if ones else None
    rt = STRtree(rbs) if rbs else None
    out = []
    for hull, _pts in junction_clusters(work_dir):
        rb_hit = [rbs[int(k)] for k in rt.query(hull)] if rt is not None else []
        rb_hit = [g for g in rb_hit if g.intersects(hull)]
        n_one = sum(1 for k in (ot.query(hull) if ot is not None else []) if ones[int(k)].intersects(hull))
        islands = 0
        if roads_dtm is not None and not roads_dtm.is_empty:
            # díry v ploše silnic z DTM (ostrůvky) se středem ve shluku; plocha se neořezává, ostrůvek na kraji obalu platí také
            for g in getattr(roads_dtm, "geoms", [roads_dtm]):
                if g.geom_type != "Polygon" or not g.intersects(hull):
                    continue
                for h in g.interiors:
                    hp = Polygon(h)
                    if hp.area < ISLAND_MIN_M2 or not hull.contains(hp.centroid):
                        continue
                    if dual_zone is not None and dual_zone.contains(hp.centroid):
                        continue  # dělicí pás rozdělené vozovky
                    islands += 1
        if rb_hit and not ROUNDABOUT_AREAS:
            continue  # kruhový objezd kreslí čáry (prstenec a ramena)
        if islands >= 1 or rb_hit or n_one >= MIN_ONEWAY:
            if rb_hit:
                hull = unary_union([hull, *[g.buffer(HULL_PAD_M) for g in rb_hit]])
            out.append(hull)
    return out


def without_small_pieces(geom):
    """Plocha křižovatky bez kusů pod ``AREA_MIN_M2`` (odřezky plochy silnic u hranice obalu, 76–85 m² vedle kruhového
    objezdu, D25) nebo None."""
    if geom is None or geom.is_empty:
        return None
    from shapely.ops import unary_union

    kept = [g for g in getattr(geom, "geoms", [geom]) if g.geom_type == "Polygon" and g.area >= AREA_MIN_M2]
    return unary_union(kept) if kept else None


def junction_area_geometry(work_dir: Path, preset_id: str, scale: int, path_source: str = "mixed"):
    """Plocha silnic z DTM u složitých křižovatek (generalizovaná, shapely) nebo None; cache podle souborů."""
    from shapely.ops import unary_union

    from app.pipeline import dtm_import as di
    from app.pipeline.osm_paths import ONEWAY_ROADS_NAME

    stamps = []
    for f in (di.roads_path(work_dir), work_dir / "osm_paths" / "paths_osm.geojson", work_dir / "osm_paths" / ONEWAY_ROADS_NAME):
        try:
            stamps.append(f.stat().st_mtime_ns)
        except OSError:
            stamps.append(0)
    key = (str(work_dir), preset_id, scale, path_source, tuple(stamps))
    if key in _CACHE:
        return _CACHE[key]
    if len(_CACHE) > 16:
        _CACHE.clear()
    result = None
    roads_dtm = di.road_geometry(work_dir, path_source)
    if roads_dtm is not None and not roads_dtm.is_empty:
        hulls = complex_junction_hulls(work_dir, path_source, preset_id, scale)
        if hulls:
            area = roads_dtm.intersection(unary_union([h.buffer(AREA_PAD_M) for h in hulls]))
            polys = [g for g in getattr(area, "geoms", [area]) if g.geom_type == "Polygon" and not g.is_empty]
            if polys:
                result = without_small_pieces(di._generalized(unary_union(polys), work_dir, scale))
                if result is not None and MOUTH_TAPER_MM > 0:
                    from app.pipeline.osm_paths import mm_to_ground_m

                    result = funnel_mouths(result, _entering_lines(work_dir, preset_id, scale), mm_to_ground_m(MOUTH_TAPER_MM, scale))
    _CACHE[key] = result
    return result


# Ústí čáry do plochy křižovatky: plocha končí kolmo na čáru přesně na jejím konci (šikmý konec plochy z DTM nesedí
# na rovný konec kresby silnice → zuby okraje vedle silnice, Petr Retest 5, 2026-10-28) a v posledních tolika mm mapy
# před ústím se zúží na šířku kresby čáry a trychtýřem (``MOUTH_FLARE``) rozšíří – vstup 502.0 Lamačovy měl zub
# (Retest 3). 0 = beze změny.
MOUTH_TAPER_MM = 1.0
# Rozšíření trychtýře: o tolik m půlky šířky na 1 m hloubky (1,0 = 45°). Dřívější pevných 30 m na vnitřním konci se
# u plochy do 20 m šířky vůbec neuplatnilo.
MOUTH_FLARE = 1.0
# Plocha za ústím (vně čáry) se odřízne do této hloubky (m) – šikmý konec plochy přesahující konec kresby.
MOUTH_OUTSIDE_M = 12.0


def _entering_lines(work_dir: Path, preset_id: str, scale: int) -> list[tuple]:
    """Kreslené čáry silnic ``(LineString, půlka šířky výplně m)``: osy 502.0 a OSM silnice mimo pás os (symbol podle tagů)."""
    from shapely.geometry import LineString

    from app.pipeline import osm_paths as op
    from app.pipeline.dtm_sidewalks import clip_outside
    from app.pipeline.dual_carriageway import dual_axes
    from app.pipeline.oom_symbol_map import symbol_fill_width_mm, symbol_line_width_mm

    edge_half = op.paved_edge_width_m(preset_id, scale) / 2.0

    def _half(code: str) -> float:
        """Půlka šířky ústí: okraj plochy navazuje na okrajovou linku silnice (střed linky = půlka celé šířky bez půlky
        okraje plochy; výplň i okrajová linka tak lícují), u čáry bez okrajů půlka výplně."""
        fill = symbol_fill_width_mm(preset_id, scale, code)
        total = symbol_line_width_mm(preset_id, scale, code)
        if not fill and not total:
            return op.drawn_symbol_width_m("road_3", preset_id, scale) * 0.4
        fill_half = op.mm_to_ground_m(fill or total or 0.0, scale) / 2.0
        total_half = op.mm_to_ground_m(total or fill or 0.0, scale) / 2.0
        return max(fill_half, total_half - edge_half)

    out = []
    axes, zone = dual_axes(work_dir, preset_id, scale)
    out += [(LineString(c), _half("502.0")) for c in axes if len(c) >= 2]
    for feat in op._paths_features_for_network(work_dir):
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        hw = str((feat.get("properties") or {}).get("highway") or "")
        if geom.get("type") != "LineString" or len(coords) < 2 or not op.path_draw_highway(hw).startswith("road_"):
            continue
        if op.is_bridge_highway(hw) or op.is_tunnel_highway(hw):
            continue
        pts = [(float(x), float(y)) for x, y in coords]
        parts = clip_outside(pts, zone) if zone is not None else [pts]
        half = _half(op.osm_oom_code(hw, preset_id) or "502")
        out += [(LineString(p), half) for p in parts if len(p) >= 2]
    return out


def funnel_mouths(area, lines: list[tuple], taper_m: float):
    """Plocha ``area`` bez zubů v ústích: tam, kde čára ``(LineString, půlka šířky ústí)`` vstupuje do plochy, plocha
    končí kolmo na čáru v místě vstupu (nic za ním do ``MOUTH_OUTSIDE_M``) a v délce ``taper_m`` od vstupu se ořízne
    na lichoběžník od šířky ústí rozšiřující se o ``MOUTH_FLARE``. Ústí jiných čar (jejich lichoběžníky a pásy) ořez
    nezasáhne."""
    import math

    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import unary_union

    if area is None or area.is_empty or not lines:
        return area
    boundary = area.boundary
    entries = []  # (lichoběžník, ořez: box bez lichoběžníku + plocha za ústím, pás čáry, pás ústí k doplnění)
    for ln, half in lines:
        hit = ln.intersection(boundary)
        pts = [g for g in getattr(hit, "geoms", [hit]) if g.geom_type == "Point"]
        band = ln.buffer(half, cap_style=2)
        for p in pts:
            t = ln.project(p)
            inner = ln.interpolate(min(ln.length, t + 1.0))
            if not area.contains(inner):
                inner = ln.interpolate(max(0.0, t - 1.0))
                if not area.contains(inner):
                    continue
            dx, dy = inner.x - p.x, inner.y - p.y
            n = math.hypot(dx, dy)
            if n < 1e-6:
                continue
            dx, dy = dx / n, dy / n
            nx, ny = -dy, dx
            wide = half + taper_m * MOUTH_FLARE
            q = (p.x + dx * taper_m, p.y + dy * taper_m)
            back = (p.x - dx * MOUTH_OUTSIDE_M, p.y - dy * MOUTH_OUTSIDE_M)
            taper = Polygon(
                [
                    (p.x + nx * half, p.y + ny * half),
                    (q[0] + nx * wide, q[1] + ny * wide),
                    (q[0] - nx * wide, q[1] - ny * wide),
                    (p.x - nx * half, p.y - ny * half),
                ]
            )
            box_ = Polygon(
                [
                    (p.x + nx * wide, p.y + ny * wide),
                    (q[0] + nx * wide, q[1] + ny * wide),
                    (q[0] - nx * wide, q[1] - ny * wide),
                    (p.x - nx * wide, p.y - ny * wide),
                ]
            )
            # za ústím (vně čáry) nic: šikmý konec plochy přesahuje konec kresby i do strany, proto širší pás
            out_w = wide + MOUTH_OUTSIDE_M
            outside = Polygon(
                [
                    (p.x + nx * out_w, p.y + ny * out_w),
                    (back[0] + nx * out_w, back[1] + ny * out_w),
                    (back[0] - nx * out_w, back[1] - ny * out_w),
                    (p.x - nx * out_w, p.y - ny * out_w),
                ]
            )
            # plocha až ke kolmému konci: šikmý konec plochy z DTM na jedné straně nedosahuje ke konci kresby,
            # okraj plochy by vedl napříč koncem silnice → pás čáry mezi ústím a koncem trychtýře se doplní
            mouth = Polygon(
                [
                    (p.x + nx * half, p.y + ny * half),
                    (q[0] + nx * half, q[1] + ny * half),
                    (q[0] - nx * half, q[1] - ny * half),
                    (p.x - nx * half, p.y - ny * half),
                ]
            )
            entries.append((taper, box_.difference(taper).union(outside), band, mouth))
    if not entries:
        return area
    cuts = []
    for k, (taper, cut, band, _mouth) in enumerate(entries):
        others = [g for m, (t2, _c, b2, _m2) in enumerate(entries) if m != k for g in (t2, b2)]
        if others:
            cut = cut.difference(unary_union(others))
        if not cut.is_empty:
            cuts.append(cut)
    out = area.difference(unary_union(cuts)) if cuts else area
    out = unary_union([out, *[e[3] for e in entries]])
    return without_small_pieces(out) if not out.is_empty else None
