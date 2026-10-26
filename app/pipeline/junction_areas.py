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
# Třídy silnic, jejichž uzly tvoří křižovatky (bez účelových komunikací ``road_1``: vjezdy a parkoviště nejsou křižovatky).
JUNCTION_ROAD_BASES = frozenset({"road_2", "road_3", "road_4"})
# Uzly OSM silnic blíž než tolik m tvoří jednu křižovatku; obal shluku; dosah plochy silnic z DTM kolem obalu.
CLUSTER_M = 30.0
HULL_PAD_M = 12.0
AREA_PAD_M = 15.0
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
        if islands >= 1 or rb_hit or n_one >= MIN_ONEWAY:
            if rb_hit:
                hull = unary_union([hull, *[g.buffer(HULL_PAD_M) for g in rb_hit]])
            out.append(hull)
    return out


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
                result = di._generalized(unary_union(polys), work_dir, scale)
                if result is not None and result.is_empty:
                    result = None
    _CACHE[key] = result
    return result
