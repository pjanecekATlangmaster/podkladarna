"""Les a MTBO: graf silnic z OSM a měření, kolik OSM napojení kresba silnic zachová.

Revize skládání silnic (Petr 2026-11-05, etapa 0): skládání silnic pracuje s čarami a zónami, OSM uzly (kdo se s kým
potkává) se zahodí už při stažení. Generalizace (rozdělená vozovka, rampy, dedup souběhu, ořezy plochami) silnici
ořízne a napojení pak ``road_ends`` dohledává podle vzdálenosti. Tento modul napojení drží jako graf a měří, kolik
z něj kresba ztratila. Kresbu nemění.

- **Graf:** uzel je konec čáry nebo bod, který sdílí víc čar (OSM uzel; po převodu do S-JTSK má v obou čarách stejné
  souřadnice, do ``NODE_SNAP_M``). Hrana vede mezi dvěma uzly. Křižovatka „T“ na průběžné čáře je tedy uzel, což
  ``road_strokes._nodes`` (jen konce čar) nevidí. Most / podjezd bez sdíleného bodu uzel není (jako v OSM).
- **Rameno** je hrana vedoucí do uzlu. Je *kreslené*, když kreslené silnice pokrývají aspoň ``DRAWN_SHARE`` jejího
  úseku ``ARM_NEAR_M``–``ARM_FAR_M`` od uzlu (do ``DRAWN_TOL_M``). Kreslené rameno je *napojené*, když jeho kreslená
  čára do ``CONNECT_RADIUS_M`` od uzlu vede i jiným ramenem téhož uzlu, nebo se dotkne kresby jiné kreslené silnice
  (``TOUCH_M`` + půlka její šířky). Kreslené rameno, které napojené není a nekončí na kreslené ploše (``areas``:
  zpevněné plochy, koridor, chodníky z DTM), je *díra*.
- **Ústí:** končí-li kreslená čára napojeného ramene u jiné silnice, posoudí se konec podle pravidel kresby (Petr
  2026-10-28): silnice s výplní končí ve výplni cíle s výplní (do půlky její výplně), černá silnice na okraji kresby
  silnice s výplní (ne uvnitř), na černou silnici se jde k ose. Jinak je ústí *špatné* (``bad_mouths``).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

# Body čar do tolika m od sebe jsou jeden uzel (sdílený OSM uzel má po převodu shodné souřadnice).
NODE_SNAP_M = 0.05
# Úsek ramene, na kterém se posuzuje, zda se kreslí (m od uzlu).
ARM_NEAR_M = 5.0
ARM_FAR_M = 25.0
# Kreslená čára do tolika m od OSM čáry ji kreslí.
DRAWN_TOL_M = 1.0
# Rameno je kreslené, je-li kreslenými čarami pokryto aspoň tolik jeho úseku.
DRAWN_SHARE = 0.5
# Napojení se hledá do tolika m od uzlu.
CONNECT_RADIUS_M = 25.0
# Dotyk kreslených čar: do tolika m + půlka šířky kresby té druhé.
TOUCH_M = 0.5
# Tolerance konce ústí proti pravidlu (m).
MOUTH_TOL_M = 0.5
# Do měření jdou uzly aspoň s tolika rameny (křižovatky; uzel stupně 2 je jen navazující úsek).
MIN_DEGREE = 3


@dataclass
class RoadEdge:
    u: int
    v: int
    coords: list[tuple[float, float]]
    highway: str
    line: int  # index vstupní čáry

    @property
    def length(self) -> float:
        return sum(math.dist(a, b) for a, b in zip(self.coords, self.coords[1:]))


@dataclass
class RoadGraph:
    nodes: list[tuple[float, float]] = field(default_factory=list)
    edges: list[RoadEdge] = field(default_factory=list)

    def incidence(self) -> dict[int, list[tuple[int, bool]]]:
        """Uzel → ``[(index hrany, True = hrana v uzlu začíná)]``; smyčka je v uzlu dvakrát."""
        out: dict[int, list[tuple[int, bool]]] = {}
        for k, e in enumerate(self.edges):
            out.setdefault(e.u, []).append((k, True))
            out.setdefault(e.v, []).append((k, False))
        return out

    def degree(self, node: int) -> int:
        return len(self.incidence().get(node, []))


class _Snap:
    """Body do ``tol`` m jsou jeden (mřížka s buňkou ``tol``, hledání v sousedních buňkách)."""

    def __init__(self, tol: float):
        self.tol = tol
        self.cells: dict[tuple[int, int], list[int]] = {}
        self.pts: list[tuple[float, float]] = []

    def key(self, p: tuple[float, float]) -> int:
        cx, cy = int(math.floor(p[0] / self.tol)), int(math.floor(p[1] / self.tol))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.cells.get((cx + dx, cy + dy), ()):
                    if math.dist(self.pts[i], p) <= self.tol:
                        return i
        i = len(self.pts)
        self.pts.append((float(p[0]), float(p[1])))
        self.cells.setdefault((cx, cy), []).append(i)
        return i


def build_road_graph(lines, snap_m: float = NODE_SNAP_M) -> RoadGraph:
    """Graf z čar ``[(souřadnice, highway), …]``: uzly v koncích čar a v bodech sdílených víc čarami (nebo dvakrát
    touž čarou), hrany mezi nimi."""
    snap = _Snap(snap_m)
    keyed: list[tuple[list[int], list[tuple[float, float]], str]] = []
    for coords, hw in lines:
        pts = [(float(c[0]), float(c[1])) for c in coords]
        if len(pts) < 2:
            continue
        keys = [snap.key(p) for p in pts]
        keyed.append((keys, pts, str(hw or "")))
    users: dict[int, int] = {}  # bod → počet výskytů ve všech čarách
    for keys, _pts, _hw in keyed:
        for k in keys:
            users[k] = users.get(k, 0) + 1
    is_node: set[int] = set()
    for keys, _pts, _hw in keyed:
        is_node.update((keys[0], keys[-1]))
        is_node.update(k for k in keys if users[k] >= 2)
    node_of: dict[int, int] = {}
    g = RoadGraph()

    def _node(k: int) -> int:
        if k not in node_of:
            node_of[k] = len(g.nodes)
            g.nodes.append(snap.pts[k])
        return node_of[k]

    for li, (keys, pts, hw) in enumerate(keyed):
        start = 0
        for i in range(1, len(keys)):
            if keys[i] in is_node:
                seg = pts[start : i + 1]
                if len(seg) >= 2 and sum(math.dist(a, b) for a, b in zip(seg, seg[1:])) > 0:
                    g.edges.append(RoadEdge(_node(keys[start]), _node(keys[i]), seg, hw, li))
                start = i
    return g


def load_osm_road_graph(work_dir: Path) -> RoadGraph | None:
    """Graf OSM silnic (``road_*`` včetně mostů a tunelů) z neoříznutého ``osm_paths/paths_osm.geojson``; None bez souboru."""
    from app.pipeline.osm_paths import path_draw_highway

    path = Path(work_dir) / "osm_paths" / "paths_osm.geojson"
    try:
        feats = json.loads(path.read_text(encoding="utf-8")).get("features") or []
    except (OSError, ValueError):
        return None
    lines = []
    for f in feats:
        geom = f.get("geometry") or {}
        hw = str((f.get("properties") or {}).get("highway") or "")
        coords = geom.get("coordinates") or []
        if geom.get("type") == "LineString" and len(coords) >= 2 and path_draw_highway(hw).startswith("road_"):
            lines.append(([(float(c[0]), float(c[1])) for c in coords], hw))
    return build_road_graph(lines)


def _arm_section(edge: RoadEdge, at_start: bool):
    """Úsek ramene ``ARM_NEAR_M``–``ARM_FAR_M`` od uzlu (u krátké hrany od 0,3 délky po její konec) nebo None."""
    from shapely.geometry import LineString
    from shapely.ops import substring

    ln = LineString(edge.coords if at_start else edge.coords[::-1])
    a, b = min(ARM_NEAR_M, 0.3 * ln.length), min(ARM_FAR_M, ln.length)
    if b - a < 1.0:
        return None
    sec = substring(ln, a, b)
    return sec if sec.geom_type == "LineString" and sec.length > 0 else None


def connection_report(graph: RoadGraph, drawn, *, areas=None, min_degree: int = MIN_DEGREE) -> dict:
    """Kolik OSM napojení kresba zachová (viz modul). ``drawn`` = ``[(souřadnice, půlka šířky kresby m[, půlka výplně m,
    s výplní])…]`` (bez posledních dvou: výplň = celá šířka, s výplní). ``areas``: plochy (shapely), na nichž konec není díra.

    Vrací ``{"nodes", "arms", "drawn", "connected", "on_area", "holes", "lonely", "mouths", "bad_mouths", "hole_list",
    "bad_mouth_list"}``; ``lonely`` = kreslené rameno uzlu, kde se jiné rameno ani jiná silnice v okolí nekreslí
    (nelze posoudit, do děr se nepočítá); ``mouths`` = posouzená ústí (konec čáry u jiné silnice)."""
    from shapely import STRtree
    from shapely.geometry import LineString, Point

    items = []
    for it in drawn:
        c, h = it[0], float(it[1])
        if len(c) >= 2 and LineString(c).length > 0:
            items.append((LineString(c), h, float(it[2]) if len(it) > 2 else h, bool(it[3]) if len(it) > 3 else True))
    rep = {
        "nodes": 0, "arms": 0, "drawn": 0, "connected": 0, "on_area": 0, "holes": 0, "lonely": 0,
        "mouths": 0, "bad_mouths": 0, "hole_list": [], "bad_mouth_list": [],
    }
    area_near = None
    if areas is not None and not areas.is_empty:
        from shapely.prepared import prep

        area_near = prep(areas.buffer(TOUCH_M))
    if not items:
        return rep
    tree = STRtree([it[0] for it in items])
    inc = graph.incidence()
    for node, arms in inc.items():
        if len(arms) < min_degree:
            continue
        rep["nodes"] += 1
        p = Point(graph.nodes[node])
        near = [int(j) for j in tree.query(p.buffer(CONNECT_RADIUS_M + ARM_FAR_M))]
        # kreslené čáry každého ramene: pokrývají aspoň DRAWN_SHARE úseku
        arm_lines: list[set[int] | None] = []
        for k, at_start in arms:
            rep["arms"] += 1
            sec = _arm_section(graph.edges[k], at_start)
            if sec is None:
                arm_lines.append(None)
                continue
            cand = [j for j in near if items[j][0].distance(sec) <= DRAWN_TOL_M]
            if not cand:
                arm_lines.append(set())
                continue
            band = None
            for j in cand:
                b = items[j][0].buffer(DRAWN_TOL_M)
                band = b if band is None else band.union(b)
            covered = sec.intersection(band).length
            on = {j for j in cand if sec.intersection(items[j][0].buffer(DRAWN_TOL_M)).length >= 1.0}
            arm_lines.append(on if covered >= DRAWN_SHARE * sec.length and on else set())
        ball = p.buffer(CONNECT_RADIUS_M)
        for idx, (k, at_start) in enumerate(arms):
            mine = arm_lines[idx]
            if not mine:
                continue
            rep["drawn"] += 1
            others = set().union(*(s for i2, s in enumerate(arm_lines) if i2 != idx and s))
            if mine & others:
                rep["connected"] += 1  # táž kreslená čára vede i jiným ramenem (průběžná silnice)
                continue
            parts = [items[j][0].intersection(ball) for j in mine]
            # konec kreslené čáry ramene nejblíž uzlu (do CONNECT_RADIUS_M), jinak čára uzlem prochází
            end = None
            for j in mine:
                for q in (items[j][0].coords[0], items[j][0].coords[-1]):
                    d = math.dist(q, graph.nodes[node])
                    if d <= CONNECT_RADIUS_M and (end is None or d < end[1]):
                        end = (Point(q), d, j)
            hit = None
            any_other = False
            for j in near:
                if j in mine:
                    continue
                other, half = items[j][0], items[j][1]
                if other.distance(p) > CONNECT_RADIUS_M + half + TOUCH_M:
                    continue
                any_other = True
                if any(not q.is_empty and q.distance(other) <= TOUCH_M + half for q in parts):
                    if end is None or hit is None or other.distance(end[0]) < items[hit][0].distance(end[0]):
                        hit = j
            x, y = graph.nodes[node]
            if hit is not None:
                rep["connected"] += 1
                if end is not None and items[hit][0].distance(end[0]) <= TOUCH_M + items[hit][1]:
                    bad = _bad_mouth(items[end[2]], items[hit], items[hit][0].distance(end[0]))
                    rep["mouths"] += 1
                    if bad:
                        rep["bad_mouths"] += 1
                        rep["bad_mouth_list"].append({"x": round(x, 1), "y": round(y, 1), "edge": k, "kind": bad})
            elif end is not None and area_near is not None and area_near.intersects(end[0]):
                rep["on_area"] += 1
            elif not any_other:
                rep["lonely"] += 1
            else:
                rep["holes"] += 1
                rep["hole_list"].append({"x": round(x, 1), "y": round(y, 1), "edge": k, "highway": graph.edges[k].highway})
    return rep


def _bad_mouth(arm, target, d: float) -> str | None:
    """Druh špatného ústí (konec ``arm`` ve vzdálenosti ``d`` od osy ``target``) nebo None; položky jako v ``items``."""
    _ln, _half, _fill, major = arm
    _tl, t_half, t_fill, t_major = target
    if t_half <= 0:
        return None  # most, tunel, schody: šířka kresby neznámá
    if t_major and major:
        return None if d <= t_fill + MOUTH_TOL_M else "výplň mimo výplň"
    if t_major:
        return None if d >= t_half - MOUTH_TOL_M else "černá ve výplni"
    return None if d <= MOUTH_TOL_M + TOUCH_M else "mimo osu černé"


def summary_line(rep: dict) -> str:
    """Jeden řádek do logu jobu."""
    drawn = rep.get("drawn") or 0
    share = 100.0 * (rep.get("connected") or 0) / drawn if drawn else 100.0
    return (
        f"Silnice: napojení v křižovatkách {share:.1f} % ({rep.get('connected', 0)} z {drawn} kreslených ramen, "
        f"{rep.get('holes', 0)} děr, {rep.get('on_area', 0)} končí na ploše, {rep.get('bad_mouths', 0)} z {rep.get('mouths', 0)} "
        f"ústí proti pravidlu, {rep.get('nodes', 0)} křižovatek OSM)"
    )


def drawn_road_lines(pieces_by_code: dict, preset_id: str, scale: int, fixed_by_code: dict | None = None) -> list:
    """``[(souřadnice, půlka šířky m, půlka výplně m, s výplní)]`` kreslených silnic z ``build_osm_path_parts``
    (``road_report``); ``fixed_by_code`` (mosty, tunely, schody) jako čáry bez šířky."""
    from app.pipeline.oom_symbol_map import symbol_fill_width_mm
    from app.pipeline.osm_paths import is_major_road_code, mm_to_ground_m
    from app.pipeline.road_ends import _half_width_m

    cache: dict = {}
    out = []
    for code, lines in (pieces_by_code or {}).items():
        # všechny kódy z ``road_report`` jsou silnice (i 504 / 831, které pravidlo tahu dá úzké silnici)
        half = _half_width_m(code, preset_id, scale, cache)
        fill_mm = symbol_fill_width_mm(preset_id, scale, code)
        fill = mm_to_ground_m(fill_mm, scale) / 2.0 if fill_mm else half
        major = is_major_road_code(code, preset_id, scale)
        out.extend((ln, half, fill, major) for ln in lines if len(ln) >= 2)
    for _code, lines in (fixed_by_code or {}).items():
        out.extend((ln, 0.0, 0.0, False) for ln in lines if len(ln) >= 2)
    return out
