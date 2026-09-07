"""Spojí KP srázové čárky (~3 m tick) do linií, plošné skalní pole do polygonů.

Karttapullautin zapisuje každý sráz jako samostatnou úsečku kolmo na spád
(délka ~2,9 m, rastr buněk 3 m). V OOM je to tisíce objektů. Tady se souosé
sousední čárky řetězí na lomenou čáru (201/104).

U volby skála se navíc hledá plošné pole: čárky se hodí do rastru s krokem KP
a plocha 210 vznikne jen tam, kde je útvar 2D (buňka obklopená ze všech stran).
Stěna je pás 1–2 buněk, žádnou takovou buňku nemá, a zůstane linií. Obrys se
obtahuje po hranách buněk, ne konvexní obálkou – zálivy a díry zůstanou.
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
# KP umí do jedné buňky 4,4 m nasypat i 1800 čárek přes sebe. Zahodit ty, co
# se liší o < 1 m a < 8°, nic viditelného nestojí (1 m = 0,1 mm na 1:10 000).
DEDUP_POS_PER_M = 1.0
DEDUP_ANG_PER_RAD = 8.0

# Rastr plošných skal – nativní krok KP, ať obrys nepřeteče přes realitu.
ROCK_CELL_M = 3.0
# Jedna zbloudilá čárka buňku neudělá skalní.
ROCK_MIN_TICKS_PER_CELL = 2
# Souvislých jader (buňka se všemi 4 sousedy) pod tímhle → nechat liniím.
ROCK_MIN_CORE_CELLS = 2
MIN_ROCK_AREA_M2 = 80.0
ROCK_SIMPLIFY_M = 1.2

# KP nedává výšku srázu, takže jistotu detekce z jedné čárky poznat nejde: každá
# je jen jedna dvojice bodů nad prahem, ne samostatné pozorování. Co poznat jde,
# je jestli je útvar dost dlouhý na nakreslení – ISOM má pro čáru minimum kolem
# 0,6 mm na mapě. Kratší nálezy by mapař stejně nekreslil a jen zaplevelí OOM.
MIN_LINE_MM = 0.6

_Tick = tuple[tuple[float, float], tuple[float, float]]
_Cell = tuple[int, int]


@dataclass(frozen=True)
class MergedCliffs:
    lines: list[list[tuple[float, float]]]
    polygons: list[list[tuple[float, float]]]


def min_line_length_m(scale: int) -> float:
    """Nejkratší sráz, který má na dané měřítko smysl kreslit."""
    return MIN_LINE_MM * scale / 1000.0


def merge_cliff_ticks(
    ticks: list[_Tick],
    *,
    as_polygons: bool = False,
    min_line_m: float = 0.0,
) -> MergedCliffs:
    """Vrátí lomené čáry a volitelně polygony. Vstup: úsečky v S-JTSK metrech."""
    remaining = _dedup_ticks(ticks)
    polygons: list[list[tuple[float, float]]] = []
    if as_polygons:
        remaining, polygons = _rock_area_polygons(remaining)
    if not remaining:
        return MergedCliffs([], polygons)
    chains = _chain_ticks(remaining)
    polylines = [_chain_polyline(remaining, chain) for chain in chains]
    polylines = [
        _simplify_polyline(pts, SIMPLIFY_M) for pts in polylines if len(pts) >= 2
    ]
    if min_line_m > 0:
        polylines = [pts for pts in polylines if _polyline_length(pts) >= min_line_m]
    return MergedCliffs(polylines, polygons)


def _polyline_length(pts: list[tuple[float, float]]) -> float:
    return sum(
        math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
        for i in range(1, len(pts))
    )


def _dedup_ticks(ticks: list[_Tick]) -> list[_Tick]:
    seen: set[tuple[int, int, int]] = set()
    out: list[_Tick] = []
    for a, b in ticks:
        if math.hypot(b[0] - a[0], b[1] - a[1]) < 0.25:
            continue
        mx = 0.5 * (a[0] + b[0])
        my = 0.5 * (a[1] + b[1])
        ang = math.atan2(b[1] - a[1], b[0] - a[0]) % math.pi
        key = (
            round(mx * DEDUP_POS_PER_M),
            round(my * DEDUP_POS_PER_M),
            round(ang * DEDUP_ANG_PER_RAD),
        )
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


def _chain_ticks(ticks: list[_Tick]) -> list[list[int]]:
    n = len(ticks)
    cell = JOIN_GAP_M
    gap_sq = JOIN_GAP_M * JOIN_GAP_M
    mids = [_mid(t) for t in ticks]
    tans = [_tangent(t) for t in ticks]
    keys = [(int(x // cell), int(y // cell)) for x, y in mids]
    # Spotřebovaná čárka z mřížky rovnou zmizí – ve skalním poli má buňka stovky
    # čárek a procházet pořád dokola ty už použité stálo většinu času.
    grid: dict[_Cell, set[int]] = defaultdict(set)
    for i, key in enumerate(keys):
        grid[key].add(i)
    used = bytearray(n)

    def best_next(i: int) -> int | None:
        mx, my = mids[i]
        tx, ty = tans[i]
        gi, gj = keys[i]
        best_j: int | None = None
        best_d = gap_sq
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                bucket = grid.get((gi + di, gj + dj))
                if not bucket:
                    continue
                for j in bucket:
                    jx, jy = mids[j]
                    dx, dy = jx - mx, jy - my
                    d_sq = dx * dx + dy * dy
                    if d_sq < 0.0225 or d_sq > best_d:
                        continue
                    ux, uy = tans[j]
                    if abs(tx * ux + ty * uy) < JOIN_ANGLE_COS:
                        continue
                    if abs(tx * dx + ty * dy) < CHAIN_DIR_COS * math.sqrt(d_sq):
                        continue
                    best_d = d_sq
                    best_j = j
        return best_j

    def take(i: int) -> None:
        used[i] = 1
        grid[keys[i]].discard(i)

    chains: list[list[int]] = []
    for start in range(n):
        if used[start]:
            continue
        take(start)
        chain = [start]

        def grow() -> None:
            while True:
                nxt = best_next(chain[-1])
                if nxt is None:
                    return
                take(nxt)
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


def _neighbors4(cell: _Cell) -> tuple[_Cell, _Cell, _Cell, _Cell]:
    i, j = cell
    return ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1))


def _rock_area_polygons(
    ticks: list[_Tick],
) -> tuple[list[_Tick], list[list[tuple[float, float]]]]:
    """Plošné skalní pole → polygon podle rastru; stěny a řídké čárky nechá liniím."""
    cell = ROCK_CELL_M
    buckets: dict[_Cell, list[int]] = defaultdict(list)
    for i, tick in enumerate(ticks):
        x, y = _mid(tick)
        buckets[(int(math.floor(x / cell)), int(math.floor(y / cell)))].append(i)
    rocky = {
        key for key, ids in buckets.items() if len(ids) >= ROCK_MIN_TICKS_PER_CELL
    }
    # Jádro = buňka obklopená skálou ze všech stran; pás stěny žádné nemá.
    core = {c for c in rocky if all(nb in rocky for nb in _neighbors4(c))}
    if len(core) < ROCK_MIN_CORE_CELLS:
        return ticks, []

    polygons: list[list[tuple[float, float]]] = []
    used_cells: set[_Cell] = set()
    for comp in _cell_components(core):
        if len(comp) < ROCK_MIN_CORE_CELLS:
            continue
        # Zpět o jednu buňku: plocha kryje jen mohutnou část, výběžky zůstanou linií.
        area_cells = set(comp)
        for c in comp:
            area_cells.update(nb for nb in _neighbors4(c) if nb in rocky)
        kept: list[list[tuple[float, float]]] = []
        for ring in _trace_cell_rings(area_cells, cell):
            if _signed_ring_area(ring) <= 0:
                continue  # díra uvnitř plochy
            simple = _simplify_ring(ring, ROCK_SIMPLIFY_M)
            if len(simple) >= 3 and _ring_area(simple) >= MIN_ROCK_AREA_M2:
                kept.append(simple)
        if kept:
            polygons.extend(kept)
            used_cells |= area_cells

    if not used_cells:
        return ticks, []
    # Čárky těsně za hranou plochy jen lemují její obrys – symbol 210 už je nese
    # a jinak z nich vznikne rám stovek třímetrových pahýlů.
    for i, j in tuple(used_cells):
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                nb = (i + di, j + dj)
                if nb in rocky:
                    used_cells.add(nb)
    remaining = [
        tick
        for tick in ticks
        if (
            int(math.floor(_mid(tick)[0] / cell)),
            int(math.floor(_mid(tick)[1] / cell)),
        )
        not in used_cells
    ]
    return remaining, polygons


def _cell_components(cells: set[_Cell]) -> list[list[_Cell]]:
    seen: set[_Cell] = set()
    out: list[list[_Cell]] = []
    for seed in cells:
        if seed in seen:
            continue
        seen.add(seed)
        stack = [seed]
        comp: list[_Cell] = []
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in _neighbors4(cur):
                if nb in cells and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        out.append(comp)
    return out


def _trace_cell_rings(
    cells: set[_Cell], cell_m: float
) -> list[list[tuple[float, float]]]:
    """Obtáhne obrys po hranách buněk. Vnější prstenec CCW, díra CW."""
    edges: dict[_Cell, list[_Cell]] = defaultdict(list)
    for i, j in cells:
        # Buňka (i, j) = čtverec [i, i+1] × [j, j+1] v rozích rastru.
        if (i, j - 1) not in cells:
            edges[(i, j)].append((i + 1, j))
        if (i + 1, j) not in cells:
            edges[(i + 1, j)].append((i + 1, j + 1))
        if (i, j + 1) not in cells:
            edges[(i + 1, j + 1)].append((i, j + 1))
        if (i - 1, j) not in cells:
            edges[(i, j + 1)].append((i, j))

    rings: list[list[tuple[float, float]]] = []
    while edges:
        start = next(iter(edges))
        ring_cells = [start]
        cur = start
        incoming: _Cell | None = None
        while True:
            outgoing = edges.get(cur)
            if not outgoing:
                break
            nxt = _pick_next(outgoing, cur, incoming)
            if not outgoing:
                del edges[cur]
            incoming = (nxt[0] - cur[0], nxt[1] - cur[1])
            if nxt == start:
                break
            ring_cells.append(nxt)
            cur = nxt
        if len(ring_cells) >= 4:
            rings.append([(x * cell_m, y * cell_m) for x, y in ring_cells])
    return rings


def _pick_next(outgoing: list[_Cell], cur: _Cell, incoming: _Cell | None) -> _Cell:
    """V rohu, kde se dva laloky dotýkají, zahne doleva – laloky zůstanou oddělené."""
    if incoming is None or len(outgoing) == 1:
        return outgoing.pop()
    ix, iy = incoming

    def rank(nxt: _Cell) -> int:
        dx, dy = nxt[0] - cur[0], nxt[1] - cur[1]
        cross = ix * dy - iy * dx
        if cross > 0:
            return 0
        if ix * dx + iy * dy > 0:
            return 1
        return 2 if cross < 0 else 3

    best = min(range(len(outgoing)), key=lambda k: rank(outgoing[k]))
    return outgoing.pop(best)


def _simplify_ring(
    ring: list[tuple[float, float]], tol: float
) -> list[tuple[float, float]]:
    """Douglas–Peucker na uzavřeném prstenci – rozdělí ho na dvě půlky."""
    if len(ring) <= 4 or tol <= 0:
        return ring
    ax, ay = ring[0]
    far = max(
        range(1, len(ring)),
        key=lambda i: (ring[i][0] - ax) ** 2 + (ring[i][1] - ay) ** 2,
    )
    first = _simplify_polyline(ring[: far + 1], tol)
    second = _simplify_polyline(ring[far:] + [ring[0]], tol)
    out = first[:-1] + second[:-1]
    return out if len(out) >= 3 else ring


def _signed_ring_area(pts: list[tuple[float, float]]) -> float:
    if len(pts) < 3:
        return 0.0
    s = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return s * 0.5


def _ring_area(pts: list[tuple[float, float]]) -> float:
    return abs(_signed_ring_area(pts))
