"""Zjednodušení tvaru objektů jako „Simplify path“ v Mapperu (Ctrl+M, ``PathObject::simplify``).

Greedy: opakovaně se odebere vrchol s nejmenší cenou; cena = největší vzdálenost PŮVODNÍCH vrcholů mezi
sousedy od nové úsečky (soused–soused), takže se odchylky nesčítají. Vrchol s cenou nad tolerancí zůstane,
konce otevřené čáry se neodebírají, uzavřený prstenec si nechá aspoň 3 vrcholy.
"""

from __future__ import annotations

import collections
import heapq
import math
import re

from app.pipeline.oom_import import OomObjectPart

# Tolerance v mm mapy – stejná jako výchozí Ctrl+M v Mapperu.
SIMPLIFY_TOLERANCE_MM = 0.1
# Objekt s méně souřadnicemi se nechá beze změny.
MIN_VERTICES = 4

_FLAG_CURVE = 1
_FLAG_CLOSE = 2
_FLAG_HOLE = 16
# T-napojení: cizí konec čáry blíž než tolik µm mapy od úsečky zamkne její vrcholy.
_JOIN_UM = 10
_GRID_UM = 2000

_OBJ_RE = re.compile(
    r'(<object type="1" symbol="-?\d+"[^>]*>\s*<coords count=")(\d+)(">)([^<]*)(</coords>)'
)


def _seg_dist2(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    if ll == 0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / ll
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    qx, qy = ax + t * dx, ay + t * dy
    return (px - qx) ** 2 + (py - qy) ** 2


def simplify_indices(pts, *, closed: bool, tol: float, locked=frozenset()) -> list[int]:
    """Indexy vrcholů, které zůstanou. ``pts`` bez uzavíracího duplikátu; ``locked`` se neodebírají."""
    n = len(pts)
    min_keep = 3 if closed else 2
    if n <= min_keep:
        return list(range(n))
    prev = list(range(-1, n - 1))
    nxt = list(range(1, n + 1))
    if closed:
        prev[0] = n - 1
        nxt[n - 1] = 0
    alive = [True] * n
    tol2 = tol * tol

    def cost(i: int) -> float:
        if i in locked or (not closed and (i == 0 or i == n - 1)):
            return math.inf
        a, b = prev[i], nxt[i]
        ax, ay = pts[a][0], pts[a][1]
        bx, by = pts[b][0], pts[b][1]
        worst = 0.0
        j = (a + 1) % n
        while j != b:
            d = _seg_dist2(pts[j][0], pts[j][1], ax, ay, bx, by)
            if d > worst:
                worst = d
                if worst > tol2:
                    return math.inf
            j = (j + 1) % n
        return worst

    costs = [cost(i) for i in range(n)]
    heap = [(costs[i], i) for i in range(n) if costs[i] <= tol2]
    heapq.heapify(heap)
    left = n
    while heap and left > min_keep:
        c, i = heapq.heappop(heap)
        if not alive[i] or c != costs[i]:
            continue
        alive[i] = False
        left -= 1
        a, b = prev[i], nxt[i]
        nxt[a] = b
        prev[b] = a
        for k in (a, b):
            if alive[k]:
                costs[k] = cost(k)
                if costs[k] <= tol2:
                    heapq.heappush(heap, (costs[k], k))
    return [i for i in range(n) if alive[i]]


def simplify_ring(pts, tol: float, *, closed: bool):
    """Zjednodušení bodů (metry); uzavřený prstenec může mít duplikát na konci, vrátí se stejně."""
    pts = list(pts)
    dup = closed and len(pts) > 1 and tuple(pts[0]) == tuple(pts[-1])
    core = pts[:-1] if dup else pts
    keep = simplify_indices(core, closed=closed, tol=tol)
    out = [core[i] for i in keep]
    if dup:
        out.append(out[0])
    return out


def _parse_coords(body: str) -> list[tuple[int, int, int]]:
    out = []
    for p in body.strip().strip(";").split(";"):
        a = p.split()
        out.append((int(a[0]), int(a[1]), int(a[2]) if len(a) > 2 else 0))
    return out


def _split_parts(co):
    parts, cur = [], []
    for c in co:
        cur.append(c)
        if c[2] & (_FLAG_CLOSE | _FLAG_HOLE):
            parts.append(cur)
            cur = []
    if cur:
        parts.append(cur)
    return parts


def simplify_object_parts(
    parts: list[OomObjectPart],
    *,
    tol_mm: float = SIMPLIFY_TOLERANCE_MM,
    stats: dict | None = None,
) -> list[OomObjectPart]:
    """Zjednoduší plošné a čárové objekty (``type="1"``) v hotovém XML; souřadnice jsou v µm mapy."""
    tol = tol_mm * 1000.0
    matches = [list(_OBJ_RE.finditer(p.objects_xml)) for p in parts]
    parsed = [[_parse_coords(m.group(4)) for m in ms] for ms in matches]

    # Polohy použité ve víc objektech (sdílené hranice, napojení) a konce otevřených čar.
    owners: dict[tuple[int, int], set] = collections.defaultdict(set)
    ends: set[tuple[int, int]] = set()
    for pi, cos in enumerate(parsed):
        for oi, co in enumerate(cos):
            for x, y, _f in co:
                owners[(x, y)].add((pi, oi))
            for seg in _split_parts(co):
                if not seg[-1][2] & _FLAG_CLOSE:
                    ends.add(seg[0][:2])
                    ends.add(seg[-1][:2])
    grid: dict[tuple[int, int], list] = collections.defaultdict(list)
    for e in ends:
        grid[(e[0] // _GRID_UM, e[1] // _GRID_UM)].append(e)
    join2 = _JOIN_UM * _JOIN_UM

    before = after = 0
    result: list[OomObjectPart] = []
    for pi, part in enumerate(parts):
        pieces, last = [], 0
        for oi, m in enumerate(matches[pi]):
            co = parsed[pi][oi]
            if len(co) < MIN_VERTICES or any(f & _FLAG_CURVE for _x, _y, f in co):
                continue
            me = (pi, oi)
            new_co: list[tuple[int, int, int]] = []
            for seg in _split_parts(co):
                closed = bool(seg[-1][2] & _FLAG_CLOSE)
                ring = seg[:-1] if closed and seg[-1][:2] == seg[0][:2] else seg
                pts = [(x, y) for x, y, _f in ring]
                locked = set()
                for i, (x, y, f) in enumerate(ring):
                    if f & ~(_FLAG_CLOSE | _FLAG_HOLE) or len(owners[(x, y)]) > 1:
                        locked.add(i)
                n = len(pts)
                for i in range(n if closed else n - 1):
                    (ax, ay), (bx, by) = pts[i], pts[(i + 1) % n]
                    for gx in range(min(ax, bx) // _GRID_UM, max(ax, bx) // _GRID_UM + 1):
                        for gy in range(min(ay, by) // _GRID_UM, max(ay, by) // _GRID_UM + 1):
                            for e in grid.get((gx, gy), ()):
                                if (
                                    e != (ax, ay)
                                    and e != (bx, by)
                                    and owners[e] != {me}
                                    and _seg_dist2(e[0], e[1], ax, ay, bx, by) <= join2
                                ):
                                    locked.add(i)
                                    locked.add((i + 1) % n)
                keep = simplify_indices(pts, closed=closed, tol=tol, locked=locked)
                kept = [ring[i] for i in keep]
                if closed:
                    kept.append((kept[0][0], kept[0][1], seg[-1][2]))
                new_co.extend(kept)
            before += len(co)
            after += len(new_co)
            if len(new_co) == len(co):
                continue
            body = ";".join(f"{x} {y} {f}" if f else f"{x} {y}" for x, y, f in new_co) + ";"
            pieces.append(part.objects_xml[last : m.start()])
            pieces.append(m.group(1) + str(len(new_co)) + m.group(3) + body + m.group(5))
            last = m.end()
        pieces.append(part.objects_xml[last:])
        result.append(OomObjectPart(name=part.name, objects_xml="".join(pieces), count=part.count))
    if stats is not None:
        stats["before"] = before
        stats["after"] = after
    return result
