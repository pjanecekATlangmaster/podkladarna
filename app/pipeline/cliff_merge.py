"""Spojí KP srázové čárky (~3 m tick) do linií, husté shluky do polygonů.

Karttapullautin zapisuje každý sráz jako samostatnou úsečku kolmo na spád
(délka ~2,9 m, rastr buněk 3 m). V OOM je to tisíce objektů. Tady se souosé
sousední čárky řetězí na lomenou čáru (201/104); u volby skála se kompaktní
shluky krátkých čárek převedou na plochu 210 (kamenitý povrch).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

# Mezera středů sousedních KP čárek (buňka 3 m, úhlopříčka ~4,2 m).
JOIN_GAP_M = 4.4
# Max. odchylka směru čárky (nesměrové).
JOIN_ANGLE_COS = math.cos(math.radians(32))
# Spojnice středů musí jít zhruba podél stěny, ne kolmo na sousední sráz.
CHAIN_DIR_COS = 0.45
SIMPLIFY_M = 0.55
# Buňka 10×10 m s ≥8 čárkami (u volby skála) → plocha 210.
DENSE_CELL_M = 10.0
DENSE_MIN_TICKS = 8
MIN_POLY_AREA_M2 = 45.0

_Tick = tuple[tuple[float, float], tuple[float, float]]


@dataclass(frozen=True)
class MergedCliffs:
    lines: list[list[tuple[float, float]]]
    polygons: list[list[tuple[float, float]]]


def merge_cliff_ticks(
    ticks: list[_Tick],
    *,
    as_polygons: bool = False,
) -> MergedCliffs:
    """Vrátí lomené čáry a volitelně polygony. Vstup: úsečky v S-JTSK metrech."""
    remaining = _dedup_ticks(ticks)
    polygons: list[list[tuple[float, float]]] = []
    if as_polygons:
        remaining, polygons = _split_dense_clusters(remaining)
    if not remaining:
        return MergedCliffs([], polygons)
    chains = _chain_ticks(remaining)
    polylines = [_chain_polyline(remaining, chain) for chain in chains]
    polylines = [_simplify_polyline(pts, SIMPLIFY_M) for pts in polylines if len(pts) >= 2]
    return MergedCliffs(polylines, polygons)


def _dedup_ticks(ticks: list[_Tick]) -> list[_Tick]:
    seen: set[tuple[int, int, int]] = set()
    out: list[_Tick] = []
    for a, b in ticks:
        if math.hypot(b[0] - a[0], b[1] - a[1]) < 0.25:
            continue
        mx = 0.5 * (a[0] + b[0])
        my = 0.5 * (a[1] + b[1])
        ang = math.atan2(b[1] - a[1], b[0] - a[0]) % math.pi
        key = (round(mx * 5), round(my * 5), round(ang * 20))
        if key in seen:
            continue
        seen.add(key)
        out.append((a, b))
    return out


def _mid(tick: _Tick) -> tuple[float, float]:
    (ax, ay), (bx, by) = tick
    return (0.5 * (ax + bx), 0.5 * (ay + by))


def _tangent(tick: _Tick) -> tuple[float, float]:
    (ax, ay), (bx, by) = tick
    dx, dy = bx - ax, by - ay
    n = math.hypot(dx, dy)
    if n < 1e-9:
        return (0.0, 0.0)
    return (dx / n, dy / n)


def _undirected_dot(ax: float, ay: float, bx: float, by: float) -> float:
    return abs(ax * bx + ay * by)


def _chain_ticks(ticks: list[_Tick]) -> list[list[int]]:
    n = len(ticks)
    cell = JOIN_GAP_M
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    mids = [_mid(t) for t in ticks]
    tans = [_tangent(t) for t in ticks]
    for i, (x, y) in enumerate(mids):
        grid[(int(math.floor(x / cell)), int(math.floor(y / cell)))].append(i)

    def nearby(i: int) -> list[int]:
        x, y = mids[i]
        gi, gj = int(math.floor(x / cell)), int(math.floor(y / cell))
        found: list[int] = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                found.extend(grid.get((gi + di, gj + dj), ()))
        return found

    def best_next(i: int, used: list[bool], avoid: set[int]) -> int | None:
        mx, my = mids[i]
        tx, ty = tans[i]
        best_j: int | None = None
        best_d = JOIN_GAP_M
        for j in nearby(i):
            if used[j] or j == i or j in avoid:
                continue
            dx, dy = mids[j][0] - mx, mids[j][1] - my
            dist = math.hypot(dx, dy)
            if dist < 0.15 or dist > best_d:
                continue
            ux, uy = tans[j]
            if _undirected_dot(tx, ty, ux, uy) < JOIN_ANGLE_COS:
                continue
            nx, ny = (dx / dist, dy / dist)
            if _undirected_dot(tx, ty, nx, ny) < CHAIN_DIR_COS:
                continue
            best_d = dist
            best_j = j
        return best_j

    used = [False] * n
    chains: list[list[int]] = []
    for start in range(n):
        if used[start]:
            continue
        used[start] = True
        chain = [start]

        def grow() -> None:
            while True:
                nxt = best_next(chain[-1], used, set(chain))
                if nxt is None:
                    return
                used[nxt] = True
                chain.append(nxt)

        grow()
        chain.reverse()
        grow()
        chains.append(chain)
    return chains


def _chain_polyline(ticks: list[_Tick], chain: list[int]) -> list[tuple[float, float]]:
    if len(chain) == 1:
        a, b = ticks[chain[0]]
        return [a, b]
    mids = [_mid(ticks[i]) for i in chain]
    first = ticks[chain[0]]
    second = mids[1]
    start = (
        first[0]
        if math.hypot(first[0][0] - second[0], first[0][1] - second[1])
        >= math.hypot(first[1][0] - second[0], first[1][1] - second[1])
        else first[1]
    )
    last = ticks[chain[-1]]
    prev = mids[-2]
    end = (
        last[0]
        if math.hypot(last[0][0] - prev[0], last[0][1] - prev[1])
        >= math.hypot(last[1][0] - prev[0], last[1][1] - prev[1])
        else last[1]
    )
    pts = [start]
    for mid in mids:
        if math.hypot(mid[0] - pts[-1][0], mid[1] - pts[-1][1]) >= 0.2:
            pts.append(mid)
    if math.hypot(end[0] - pts[-1][0], end[1] - pts[-1][1]) >= 0.2:
        pts.append(end)
    return pts


def _point_seg_dist(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom < 1e-18:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _simplify_polyline(
    pts: list[tuple[float, float]], tol: float
) -> list[tuple[float, float]]:
    if len(pts) <= 2 or tol <= 0:
        return pts
    max_d = -1.0
    idx = 0
    ax, ay = pts[0]
    bx, by = pts[-1]
    for i in range(1, len(pts) - 1):
        d = _point_seg_dist(pts[i][0], pts[i][1], ax, ay, bx, by)
        if d > max_d:
            max_d = d
            idx = i
    if max_d > tol:
        left = _simplify_polyline(pts[: idx + 1], tol)
        right = _simplify_polyline(pts[idx:], tol)
        return left[:-1] + right
    return [pts[0], pts[-1]]


def _split_dense_clusters(
    ticks: list[_Tick],
) -> tuple[list[_Tick], list[list[tuple[float, float]]]]:
    """Buňky s mnoha čárkami (skalní suť) → konvexní obálka; zbytek ke spojení do linií."""
    if len(ticks) < DENSE_MIN_TICKS:
        return ticks, []
    cell = DENSE_CELL_M
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, tick in enumerate(ticks):
        x, y = _mid(tick)
        buckets[(int(math.floor(x / cell)), int(math.floor(y / cell)))].append(i)
    dense_cells = {key for key, ids in buckets.items() if len(ids) >= DENSE_MIN_TICKS}
    if not dense_cells:
        return ticks, []
    extra: set[tuple[int, int]] = set()
    for gi, gj in dense_cells:
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                nb = (gi + di, gj + dj)
                if nb not in dense_cells and len(buckets.get(nb, ())) >= 2:
                    extra.add(nb)
    dense_cells |= extra

    seen: set[tuple[int, int]] = set()
    used: set[int] = set()
    polygons: list[list[tuple[float, float]]] = []
    for seed in dense_cells:
        if seed in seen:
            continue
        stack = [seed]
        seen.add(seed)
        comp: list[tuple[int, int]] = []
        while stack:
            cur = stack.pop()
            comp.append(cur)
            ci, cj = cur
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (ci + di, cj + dj)
                if nb in dense_cells and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        pts: list[tuple[float, float]] = []
        for key in comp:
            for i in buckets.get(key, ()):
                used.add(i)
                a, b = ticks[i]
                pts.extend((a, b))
        hull = _convex_hull(pts)
        if len(hull) >= 3 and _ring_area(hull) >= MIN_POLY_AREA_M2:
            polygons.append(hull)
        else:
            used.difference_update(
                i for key in comp for i in buckets.get(key, ())
            )
    remaining = [t for i, t in enumerate(ticks) if i not in used]
    return remaining, polygons


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    uniq = sorted(set(points))
    if len(uniq) <= 2:
        return uniq

    def cross(
        o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
    ) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in uniq:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(uniq):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _ring_area(pts: list[tuple[float, float]]) -> float:
    if len(pts) < 3:
        return 0.0
    s = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5
