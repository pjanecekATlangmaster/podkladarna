"""Ploty z DTM kraje (ZPS_KR, objekt ``Plot``) – načtení, cache a napojení v rozích.

Geodeticky zaměřené ploty v OSM často chybí. V JVF jsou ale rozsekané na krátké
úseky (po lomech, po změně materiálu). Pro mapu se mají navazující úseky spojit
do jedné souvislé čáry, aby plot nebyl hromada drobných objektů – spojují se
koncové body, které se potkají (v rozích i na přímce). T-křižovatka (tři úseky
v jednom bodě) zůstane rozdělená, protože tam plot opravdu větví.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from app.pipeline.jvf_reader import iter_records

# ISSprOM: nepřekonatelný plot / zábradlí, živý plot = zřetelná hranice vegetace.
CODE_FENCE = "518"
CODE_HEDGE = "416"
# Atribut DruhPlotu: 1 dřevěný, 2 drátěný, 3 kovový, 4 zděný, 5 živý, 98 jiný, 99 nezjištěno.
DRUH_HEDGE = "5"
# Koncové body blíž než toto (m) se považují za jeden uzel.
JOIN_TOLERANCE_M = 0.3
CACHE_SUFFIX = ".plot2.json"
AREAS_CACHE_SUFFIX = ".orient3.json"

# Plochy z ZPS_OPL, podle kterých se orientují ploty (čárky symbolu 518 mají mířit
# k „olivě“ = soukromá plocha, ne k chodníku / silnici).
OLIVE_TYPES = ("ZahradaPlocha", "DvurNadvoriPlocha")
PUBLIC_TYPES = (
    "ChodnikPlocha",
    "ProvozniPlochaPozemniKomunikacePlocha",
    "CyklostezkaPlocha",
    "ParkovisteOdstavnaPlochaPlocha",
    "DopravniOstruvekPlocha",
    "NastupistePlocha",
    "SchodistePlocha",
)
# Čárky (dvojitý tick) symbolu 518 jsou na PRAVÉ straně ve směru kreslení. Původní
# odhad „vlevo“ (z lávek, 512.100) se na reálné mapě ukázal většinově špatně →
# otočeno (uživatelské ověření 2026-10-19). Přepíná se touto konstantou.
TICKS_ON_LEFT = False
# Soukromé plochy, u kterých je plot věrohodný (zahrada, dvůr, budova); viz filter_dubious_fences.
PRIVATE_BUILDING_TYPES = ("BudovaPlocha",)
# Zeleň a les: spolu se zahradou/dvorem tvoří „vnitřek plochy“ (viz filter_dubious_fences).
OPEN_TYPES = ("UdrzovanaPlochaZelenePlocha", "LesPlocha")
# Plot s neznámým původem uvnitř jedné plochy se vynechá, jen když v tomto okolí (m) není budova.
INNER_BUILDING_FREE_M = 15.0
INNER_SIDE_OFFSET_M = 1.2
# ZpusobPorizeniZPS: 99 = nezjištěno (stará migrace z roku 2008, ne zaměření).
ZPUSOB_UNKNOWN = "99"
# Plot s neznámým původem musí ležet do této vzdálenosti (m) od hranice soukromé plochy.
DUBIOUS_NEAR_M = 0.35
# Orientace plotu: vzorek každých ``ORIENT_STEP_M`` m, bod ``ORIENT_OFFSET_M`` m od osy.
ORIENT_STEP_M = 6.0
ORIENT_OFFSET_M = 1.2
# Odstup plotu od cesty/chodníku směrem k soukromé ploše (zahrada, dvůr, budova), aby se
# čára nekryla s okrajem cesty a byla čitelná. 0 = bez odstupu.
FENCE_SETBACK_M = 0.0
# Oplocení (uzavřený plot): uvnitř ležící ploty se zahodí, oplocení se zvenku vyplní olivou.
ENCLOSURE_CLOSE_TOL_M = 0.3
ENCLOSURE_MIN_AREA_M2 = 15.0
ENCLOSURE_MAX_AREA_M2 = 100_000.0
# Oplocení se vyplní olivou, jen když aspoň tento podíl jeho vnitřku je zahrada/dvůr (OPL).
ENCLOSURE_PRIVATE_SHARE = 0.5
# Parcela uzavřená plotem a okraji cest/zpevněných ploch (plot u cesty v datech chybí): vyplní
# se, když má plot aspoň ``PLOT_FENCE_SHARE`` obvodu (±``PLOT_FENCE_BUFFER_M``), průměrnou šířku
# aspoň ``PLOT_MIN_WIDTH_M``, nejvýš ``PLOT_MAX_AREA_M2`` a stojí v ní budova.
PLOT_FENCE_SHARE = 0.35
# Oplocení uzavřené samotnými ploty se vyplní vždy (zahrada, areál, hřiště …), je-li dost
# široké a ne příliš velké; větší plochy se vyplní jen s převahou zahrady/dvora (OPL).
ENCLOSURE_CLOSED_MAX_AREA_M2 = 20_000.0
PLOT_FENCE_BUFFER_M = 0.7
PLOT_MIN_WIDTH_M = 2.5
PLOT_MAX_AREA_M2 = 3000.0
# Odsun od cest: poloviční šířka čáry plotu (m), mezera od černé čáry cesty (m),
# největší povolený posun jednoho bodu a rozestup bodů při úpravě.
FENCE_HALF_WIDTH_M = 0.4
PATH_GAP_M = 0.3
AVOID_MAX_SHIFT_M = 3.0
AVOID_STEP_M = 2.0


@dataclass(frozen=True)
class Fence:
    coords: tuple[tuple[float, float], ...]  # S-JTSK (x, y)
    druh: str  # DruhPlotu
    level: int  # UrovenUmisteniObjektuZPS
    boundary: bool  # HraniceJinehoObjektu (plot je zároveň hranicí jiného objektu)
    zpusob: str = ""  # ZpusobPorizeniZPS (1 terestricky … 5 odvozením, 99 nezjištěno)
    oid: str = ""  # ID objektu v DTM (pro ladění / hlášení chyb)

    @property
    def code(self) -> str:
        return CODE_HEDGE if self.druh == DRUH_HEDGE else CODE_FENCE


def load_fences(package_zip: str | Path, *, use_cache: bool = True) -> list[Fence]:
    """Všechny ploty z balíku ZPS_KR (poprvé ~20 s u velké MČ, pak z JSON cache)."""
    package_zip = Path(package_zip)
    cache = package_zip.with_name(package_zip.name + CACHE_SUFFIX)
    if use_cache and cache.is_file() and cache.stat().st_mtime >= package_zip.stat().st_mtime:
        try:
            rows = json.loads(cache.read_text(encoding="utf-8"))
            return [
                Fence(tuple((x, y) for x, y in r["c"]), r["d"], r["l"], bool(r["b"]), r["z"], r["i"])
                for r in rows
            ]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    fences: list[Fence] = []
    for rec in iter_records(package_zip, {"Plot"}):
        if rec.geom_type != "LineString" or len(rec.coords) < 2:
            continue
        fences.append(
            Fence(
                tuple((float(c[0]), float(c[1])) for c in rec.coords),
                rec.attrs.get("DruhPlotu", "99"),
                rec.level,
                rec.attrs.get("HraniceJinehoObjektu", "false").lower() == "true",
                rec.attrs.get("ZpusobPorizeniZPS", ""),
                rec.attrs.get("ID", ""),
            )
        )
    if use_cache:
        try:
            cache.write_text(
                json.dumps(
                    [
                        {
                            "c": [list(p) for p in f.coords],
                            "d": f.druh,
                            "l": f.level,
                            "b": f.boundary,
                            "z": f.zpusob,
                            "i": f.oid,
                        }
                        for f in fences
                    ]
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
    return fences


def fences_in_bounds(
    fences: list[Fence], bounds: tuple[float, float, float, float]
) -> list[Fence]:
    """Ploty, jejichž obdélník zasahuje do ``(xmin, ymin, xmax, ymax)``."""
    xmin, ymin, xmax, ymax = bounds
    out = []
    for f in fences:
        xs = [p[0] for p in f.coords]
        ys = [p[1] for p in f.coords]
        if max(xs) < xmin or min(xs) > xmax or max(ys) < ymin or min(ys) > ymax:
            continue
        out.append(f)
    return out


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _snap_endpoints(
    lines: list[list[tuple[float, float]]], tol: float
) -> list[list[tuple[float, float]]]:
    """Koncové body do vzdálenosti ``tol`` splyne do jednoho uzlu (průměr)."""
    ends = [(i, 0) for i in range(len(lines))] + [(i, -1) for i in range(len(lines))]
    pts = [lines[i][e] for i, e in ends]
    uf = _UnionFind(len(pts))
    cell = max(tol, 1e-6)
    grid: dict[tuple[int, int], list[int]] = {}
    for k, (x, y) in enumerate(pts):
        cx, cy = int(math.floor(x / cell)), int(math.floor(y / cell))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((cx + dx, cy + dy), ()):
                    if math.hypot(x - pts[j][0], y - pts[j][1]) <= tol:
                        uf.union(k, j)
        grid.setdefault((cx, cy), []).append(k)
    groups: dict[int, list[int]] = {}
    for k in range(len(pts)):
        groups.setdefault(uf.find(k), []).append(k)
    snapped = [list(l) for l in lines]
    for members in groups.values():
        if len(members) < 2:
            continue
        mx = sum(pts[k][0] for k in members) / len(members)
        my = sum(pts[k][1] for k in members) / len(members)
        for k in members:
            i, e = ends[k]
            snapped[i][e] = (mx, my)
    return snapped


def merge_lines(
    lines: list[list[tuple[float, float]]], tol: float = JOIN_TOLERANCE_M
) -> list[list[tuple[float, float]]]:
    """Spojí navazující linie (koncové body do ``tol``) do souvislých polylinií.

    Spojuje jen v uzlech, kde se potkají přesně dva úseky (roh / pokračování);
    uzel se třemi a více úseky zůstane rozdělený. Duplicitní úseky se zahodí.
    """
    from shapely.geometry import LineString, MultiLineString
    from shapely.ops import linemerge

    clean: list[list[tuple[float, float]]] = []
    for ln in lines:
        pts = [ln[0]] + [p for a, p in zip(ln, ln[1:]) if p != a]
        if len(pts) >= 2:
            clean.append(pts)
    if not clean:
        return []
    snapped = _snap_endpoints(clean, tol)
    seen: set[tuple] = set()
    unique: list[list[tuple[float, float]]] = []
    for ln in snapped:
        key = tuple(ln) if ln[0] <= ln[-1] else tuple(reversed(ln))
        if len(set(ln)) < 2 or key in seen:
            continue
        seen.add(key)
        unique.append(ln)
    merged = linemerge(MultiLineString([LineString(ln) for ln in unique]))
    geoms = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
    return [[(x, y) for x, y in g.coords] for g in geoms if g.geom_type == "LineString"]


def merged_fences(
    fences: list[Fence], tol: float = JOIN_TOLERANCE_M
) -> list[tuple[str, list[tuple[float, float]]]]:
    """``[(kód symbolu, polyline)]`` – ploty nad povrchem, napojené podle kódu.

    Úrovně < 0 (pod povrchem) se vynechají. Ploty různých materiálů se stejným
    symbolem (518) se spojí do jednoho kusu, živý plot (416) zvlášť.
    """
    out: list[tuple[str, list[tuple[float, float]]]] = []
    for code in (CODE_FENCE, CODE_HEDGE):
        group = [list(f.coords) for f in fences if f.level >= 0 and f.code == code]
        out.extend((code, ln) for ln in merge_lines(group, tol))
    return out


def load_orientation_areas(
    opl_zip: str | Path, *, use_cache: bool = True
) -> dict[str, list[list[list[tuple[float, float]]]]]:
    """Plochy z ZPS_OPL pro orientaci plotů: ``{"olive": [polygon, …], "public": [...]}``.

    Polygon = ``[vnější obrys, díra, …]`` (S-JTSK x, y). Výsledek se cachuje u balíku.
    """
    opl_zip = Path(opl_zip)
    cache = opl_zip.with_name(opl_zip.name + AREAS_CACHE_SUFFIX)
    if use_cache and cache.is_file() and cache.stat().st_mtime >= opl_zip.stat().st_mtime:
        try:
            raw = json.loads(cache.read_text(encoding="utf-8"))
            return {
                k: [[[(x, y) for x, y in ring] for ring in poly] for poly in raw[k]]
                for k in ("olive", "public", "building", "open")
            }
        except (OSError, ValueError, KeyError, TypeError):
            pass
    wanted = set(OLIVE_TYPES) | set(PUBLIC_TYPES) | set(PRIVATE_BUILDING_TYPES) | set(OPEN_TYPES)
    out: dict[str, list] = {"olive": [], "public": [], "building": [], "open": []}
    for rec in iter_records(opl_zip, wanted):
        if rec.geom_type != "Polygon" or not rec.coords:
            continue
        poly = [[(float(c[0]), float(c[1])) for c in ring] for ring in rec.coords if len(ring) >= 4]
        if not poly:
            continue
        if rec.type_name in OLIVE_TYPES:
            out["olive"].append(poly)
        elif rec.type_name in PRIVATE_BUILDING_TYPES:
            out["building"].append(poly)
        elif rec.type_name in OPEN_TYPES:
            out["open"].append(poly)
        else:
            out["public"].append(poly)
    if use_cache:
        try:
            cache.write_text(
                json.dumps({k: [[[list(p) for p in ring] for ring in poly] for poly in v] for k, v in out.items()}),
                encoding="utf-8",
            )
        except OSError:
            pass
    return out


def orient_fences(
    lines: list[tuple[str, list[tuple[float, float]]]],
    areas: dict[str, list],
    *,
    bounds: tuple[float, float, float, float] | None = None,
    ticks_on_left: bool = TICKS_ON_LEFT,
    setback_m: float = 0.0,
) -> list[tuple[str, list[tuple[float, float]]]]:
    """Otočí směr plotů (kód 518) tak, aby čárky symbolu mířily k olivě, ne k chodníku.

    Podél čáry se po ``ORIENT_STEP_M`` m zkoumají body vlevo / vpravo; zahrada nebo dvůr
    = +1 (oliva), chodník / komunikace / parkoviště = −1. Čára se otočí, když by na straně
    čárek bylo méně „olivy“ než na druhé. Remíza nebo žádná plocha = beze změny. Živý plot
    (416) čárky nemá a zůstává.

    ``setback_m`` > 0: plot (518 i 416), který má na jedné straně chodník/komunikaci a na
    druhé olivu, se navíc odsune o tolik metrů k olivě (od cesty).
    """
    from shapely import STRtree
    from shapely.geometry import LineString, Point, Polygon

    def _polys(items):
        out = []
        for poly in items:
            try:
                p = Polygon(poly[0], poly[1:])
                out.append(p if p.is_valid else p.buffer(0))
            except (ValueError, IndexError):
                continue
        return out

    def _in_bounds(items):
        if bounds is None:
            return items
        xmin, ymin, xmax, ymax = bounds
        keep = []
        for poly in items:
            xs = [p[0] for p in poly[0]]
            ys = [p[1] for p in poly[0]]
            if not (max(xs) < xmin or min(xs) > xmax or max(ys) < ymin or min(ys) > ymax):
                keep.append(poly)
        return keep

    olive = _polys(_in_bounds(areas.get("olive") or []))
    public = _polys(_in_bounds(areas.get("public") or []))
    if not olive and not public:
        return lines
    tree_o = STRtree(olive) if olive else None
    tree_p = STRtree(public) if public else None

    def _hit(tree, polys, pt) -> bool:
        if tree is None:
            return False
        return any(polys[i].contains(pt) for i in tree.query(pt))

    def _score(x: float, y: float) -> int:
        pt = Point(x, y)
        o = _hit(tree_o, olive, pt)
        p = _hit(tree_p, public, pt)
        return 1 if o and not p else (-1 if p and not o else 0)

    out: list[tuple[str, list[tuple[float, float]]]] = []
    for code, coords in lines:
        if code not in (CODE_FENCE, CODE_HEDGE) or len(coords) < 2:
            out.append((code, coords))
            continue
        line = LineString(coords)
        length = line.length
        net = 0
        raw = 0  # olivová strana: >0 vlevo, <0 vpravo
        road_votes = 0  # vzorky, kde je na jedné straně cesta a na druhé ne
        d = ORIENT_STEP_M / 2.0
        while d < length:
            a = line.interpolate(max(0.0, d - 0.5))
            b = line.interpolate(min(length, d + 0.5))
            dx, dy = b.x - a.x, b.y - a.y
            n = math.hypot(dx, dy)
            if n > 1e-9:
                lx, ly = -dy / n, dx / n  # levá normála (S-JTSK: x východ, y sever)
                p = line.interpolate(d)
                left = _score(p.x + ORIENT_OFFSET_M * lx, p.y + ORIENT_OFFSET_M * ly)
                right = _score(p.x - ORIENT_OFFSET_M * lx, p.y - ORIENT_OFFSET_M * ly)
                net += (left - right) if ticks_on_left else (right - left)
                raw += left - right
                if (left == -1) != (right == -1):
                    road_votes += 1
            d += ORIENT_STEP_M
        if code == CODE_FENCE and net < 0:
            coords = list(reversed(coords))
        if setback_m > 0 and raw != 0 and road_votes > 0:
            coords = _setback(coords, setback_m if raw > 0 else -setback_m, line_in=line, reversed_=code == CODE_FENCE and net < 0)
        out.append((code, coords))
    return out


def _setback(coords, signed_m, *, line_in, reversed_):
    """Posune čáru o ``signed_m`` m (+ vlevo ve směru původního ``line_in``); při chybě beze změny."""
    from shapely.geometry import LineString

    try:
        # směr původní čáry; ``coords`` mohly být otočeny → posun se počítá na původním směru
        shifted = line_in.offset_curve(signed_m, join_style="mitre", mitre_limit=2.0)
    except Exception:
        return coords
    if shifted.is_empty or not isinstance(shifted, LineString) or len(shifted.coords) < 2:
        return coords
    # délka se nesmí výrazně změnit (ostré zlomy) – jinak raději původní poloha
    if abs(shifted.length - line_in.length) > max(2.0, 0.25 * line_in.length):
        return coords
    pts = [(float(x), float(y)) for x, y, *_ in shifted.coords]
    return list(reversed(pts)) if reversed_ else pts


def filter_dubious_fences(
    fences: list[Fence],
    areas: dict[str, list],
    *,
    near_m: float = DUBIOUS_NEAR_M,
    snap_m: float = JOIN_TOLERANCE_M,
) -> tuple[list[Fence], list[Fence]]:
    """Vynechá „plot“, který nejspíš nikdy neexistoval: ``(ponechané, vynechané)``.

    V DTM jsou z roku 2008 přenesené čáry s neznámým původem (``ZpusobPorizeniZPS = 99``),
    které jsou jen hranicí jiného objektu (``HraniceJinehoObjektu``), ne skutečným
    plotem. Skutečný plot dělí soukromý pozemek od okolí, proto se takový úsek
    ponechá, když leží u hranice zahrady, dvora nebo budovy. Ostatní se vynechají a
    vynechání se šíří na navazující úseky s neznámým původem, které také nemají
    soukromého souseda (pokračování téže čáry). Zaměřené ploty (způsob 1–5) se nikdy
    nevynechávají. Bez ploch (``areas``) se nic nevynechává.
    """
    from shapely import STRtree
    from shapely.geometry import LineString

    private = (areas.get("olive") or []) + (areas.get("building") or [])
    if not private or not fences:
        return list(fences), []
    borders = []
    for poly in private:
        for ring in poly:
            if len(ring) >= 2:
                borders.append(LineString(ring))
    if not borders:
        return list(fences), []
    tree = STRtree(borders)

    def near_private(line: LineString) -> bool:
        for frac in (0.25, 0.5, 0.75):
            p = line.interpolate(frac, normalized=True)
            if any(borders[i].distance(p) <= near_m for i in tree.query(p.buffer(near_m))):
                return True
        return False

    lines = [LineString(f.coords) for f in fences]
    unknown = [f.zpusob == ZPUSOB_UNKNOWN for f in fences]
    # soukromého souseda stačí zjišťovat jen u úseků s neznámým původem
    has_private = [(not unknown[i]) or near_private(lines[i]) for i in range(len(fences))]
    dropped = {
        i for i, f in enumerate(fences) if unknown[i] and f.boundary and not has_private[i]
    }
    dropped |= _inside_single_area(fences, lines, unknown, has_private, areas)

    def cell(p: tuple[float, float]) -> tuple[int, int]:
        return (int(round(p[0] / snap_m)), int(round(p[1] / snap_m)))

    ends: dict[tuple[int, int], list[int]] = {}
    for i, f in enumerate(fences):
        for p in (f.coords[0], f.coords[-1]):
            ends.setdefault(cell(p), []).append(i)
    changed = True
    while changed:
        changed = False
        for i, f in enumerate(fences):
            if i in dropped or not unknown[i] or has_private[i]:
                continue
            for p in (f.coords[0], f.coords[-1]):
                cx, cy = cell(p)
                touching = {
                    j
                    for dx in (-1, 0, 1)
                    for dy in (-1, 0, 1)
                    for j in ends.get((cx + dx, cy + dy), ())
                }
                if touching & dropped:
                    dropped.add(i)
                    changed = True
                    break
    kept = [f for i, f in enumerate(fences) if i not in dropped]
    removed = [f for i, f in enumerate(fences) if i in dropped]
    return kept, removed


def _inside_single_area(fences, lines, unknown, has_private, areas) -> set[int]:
    """Indexy úseků s neznámým původem ležících uvnitř jedné plochy daleko od budov.

    Skutečný plot dělí dvě různé plochy (nebo leží na hranici plochy). Čára, která má
    po obou stranách tutéž plochu zahrady/dvora/zeleně/lesa a v okolí není budova, je
    nejspíš pozůstatek zákresu (např. cesta či hranice v parku).
    """
    from shapely import STRtree
    from shapely.geometry import Point, Polygon

    def polygons(items):
        out = []
        for poly in items:
            try:
                p = Polygon(poly[0], poly[1:]).buffer(0)
            except (ValueError, IndexError):
                continue
            if not p.is_empty:
                out.append(p)
        return out

    inner_polys = polygons((areas.get("olive") or []) + (areas.get("open") or []))
    candidates = [i for i in range(len(fences)) if unknown[i] and not has_private[i]]
    if not inner_polys or not candidates:
        return set()
    tree = STRtree(inner_polys)
    buildings = polygons(areas.get("building") or [])
    btree = STRtree(buildings) if buildings else None

    def side(pt):
        return frozenset(int(j) for j in tree.query(pt) if inner_polys[int(j)].contains(pt))

    out: set[int] = set()
    for i in candidates:
        line = lines[i]
        if line.length < 1.0:
            continue
        inside = True
        for frac in (0.25, 0.5, 0.75):
            p = line.interpolate(frac, normalized=True)
            q = line.interpolate(min(frac + 0.01, 1.0), normalized=True)
            dx, dy = q.x - p.x, q.y - p.y
            norm = (dx * dx + dy * dy) ** 0.5 or 1.0
            ox, oy = -dy / norm * INNER_SIDE_OFFSET_M, dx / norm * INNER_SIDE_OFFSET_M
            left = side(Point(p.x + ox, p.y + oy))
            if not left or left != side(Point(p.x - ox, p.y - oy)):
                inside = False
                break
        if inside and btree is not None and len(btree.query(line.buffer(INNER_BUILDING_FREE_M))):
            inside = False
        if inside:
            out.add(i)
    return out


def _closed_ring_polygon(coords, tol: float = ENCLOSURE_CLOSE_TOL_M):
    """Polygon z uzavřené čáry (první a poslední bod do ``tol`` m), jinak None."""
    from shapely.geometry import Polygon

    if len(coords) < 4:
        return None
    (x0, y0), (x1, y1) = coords[0], coords[-1]
    if math.hypot(x1 - x0, y1 - y0) > tol:
        return None
    try:
        poly = Polygon(coords)
        poly = poly if poly.is_valid else poly.buffer(0)
    except ValueError:
        return None
    if poly.is_empty or poly.geom_type != "Polygon":
        return None
    if not (ENCLOSURE_MIN_AREA_M2 <= poly.area <= ENCLOSURE_MAX_AREA_M2):
        return None
    return poly


def fence_faces(lines: list[tuple[str, list[tuple[float, float]]]], *, barriers=None, max_area: float = ENCLOSURE_MAX_AREA_M2):
    """Uzavřené plochy tvořené sítí plotů (i složené z více čar spojených do „T“).

    Čáry (a volitelné ``barriers`` = další čáry, např. okraje cest) se uzlí (``unary_union``)
    a polygonizují; plochy mimo rozsah ``ENCLOSURE_MIN_AREA_M2``–``max_area`` se nepoužívají.
    """
    from shapely.geometry import LineString
    from shapely.ops import polygonize, unary_union

    geoms = [LineString(c) for _code, c in lines if len(c) >= 2]
    if not geoms:
        return []
    geoms.extend(barriers or [])
    return [
        poly
        for poly in polygonize(unary_union(geoms))
        if ENCLOSURE_MIN_AREA_M2 <= poly.area <= max_area
    ]


def drop_fences_inside_enclosures(
    lines: list[tuple[str, list[tuple[float, float]]]], *, tol: float = ENCLOSURE_CLOSE_TOL_M
) -> tuple[list[tuple[str, list[tuple[float, float]]]], int]:
    """Zahodí ploty ležící uvnitř většího oplocení: ``(ponechané, počet zahozených)``.

    Pravidla viz ``fences_inside_enclosures``.
    """
    dropped = fences_inside_enclosures(lines, tol=tol)
    return [item for i, item in enumerate(lines) if i not in dropped], len(dropped)


def fences_inside_enclosures(
    lines: list[tuple[str, list[tuple[float, float]]]], *, tol: float = ENCLOSURE_CLOSE_TOL_M
) -> set[int]:
    """Indexy plotů ležících uvnitř většího oplocení.

    1. Čára celá uvnitř jiné uzavřené čáry (vnořené oplocení, přepážka).
    2. Čára celá uvnitř uzavřené plochy sítě plotů (osamělý / vyčnívající kus),
       která neleží na hranici té plochy – dělicí plot spojující dvě strany oplocení
       je hranicí dvou ploch, ten zůstává.
    Hranici oplocení samotnou nikdy nezahazujeme.
    """
    from shapely import STRtree
    from shapely.geometry import LineString

    rings = []  # (index čáry, polygon)
    for i, (_code, coords) in enumerate(lines):
        poly = _closed_ring_polygon(coords, tol)
        if poly is not None:
            rings.append((i, poly))
    dropped: set[int] = set()
    if rings:
        tree = STRtree([p.buffer(tol) for _i, p in rings])
        for i, (_code, coords) in enumerate(lines):
            if len(coords) < 2:
                continue
            line = LineString(coords)
            own = next((p for j, p in rings if j == i), None)
            for k in tree.query(line):
                j, poly = rings[int(k)]
                if j == i or (own is not None and own.area >= poly.area):
                    continue
                if poly.buffer(tol).covers(line):
                    dropped.add(i)
                    break
    rest = [(i, item) for i, item in enumerate(lines) if i not in dropped]
    faces = fence_faces([item for _i, item in rest])
    if faces:
        ftree = STRtree(faces)
        for i, (_code, coords) in rest:
            if len(coords) < 2:
                continue
            line = LineString(coords)
            if line.length <= 0:
                continue
            for k in ftree.query(line):
                face = faces[int(k)]
                if not face.buffer(tol).covers(line):
                    continue
                on_edge = line.intersection(face.boundary.buffer(2 * tol)).length
                if on_edge < 0.5 * line.length:
                    dropped.add(i)
                    break
    return dropped


def private_enclosure_rings(
    lines: list[tuple[str, list[tuple[float, float]]]], areas: dict[str, list]
) -> list[list[tuple[float, float]]]:
    """Obrysy oplocení k vyplnění olivou (vnější obrys, S-JTSK).

    1. Plocha uzavřená samotnými ploty: vyplní se, když je široká aspoň ``PLOT_MIN_WIDTH_M`` a
       do ``ENCLOSURE_CLOSED_MAX_AREA_M2``, nebo (větší) když je aspoň z poloviny zahrada/dvůr (OPL).
    2. Parcela uzavřená plotem a okraji cest/zpevněných ploch (plot u cesty chybí): plot na
       ≥ ``PLOT_FENCE_SHARE`` obvodu, šířka ≥ ``PLOT_MIN_WIDTH_M``, plocha ≤ ``PLOT_MAX_AREA_M2``
       a uvnitř budova (hřiště a velké areály se tak nevyplní).
    Bez ploch z OPL se nevyplňuje nic.
    """
    from shapely import STRtree
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union

    xs = [x for _c, line in lines for x, _y in line]
    ys = [y for _c, line in lines for _x, y in line]
    if not xs:
        return []
    margin = 30.0
    bx = (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)

    def polygons(items):
        out = []
        for poly in items:
            px = [p[0] for p in poly[0]]
            py = [p[1] for p in poly[0]]
            if max(px) < bx[0] or min(px) > bx[2] or max(py) < bx[1] or min(py) > bx[3]:
                continue
            try:
                g = Polygon(poly[0], poly[1:])
                out.append(g if g.is_valid else g.buffer(0))
            except (ValueError, IndexError):
                continue
        return [g for g in out if not g.is_empty]

    olive = polygons(areas.get("olive") or [])
    otree = STRtree(olive) if olive else None
    out: list[Polygon] = []
    for face in fence_faces(lines):
        per = face.exterior.length
        compact = per > 0 and 2.0 * face.area / per >= PLOT_MIN_WIDTH_M and face.area <= ENCLOSURE_CLOSED_MAX_AREA_M2
        private = False
        if otree is not None:
            covered = sum(olive[int(k)].intersection(face).area for k in otree.query(face))
            private = covered / face.area >= ENCLOSURE_PRIVATE_SHARE
        if compact or private:
            out.append(face)

    buildings = polygons(areas.get("building") or [])
    if buildings:
        rings = [
            LineString(g.exterior.coords)
            for g in polygons(areas.get("public") or [])
            if g.geom_type == "Polygon"
        ]
        fence_zone = unary_union([LineString(c).buffer(PLOT_FENCE_BUFFER_M) for _k, c in lines if len(c) >= 2])
        btree = STRtree(buildings)
        for face in fence_faces(lines, barriers=rings, max_area=PLOT_MAX_AREA_M2):
            per = face.exterior.length
            if per <= 0 or 2.0 * face.area / per < PLOT_MIN_WIDTH_M:
                continue
            if face.exterior.intersection(fence_zone).length / per < PLOT_FENCE_SHARE:
                continue
            has_house = any(
                buildings[int(k)].intersection(face).area > 0.5 * buildings[int(k)].area
                for k in btree.query(face)
            )
            if not has_house:
                continue
            c = face.representative_point()
            if any(o.contains(c) for o in out):
                continue  # už vyplněno prvním pravidlem
            out.append(face)
    return [[(float(x), float(y)) for x, y in face.exterior.coords] for face in out]


def avoid_paths(
    lines: list[tuple[str, list[tuple[float, float]]]],
    paths: list[tuple[list[tuple[float, float]], float]],
    *,
    max_shift: float = AVOID_MAX_SHIFT_M,
    step: float = AVOID_STEP_M,
) -> list[tuple[str, list[tuple[float, float]]]]:
    """Odsune body plotů od os cest tak, aby bylo mezi osou cesty a plotem aspoň ``clearance`` m.

    ``paths`` = ``[(osa cesty, požadovaná vzdálenost osy plotu od osy cesty v m), …]``.
    Bod se posune po kolmici od nejbližšího bodu cesty (zůstane na své straně), nejvýš o
    ``max_shift`` m; body, které by se musely posunout víc (plot cestu kříží), se nemění.
    Posuny se vyhladí a uzavřené čáry zůstanou uzavřené.
    """
    from shapely import STRtree
    from shapely.geometry import LineString, Point

    geoms, req = [], []
    for coords, clearance in paths:
        if len(coords) >= 2 and clearance > 0:
            geoms.append(LineString(coords))
            req.append(float(clearance))
    if not geoms:
        return lines
    tree = STRtree(geoms)
    reach = max(req)

    def displacement(x: float, y: float) -> tuple[float, float]:
        pt = Point(x, y)
        best = None  # (nedostatek, dx, dy)
        for k in tree.query(pt.buffer(reach)):
            k = int(k)
            g = geoms[k]
            d = g.distance(pt)
            deficit = req[k] - d
            if deficit <= 1e-6 or deficit > max_shift:
                continue
            q = g.interpolate(g.project(pt))
            vx, vy = x - q.x, y - q.y
            n = math.hypot(vx, vy)
            if n < 1e-6:
                continue
            if best is None or deficit > best[0]:
                best = (deficit, vx / n * deficit, vy / n * deficit)
        return (best[1], best[2]) if best else (0.0, 0.0)

    out = []
    for code, coords in lines:
        if len(coords) < 2:
            out.append((code, coords))
            continue
        line = LineString(coords)
        n_seg = max(1, int(math.ceil(line.length / step)))
        pts = [tuple(line.interpolate(line.length * t / n_seg).coords[0]) for t in range(n_seg + 1)]
        pts[0], pts[-1] = tuple(coords[0]), tuple(coords[-1])
        disp = [displacement(x, y) for x, y in pts]
        if not any(dx or dy for dx, dy in disp):
            out.append((code, coords))
            continue
        for _ in range(2):  # vyhlazení posunů, krajní body zůstávají
            disp = [disp[0]] + [
                (
                    (disp[i - 1][0] + 2 * disp[i][0] + disp[i + 1][0]) / 4,
                    (disp[i - 1][1] + 2 * disp[i][1] + disp[i + 1][1]) / 4,
                )
                for i in range(1, len(disp) - 1)
            ] + [disp[-1]]
        moved = [(x + dx, y + dy) for (x, y), (dx, dy) in zip(pts, disp)]
        simple = LineString(moved).simplify(0.1)
        new = [(float(x), float(y)) for x, y in simple.coords]
        if len(new) < 2:
            out.append((code, coords))
            continue
        if math.hypot(coords[0][0] - coords[-1][0], coords[0][1] - coords[-1][1]) <= ENCLOSURE_CLOSE_TOL_M:
            new[-1] = new[0]
        out.append((code, new))
    return out
