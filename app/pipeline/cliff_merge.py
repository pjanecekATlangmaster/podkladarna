"""Spojí srázové čárky (~3 m tick) do linií, plošné skalní masy do polygonů.

Vstup (KP i DEM) jsou krátké úsečky kolmo na spád. Souosé sousední čárky se
řetězí na lomenou čáru (201/104).

U skal (201) se hledá plošná masa: buffer+dissolve ticků, morfologické
otevření (tenké stěny pryč) a vyhlazení. Obrys je plynulý prstenec footprintu,
ne zigzag přes konce ticků. Stěny a řídké pásy zůstanou liniemi 201.

Zemní srázy (104): zamotané / smyčkové linie raději nekreslit
(``reject_tangled``); delší minimální délka než u skály.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

# Mezera středů sousedních KP čárek – trochu volnější než buňka 3 m, ať se
# sousední úsečky slepí do jedné dlouhé stěny místo mraku krátkých ticků.
JOIN_GAP_M = 6.0
# Max. odchylka směru čárky (nesměrové).
JOIN_ANGLE_COS = math.cos(math.radians(40))
# Spojnice středů musí jít zhruba podél stěny, ne kolmo na sousední sráz.
CHAIN_DIR_COS = 0.35
SIMPLIFY_M = 0.55
# KP umí do jedné buňky 4,4 m nasypat i 1800 čárek přes sebe. Zahodit ty, co
# se liší o < 1 m a < 8°, nic viditelného nestojí (1 m = 0,1 mm na 1:10 000).
DEDUP_POS_PER_M = 1.0
DEDUP_ANG_PER_RAD = 8.0

# Plošná skála: buffer ticků → uzavření mezer → otevření tenkých stěn →
# stažení k mase → vyhlazení. Parametry laděné proti syntetickým stěnám/polím
# i DEM kandidátům (job 21caf0ddcec2).
ROCK_BUFFER_M = 3.0
ROCK_CLOSE_M = 1.8
ROCK_OPEN_M = 5.0
ROCK_SHRINK_M = 1.6
ROCK_SMOOTH_M = 1.5
ROCK_SIMPLIFY_M = 2.0
MIN_ROCK_AREA_M2 = 80.0
MIN_ROCK_WIDTH_M = 9.5
MAX_ROCK_ASPECT = 3.6
# Halo kolem plochy: čárky na okraji už nekreslit jako 201 (obrys nese plocha).
ROCK_TICK_HALO_M = 3.0

# Legacy rastr (fallback bez shapely + testy obtahu buněk).
ROCK_CELL_M = 3.0
ROCK_MIN_TICKS_PER_CELL = 2
ROCK_MIN_CORE_CELLS = 2

# Min. délka na mapě: skála ~1,2 mm, zem ~1,8 mm (raději nic než krátký šum).
# 1:10 000 → skála 12 m, zem 18 m; 1:4000 → 4,8 m / 7,2 m.
MIN_LINE_MM_ROCK = 1.2
MIN_LINE_MM_EARTH = 1.8
# Zpětná kompatibilita (testy / starší volání).
MIN_LINE_MM = MIN_LINE_MM_ROCK

# Zemní sráz: po hrubém zjednodušení path/chord a zatáčky – nad tím raději nekreslit.
# (Surový řetěz DEM ticků je zubatý i u rovné stěny, proto nejdřív simplify.)
BANK_SHAPE_SIMPLIFY_M = 4.0
MAX_BANK_SINUOSITY = 2.45
MAX_BANK_TURN_DEG = 200.0


_Tick = tuple[tuple[float, float], tuple[float, float]]
_Cell = tuple[int, int]


@dataclass(frozen=True)
class MergedCliffs:
    lines: list[list[tuple[float, float]]]
    polygons: list[list[tuple[float, float]]]


def min_line_length_m(scale: int, *, earth: bool = False) -> float:
    """Nejkratší sráz, který má na dané měřítko smysl kreslit."""
    mm = MIN_LINE_MM_EARTH if earth else MIN_LINE_MM_ROCK
    return mm * float(scale) / 1000.0


def merge_cliff_ticks(
    ticks: list[_Tick],
    *,
    as_polygons: bool = False,
    min_line_m: float = 0.0,
    reject_tangled: bool = False,
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
    if reject_tangled:
        polylines = [pts for pts in polylines if polyline_is_simple_bank(pts)]
    if min_line_m > 0:
        polylines = [pts for pts in polylines if _polyline_length(pts) >= min_line_m]
    return MergedCliffs(polylines, polygons)


def polyline_is_simple_bank(pts: list[tuple[float, float]]) -> bool:
    """True = zhruba rovný nebo mírně zatáčející sráz; False = uzel / smyčka.

    Raději nekreslit než zamotanou skupinku 104. Neříká nic o terénní pravdě.
    Hodnotí se hrubě zjednodušená linie – surový řetěz DEM ticků je zubatý i
    u rovné stěny.
    """
    if len(pts) < 2:
        return False
    length = _polyline_length(pts)
    if length < 1e-6:
        return False
    chord = math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1])
    if chord < 1e-3 or chord < 0.25 * length and chord < 8.0:
        return False  # uzavřená / téměř uzavřená smyčka
    if _polyline_self_intersects(pts):
        return False
    shape = _simplify_polyline(pts, BANK_SHAPE_SIMPLIFY_M)
    if len(shape) < 2:
        return False
    shape_len = _polyline_length(shape)
    shape_chord = math.hypot(shape[-1][0] - shape[0][0], shape[-1][1] - shape[0][1])
    if shape_chord < 1e-3:
        return False
    if shape_len / shape_chord > MAX_BANK_SINUOSITY:
        return False
    if _total_turning_deg(shape) > MAX_BANK_TURN_DEG:
        return False
    return True

def _total_turning_deg(pts: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(1, len(pts) - 1):
        ax, ay = pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]
        bx, by = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
        na, nb = math.hypot(ax, ay), math.hypot(bx, by)
        if na < 1e-9 or nb < 1e-9:
            continue
        cos = max(-1.0, min(1.0, (ax * bx + ay * by) / (na * nb)))
        total += math.degrees(math.acos(cos))
    return total


def _polyline_self_intersects(pts: list[tuple[float, float]]) -> bool:
    """Proper self-crossing of an open polyline (shared vertices OK)."""
    n = len(pts)
    if n < 4:
        return False
    for i in range(n - 1):
        a, b = pts[i], pts[i + 1]
        for j in range(i + 2, n - 1):
            if i == 0 and j == n - 2:
                continue
            if _segments_intersect(a, b, pts[j], pts[j + 1]):
                return True
    return False


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
    """Plošná skalní masa → plynulý polygon; stěny nechá liniím.

    Footprint = buffer+dissolve ticků (ne řetězení konců). Morfologické otevření
    sežere tenké stěny; MRR šířka/aspect odfiltruje zbytky pásů.
    """
    polygons = _rock_footprint_rings(ticks)
    if not polygons:
        # Bez shapely / prázdný výsledek: starý rastr jader (KP hustá pole).
        return _rock_area_polygons_cells(ticks)
    remaining = _ticks_outside_rings(ticks, polygons, halo_m=ROCK_TICK_HALO_M)
    return remaining, polygons


def _rock_footprint_rings(ticks: list[_Tick]) -> list[list[tuple[float, float]]]:
    """Vyhlazený footprint z bufferovaných ticků. [] když shapely chybí."""
    if not ticks:
        return []
    try:
        from shapely import make_valid
        from shapely.geometry import LineString
        from shapely.ops import unary_union
    except ImportError:
        return []

    segs = [LineString([a, b]) for a, b in ticks]
    try:
        geom = unary_union(segs).buffer(ROCK_BUFFER_M, join_style=1, cap_style=1)
        geom = geom.buffer(ROCK_CLOSE_M, join_style=1).buffer(-ROCK_CLOSE_M, join_style=1)
        # Tenké stěny (1D) zmizí; kompaktní masa zůstane.
        geom = geom.buffer(-ROCK_OPEN_M, join_style=1).buffer(ROCK_OPEN_M, join_style=1)
        if ROCK_SHRINK_M > 0:
            geom = geom.buffer(-ROCK_SHRINK_M, join_style=1)
        if ROCK_SMOOTH_M > 0 and not geom.is_empty:
            geom = geom.buffer(ROCK_SMOOTH_M, join_style=1).buffer(
                -ROCK_SMOOTH_M, join_style=1
            )
        geom = make_valid(geom)
    except Exception:
        return []

    if geom is None or geom.is_empty:
        return []

    parts = []
    if geom.geom_type == "Polygon":
        parts = [geom]
    elif geom.geom_type == "MultiPolygon":
        parts = list(geom.geoms)
    else:
        try:
            parts = [g for g in geom.geoms if g.geom_type == "Polygon"]
        except Exception:
            return []

    rings: list[list[tuple[float, float]]] = []
    for poly in parts:
        if poly.is_empty or poly.area < MIN_ROCK_AREA_M2:
            continue
        try:
            poly = poly.simplify(ROCK_SIMPLIFY_M, preserve_topology=True)
        except Exception:
            pass
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        if poly.area < MIN_ROCK_AREA_M2:
            continue
        width, length = _mrr_width_length(poly)
        if width < MIN_ROCK_WIDTH_M:
            continue
        if length / max(width, 1e-6) > MAX_ROCK_ASPECT and width < 14.0:
            continue
        coords = [(float(x), float(y)) for x, y in poly.exterior.coords]
        if len(coords) >= 2 and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) >= 3 and _ring_is_simple(coords):
            rings.append(coords)
        elif len(coords) >= 3:
            hull = _convex_hull_ring(coords)
            if len(hull) >= 3 and _ring_area(hull) >= MIN_ROCK_AREA_M2:
                rings.append(hull)
    return rings


def _mrr_width_length(poly) -> tuple[float, float]:
    """Šířka a délka minimum rotated rectangle (shapely Polygon)."""
    try:
        mrr = poly.minimum_rotated_rectangle
        coords = list(mrr.exterior.coords)
    except Exception:
        minx, miny, maxx, maxy = poly.bounds
        w, h = maxx - minx, maxy - miny
        return min(w, h), max(w, h)
    sides = [
        math.hypot(coords[i][0] - coords[i + 1][0], coords[i][1] - coords[i + 1][1])
        for i in range(4)
    ]
    return min(sides), max(sides)


def _ticks_outside_rings(
    ticks: list[_Tick],
    rings: list[list[tuple[float, float]]],
    *,
    halo_m: float,
) -> list[_Tick]:
    """Zahodí čárky uvnitř / na okraji plochy (halo), ať nezůstane rám 201."""
    try:
        from shapely.geometry import Point, Polygon
        from shapely.ops import unary_union
    except ImportError:
        return ticks
    polys = []
    for ring in rings:
        if len(ring) < 3:
            continue
        try:
            p = Polygon(ring)
            if halo_m > 0:
                p = p.buffer(halo_m)
            if not p.is_empty:
                polys.append(p)
        except Exception:
            continue
    if not polys:
        return ticks
    mask = unary_union(polys)
    out: list[_Tick] = []
    for tick in ticks:
        mx, my = _mid(tick)
        try:
            if mask.contains(Point(mx, my)):
                continue
        except Exception:
            pass
        out.append(tick)
    return out


def _rock_area_polygons_cells(
    ticks: list[_Tick],
) -> tuple[list[_Tick], list[list[tuple[float, float]]]]:
    """Fallback: plošné jádro v rastru 3 m (bez shapely / řídký footprint)."""
    cell = ROCK_CELL_M
    buckets: dict[_Cell, list[int]] = defaultdict(list)
    for i, tick in enumerate(ticks):
        x, y = _mid(tick)
        buckets[(int(math.floor(x / cell)), int(math.floor(y / cell)))].append(i)
    rocky = {
        key for key, ids in buckets.items() if len(ids) >= ROCK_MIN_TICKS_PER_CELL
    }
    core = {c for c in rocky if all(nb in rocky for nb in _neighbors4(c))}
    if len(core) < ROCK_MIN_CORE_CELLS:
        return ticks, []

    polygons: list[list[tuple[float, float]]] = []
    used_cells: set[_Cell] = set()
    for comp in _cell_components(core):
        if len(comp) < ROCK_MIN_CORE_CELLS:
            continue
        area_cells = set(comp)
        for c in comp:
            area_cells.update(nb for nb in _neighbors4(c) if nb in rocky)
        kept: list[list[tuple[float, float]]] = []
        for ring in _rock_outer_rings(area_cells, cell):
            if len(ring) >= 3 and _ring_area(ring) >= MIN_ROCK_AREA_M2:
                kept.append(ring)
        if kept:
            polygons.extend(kept)
            used_cells |= area_cells

    if not used_cells:
        return ticks, []
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


def _rock_outer_rings(
    cells: set[_Cell], cell_m: float
) -> list[list[tuple[float, float]]]:
    """Vnější obrys buněk (fallback). Preferuje union čtverců přes shapely."""
    rings = _rings_from_cell_union(cells, cell_m)
    if rings is not None:
        return rings
    out: list[list[tuple[float, float]]] = []
    for ring in _trace_cell_rings(cells, cell_m):
        if _signed_ring_area(ring) <= 0:
            continue
        simple = _simplify_ring_safe(ring, ROCK_SIMPLIFY_M)
        if len(simple) >= 3 and _ring_is_simple(simple):
            out.append(simple)
    return out


def _rings_from_cell_union(
    cells: set[_Cell], cell_m: float
) -> list[list[tuple[float, float]]] | None:
    """Union čtverců buněk → vnější prstence. None = shapely není k dispozici."""
    if not cells:
        return []
    try:
        from shapely import make_valid
        from shapely.geometry import box
        from shapely.ops import unary_union
    except ImportError:
        return None

    polys = [
        box(i * cell_m, j * cell_m, (i + 1) * cell_m, (j + 1) * cell_m)
        for i, j in cells
    ]
    geom = unary_union(polys)
    pad = cell_m * 0.45
    try:
        geom = geom.buffer(pad, join_style=1).buffer(-pad, join_style=1)
    except Exception:
        pass
    try:
        geom = make_valid(geom)
    except Exception:
        pass
    if geom is None or geom.is_empty:
        return []

    parts = []
    if geom.geom_type == "Polygon":
        parts = [geom]
    elif geom.geom_type == "MultiPolygon":
        parts = list(geom.geoms)
    else:
        try:
            parts = [g for g in geom.geoms if g.geom_type == "Polygon"]
        except Exception:
            return []

    rings: list[list[tuple[float, float]]] = []
    for poly in parts:
        if poly.is_empty or poly.area < MIN_ROCK_AREA_M2 * 0.5:
            continue
        try:
            poly = poly.simplify(ROCK_SIMPLIFY_M, preserve_topology=True)
        except Exception:
            pass
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        coords = [(float(x), float(y)) for x, y in poly.exterior.coords]
        if len(coords) >= 2 and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) >= 3 and _ring_is_simple(coords):
            rings.append(coords)
        elif len(coords) >= 3:
            hull = _convex_hull_ring(coords)
            if len(hull) >= 3:
                rings.append(hull)
    return rings


def _simplify_ring_safe(
    ring: list[tuple[float, float]], tol: float
) -> list[tuple[float, float]]:
    """Zjednoduší prstenec; při self-intersect vrátí původní nebo konvexní obálku."""
    simple = _simplify_ring(ring, tol)
    if _ring_is_simple(simple):
        return simple
    if _ring_is_simple(ring):
        return ring
    hull = _convex_hull_ring(ring)
    return hull if len(hull) >= 3 else ring


def _ring_is_simple(pts: list[tuple[float, float]]) -> bool:
    """True, když se nekříží nesousední hrany (včetně uzavření prstence)."""
    n = len(pts)
    if n < 3:
        return False
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for j in range(i + 1, n):
            if (j + 1) % n == i or (i + 1) % n == j:
                continue
            c, d = pts[j], pts[(j + 1) % n]
            if _segments_intersect(a, b, c, d):
                return False
    return True


def _segments_intersect(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    """Proper or improper intersection of open segments ab and cd (shared vertex OK)."""

    def orient(
        p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]
    ) -> int:
        v = (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])
        if abs(v) < 1e-12:
            return 0
        return 1 if v > 0 else 2

    def on_seg(
        p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]
    ) -> bool:
        return (
            min(p[0], r[0]) - 1e-9 <= q[0] <= max(p[0], r[0]) + 1e-9
            and min(p[1], r[1]) - 1e-9 <= q[1] <= max(p[1], r[1]) + 1e-9
        )

    # Shared endpoint of adjacent ring edges is not a crossing.
    if a == c or a == d or b == c or b == d:
        return False
    o1, o2 = orient(a, b, c), orient(a, b, d)
    o3, o4 = orient(c, d, a), orient(c, d, b)
    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and on_seg(a, c, b):
        return True
    if o2 == 0 and on_seg(a, d, b):
        return True
    if o3 == 0 and on_seg(c, a, d):
        return True
    if o4 == 0 and on_seg(c, b, d):
        return True
    return False


def _convex_hull_ring(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Monotone chain – nouzový jednoduchý vnější prstenec."""
    uniq = sorted(set(pts))
    if len(uniq) <= 2:
        return list(uniq)

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


def polyline_to_strip_ring(
    pts: list[tuple[float, float]],
    half_width_m: float = 1.5,
) -> list[tuple[float, float]] | None:
    """Úzký uzavřený prstenec kolem linie – pro kreslení srázu symbolem plochy (206)."""
    if len(pts) < 2 or half_width_m <= 0:
        return None
    left: list[tuple[float, float]] = []
    right: list[tuple[float, float]] = []
    n = len(pts)
    for i, (x, y) in enumerate(pts):
        if i == 0:
            dx, dy = pts[1][0] - x, pts[1][1] - y
        elif i == n - 1:
            dx, dy = x - pts[i - 1][0], y - pts[i - 1][1]
        else:
            dx, dy = pts[i + 1][0] - pts[i - 1][0], pts[i + 1][1] - pts[i - 1][1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        nx, ny = -dy / length, dx / length
        left.append((x + nx * half_width_m, y + ny * half_width_m))
        right.append((x - nx * half_width_m, y - ny * half_width_m))
    if len(left) < 2:
        return None
    ring = left + list(reversed(right))
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    if len(ring) < 4:
        return None
    # Zigzagová střednice → pás se kříží; vezmi obálku, ať OOM nekreslí smyčky.
    body = ring[:-1]
    if not _ring_is_simple(body):
        hull = _convex_hull_ring(body)
        if len(hull) < 3:
            return None
        if hull[0] != hull[-1]:
            hull = hull + [hull[0]]
        return hull
    return ring
