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
CACHE_SUFFIX = ".plot.json"


@dataclass(frozen=True)
class Fence:
    coords: tuple[tuple[float, float], ...]  # S-JTSK (x, y)
    druh: str  # DruhPlotu
    level: int  # UrovenUmisteniObjektuZPS
    boundary: bool  # HraniceJinehoObjektu (plot je zároveň hranicí jiného objektu)

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
                Fence(tuple((x, y) for x, y in r["c"]), r["d"], r["l"], bool(r["b"])) for r in rows
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
            )
        )
    if use_cache:
        try:
            cache.write_text(
                json.dumps(
                    [
                        {"c": [list(p) for p in f.coords], "d": f.druh, "l": f.level, "b": f.boundary}
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
