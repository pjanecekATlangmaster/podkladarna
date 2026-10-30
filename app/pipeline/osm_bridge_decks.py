"""Lávky z OSM jako víceúrovňové křížení (sprint), když je DTM nemá jako mostovku.

V DTM bývá lávka jen chodník na úrovni 0 a silnice pod ní je vystřižená (nic na úrovni −1). Lávka (OSM ``bridge=*``),
která jinou cestu opravdu kříží, se proto kreslí jako mosty z DTM (``dtm_bridges``): mostovka, spodní zpevněná plocha
512.311 přes celý most a hrany 512.100 na okrajích spodní plochy (ne na koncích lávky, kde se jen napojuje na chodník).
Bez DTM vychází spodní plocha jen z OSM čáry v šířce její kresby.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from app.pipeline import dtm_bridges, osm_paths as op
from app.pipeline.dtm_import import _job_memo

# Křížení blíž ke konci lávky je napojení na cestu, ne průchod pod mostem.
END_CLEAR_M = op._BRIDGE_EDGE_END_CLEAR_M  # 1,5 m
# Pás kolem osy lávky, ve kterém se hledá její povrch z DTM (chodník / silnice na úrovni 0).
DECK_BAND_M = 4.0
# Plocha z DTM patří lávce, když aspoň tento podíl leží v pásu (jinak je to souvislá silnice / chodník).
DECK_DTM_SHARE = 0.5
# Nejmenší poloviční šířka mostovky (m): souvislost mostovky, i kdyby DTM povrch chyběl.
DECK_MIN_HALF_M = 2.0
# Lávky s konci blíž než tolik m (m) jsou jeden most (lávka + rampa).
JOIN_M = 0.5
# Spodní plocha z DTM se bere pro hranu, když leží do tolika m od místa, kde spodní čára protíná okraj mostovky.
EDGE_NEAR_M = 1.0
# Okraj mostovky se hledá v tomto pásu kolem spodní plochy z DTM (okraje ploch z DTM sedí jen přibližně).
EDGE_MATCH_M = 0.5
LIGHT_TYPE = "ChodnikPlocha"
HEAVY_TYPE = "ProvozniPlochaPozemniKomunikacePlocha"


def _read_lines(work_dir: Path) -> list[tuple[object, str]]:
    """``[(LineString, highway)]`` ze stejného souboru jako ``osm_paths.bridge_deck_geometry``."""
    from shapely.geometry import LineString

    for name in ("paths_osm.geojson", "paths_mixed.geojson", "paths.geojson"):
        gj_path = work_dir / "osm_paths" / name
        if gj_path.is_file():
            break
    else:
        return []
    try:
        data = json.loads(gj_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for feat in data.get("features") or []:
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") != "LineString" or len(coords) < 2:
            continue
        line = LineString([(float(c[0]), float(c[1])) for c in coords])
        if line.length > 0:
            out.append((line, str((feat.get("properties") or {}).get("highway") or "")))
    return out


def _tangent(line, d: float) -> tuple[float, float]:
    a = line.interpolate(max(0.0, d - 1.0))
    b = line.interpolate(min(line.length, d + 1.0))
    dx, dy = b.x - a.x, b.y - a.y
    n = math.hypot(dx, dy) or 1.0
    return dx / n, dy / n


def _points(geom) -> list:
    if geom.is_empty:
        return []
    return [geom] if geom.geom_type == "Point" else [g for g in getattr(geom, "geoms", []) if g.geom_type == "Point"]


def _polygons(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    return [geom] if geom.geom_type == "Polygon" else [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]


@_job_memo
def _dtm_polygons(work_dir: Path, kind: str) -> tuple:
    """Plochy DTM jako shapely (``sidewalks`` + ``roads`` nebo ``lower`` = i parkoviště)."""
    from app.pipeline import dtm_import as di

    raw = di._load_sidewalk_polygons(work_dir) + di._load_road_polygons(work_dir)
    if kind == "lower":
        raw = raw + di._load_polygons_file(di.parking_path(work_dir))
    return tuple(g for g in (dtm_bridges._poly(r) for r in raw) if g is not None)


@_job_memo
def _buildings(work_dir: Path):
    """Budovy z OSM a DTM (shapely) nebo None – lávka vedoucí do budovy se v budově nekreslí."""
    from shapely.geometry import shape
    from shapely.ops import unary_union

    from app.pipeline import dtm_import as di

    polys = []
    gj = work_dir / "osm_paths" / "buildings.geojson"
    try:
        feats = (json.loads(gj.read_text(encoding="utf-8")).get("features") or []) if gj.is_file() else []
    except (OSError, json.JSONDecodeError):
        feats = []
    for feat in feats:
        try:
            g = shape(feat["geometry"])
        except Exception:
            continue
        if not g.is_empty and g.geom_type in {"Polygon", "MultiPolygon"}:
            polys.append(g if g.is_valid else g.buffer(0))
    if di.dtm_owns_buildings(work_dir):
        polys.extend(g for g in (dtm_bridges._poly(r) for r in di._load_polygons_file(di.buildings_path(work_dir))) if g is not None)
    return unary_union(polys) if polys else None


@_job_memo
def _analysis(work_dir: Path) -> dict:
    """Lávky ve skupinách s křížením, jejich mostovky a křížení: ``decks``, ``sources``, ``group_decks``, ``crossings``."""
    from shapely.geometry import Point
    from shapely.ops import unary_union

    from app.pipeline import dtm_import as di

    empty = {"decks": [], "sources": None, "group_decks": {}, "crossings": []}
    lines = _read_lines(work_dir)
    bridges, lower = [], []
    for line, hw in lines:
        if op.is_tunnel_highway(hw):
            continue
        (bridges if op.is_bridge_highway(hw) else lower).append((line, hw))
    if not bridges or not lower:
        return empty
    dtm_foot = dtm_bridges.footprint(di.dtm_deck_levels(work_dir))
    if dtm_foot is not None and not dtm_foot.is_empty:  # most z DTM kreslí DTM
        bridges = [
            (ln, hw)
            for ln, hw in bridges
            if op._line_overlap_m(list(ln.coords), dtm_foot) < op.DTM_BRIDGE_OVERLAP_M
        ]
    if not bridges:
        return empty
    # skupiny: lávky spojené konci
    parent = list(range(len(bridges)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    ends = [(Point(ln.coords[0]), Point(ln.coords[-1])) for ln, _hw in bridges]
    for i in range(len(bridges)):
        for j in range(i + 1, len(bridges)):
            if any(a.distance(b) <= JOIN_M for a in ends[i] for b in ends[j]):
                parent[find(i)] = find(j)
    # křížení lávky s jinou cestou mimo její konce
    buildings = _buildings(work_dir)
    crossings = []  # (index lávky, bod křížení, spodní čára, její highway)
    for i, (bridge, _bhw) in enumerate(bridges):
        bminx, bminy, bmaxx, bmaxy = bridge.bounds
        for line, lhw in lower:
            lminx, lminy, lmaxx, lmaxy = line.bounds
            if lminx > bmaxx or lmaxx < bminx or lminy > bmaxy or lmaxy < bminy:
                continue
            for p in _points(bridge.intersection(line)):
                if buildings is not None and buildings.contains(p):
                    continue  # křížení uvnitř budovy (lávka do budovy) se nekreslí
                s = bridge.project(p)
                if s <= END_CLEAR_M or bridge.length - s <= END_CLEAR_M:
                    continue
                s_l = line.project(p)
                if (s_l <= END_CLEAR_M or line.length - s_l <= END_CLEAR_M) and sum(
                    1 for other, _ohw in lower if other.distance(p) <= JOIN_M
                ) < 2:
                    continue  # cesta (schody) končí na lávce a dál nepokračuje – napojení, ne průchod pod ní
                tb, tl = _tangent(bridge, s), _tangent(line, line.project(p))
                if abs(tb[0] * tl[1] - tb[1] * tl[0]) < op._BRIDGE_EDGE_MIN_SIN:
                    continue
                crossings.append((i, p, line, lhw))
    taken = {find(c[0]) for c in crossings}
    if not taken:
        return empty
    surfaces = _dtm_polygons(work_dir, "sidewalks")
    decks, deck_geoms, sources = [], {}, []
    for i, (bridge, hw) in enumerate(bridges):
        grp = find(i)
        if grp not in taken:
            continue
        band = bridge.buffer(DECK_BAND_M, cap_style="flat")
        parts = [bridge.buffer(max(op.drawn_width_m(hw) / 2.0, DECK_MIN_HALF_M), cap_style="flat")]
        for g in surfaces:
            if not band.intersects(g):
                continue
            inside = g.intersection(band)
            if inside.area >= DECK_DTM_SHARE * g.area:
                parts.append(inside)
                sources.append(g)
        deck = unary_union(parts)
        if buildings is not None:
            deck = deck.difference(buildings)  # lávka vedoucí do budovy končí na jejím obrysu
        typ = HEAVY_TYPE if op.path_draw_highway(hw).startswith("road_") else LIGHT_TYPE
        for poly in _polygons(deck):
            rings = [list(poly.exterior.coords)] + [list(r.coords) for r in poly.interiors]
            decks.append({"l": 1, "t": typ, "p": rings})
        deck_geoms.setdefault(grp, []).append(deck)
    return {
        "decks": decks,
        "sources": unary_union(sources) if sources else None,
        "group_decks": {k: unary_union(v) for k, v in deck_geoms.items()},
        "crossings": [(find(i), p, ln, hw, bridges[i][0]) for i, p, ln, hw in crossings if find(i) in deck_geoms],
    }


def osm_decks(work_dir: Path) -> list[dict]:
    """Mostovky OSM lávek křížících cestu ve formátu ploch mostů z DTM (``[{"l", "t", "p"}]``, úroveň 1)."""
    return _analysis(work_dir)["decks"]


def deck_sources(work_dir: Path):
    """Plochy DTM (chodníky, silnice), které tvoří mostovku OSM lávky – nejsou „pod mostem“ (shapely nebo None)."""
    return _analysis(work_dir)["sources"]


def osm_lower_paved(work_dir: Path):
    """Spodní zpevněná plocha z OSM: kresba čar křižovaných pod lávkami (shapely nebo None)."""
    from shapely.ops import unary_union

    seen, parts = set(), []
    for _grp, _p, line, lhw, _bridge in _analysis(work_dir)["crossings"]:
        if id(line) not in seen:
            seen.add(id(line))
            parts.append(line.buffer(op.drawn_width_m(lhw) / 2.0))
    return unary_union(parts) if parts else None


def _edge_near(deck, boundary, q, low, half_m: float):
    """Kus okraje mostovky souvislý a nejblíž bodu ``q`` (spodní plocha ``low``, jinak ``half_m`` kolem ``q``) nebo None."""
    from shapely.geometry import Point
    from shapely.ops import linemerge, substring

    area = low.buffer(EDGE_MATCH_M) if low is not None else Point(q.x, q.y).buffer(half_m)
    cut = boundary.intersection(area)
    pieces = [cut] if cut.geom_type == "LineString" else [g for g in getattr(cut, "geoms", []) if g.geom_type == "LineString"]
    if not pieces:
        return None
    merged = linemerge(pieces)
    pieces = [merged] if merged.geom_type == "LineString" else list(merged.geoms)
    piece = min(pieces, key=lambda g: g.distance(q))
    if piece.distance(q) > EDGE_NEAR_M + EDGE_MATCH_M:
        return None
    if piece.length > dtm_bridges.ROAD_EDGE_MAX_LEN_M:
        s = piece.project(q)
        half = dtm_bridges.ROAD_EDGE_MAX_LEN_M / 2.0
        piece = substring(piece, max(0.0, s - half), min(piece.length, s + half))
    return piece if piece.geom_type == "LineString" else None


@_job_memo
def osm_entrance_edges(work_dir: Path) -> list[list[tuple[float, float]]]:
    """Hrany 512.100 na okrajích mostovky OSM lávky tam, kde ji kříží spodní čára (vnitřek mostu vlevo od směru)."""
    from shapely.ops import linemerge, unary_union

    an = _analysis(work_dir)
    if not an["crossings"]:
        return []
    lower_polys = _dtm_polygons(work_dir, "lower")
    sources = an["sources"]
    group_polys = {grp: _polygons(g) for grp, g in an["group_decks"].items()}
    per_deck: dict[tuple[int, int], list] = {}
    for grp, p, line, lhw, bridge in an["crossings"]:
        if not group_polys.get(grp):
            continue
        idx = min(range(len(group_polys[grp])), key=lambda k: group_polys[grp][k].distance(p))
        deck = group_polys[grp][idx]
        boundary = deck.boundary
        s_p = line.project(p)
        tb, tl = _tangent(bridge, bridge.project(p)), _tangent(line, s_p)
        sin = max(abs(tb[0] * tl[1] - tb[1] * tl[0]), op._BRIDGE_EDGE_MIN_SIN)
        half = min(dtm_bridges.ROAD_EDGE_MAX_LEN_M / 2.0, (op.drawn_width_m(lhw) / 2.0 + 0.5) / sin)
        # q: nejbližší průsečík spodní čáry s okrajem mostovky na každé straně křížení
        sides = {-1: None, 1: None}
        for q in _points(line.intersection(boundary)):
            d = line.project(q) - s_p
            side = 1 if d > 0 else -1
            if abs(d) > 0.0 and (sides[side] is None or abs(d) < abs(sides[side][0])):
                sides[side] = (d, q)
        for found in sides.values():
            if found is None:
                continue
            q = found[1]
            near = [g.difference(sources) if sources is not None else g for g in lower_polys if g.distance(q) <= EDGE_NEAR_M]
            near = [g for g in near if not g.is_empty and g.distance(q) <= EDGE_NEAR_M]  # mostovka sama není spodní plocha
            low = unary_union(near) if near else None
            piece = _edge_near(deck, boundary, q, low, half)
            if piece is not None:
                per_deck.setdefault((grp, idx), [deck]).append(piece)
    edges: list[list[tuple[float, float]]] = []
    for deck, *pieces in per_deck.values():
        merged = unary_union(pieces)
        if merged.geom_type == "MultiLineString":
            merged = linemerge(merged)
        for piece in [merged] if merged.geom_type == "LineString" else list(getattr(merged, "geoms", [])):
            if piece.geom_type != "LineString" or piece.length < dtm_bridges.EDGE_MIN_LEN_M:
                continue
            coords = dtm_bridges._orient_inside(piece, deck)
            if coords is not None:
                edges.append(coords)
    return edges
