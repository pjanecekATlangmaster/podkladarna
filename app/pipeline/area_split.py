"""Rozřezání velkých ploch vegetace – přednostně v úzkých místech (hrdlech).

OCAD při výběru / posunu obrazovky přepočítává celý objekt bod po bodu, louka
přes celou mapu s tisíci vrcholy ho brzdí a mapař s ní špatně pracuje. Plochu
nad limitem (vrcholy a/nebo m²) proto dělíme:

1. **Hrdlo** – hledáme krátkou úsečku uvnitř plochy mezi dvěma body obvodu,
   které jsou si blízko vzdušnou čarou, ale daleko podél obvodu. Kvalita řezu
   ``šířka / sqrt(plocha menšího dílu)``: úzké hrdlo mezi dvěma velkými díly
   má malé číslo. Řez se přijme jen pod ``NECK_MAX_RATIO`` a jen když menší díl
   není střep (``min_piece``). Kromě řezu jednoho obrysu (obvod–obvod, i okraj
   díry–tentýž okraj) umí i dvojici řezů přes díru (obvod–díra–obvod), typicky
   louka zúžená lesním ostrůvkem.
2. **Rovný řez** (fallback) – když hrdlo není: přímka kolmo na delší stranu
   obálky v 30–70 % délky, vybere se poloha s nejkratším řezem (pod 50 %
   délky se tak i bez hrdla řeže v užším místě) a bez drobných úlomků.

Rekurzivně, dokud každý díl nesplní limity (nebo ``MAX_PIECES``). Díly vznikají
polygonizací obvodu + řezů (resp. průnikem s polorovinou), takže součet ploch
= původní plocha, díry zůstávají a geometrie je platná. Souřadnice jsou
projekční (metry) – prahy v m / m².

Jen pro výplně bez obrysu (vegetace), jinak by byly řezy v mapě vidět.
"""

from __future__ import annotations

import math

import numpy as np

Ring = list[tuple[float, float]]

# Hrdlo: šířka řezu / sqrt(plocha menšího dílu). 0.5 ≈ „otvor je nejvýš poloviční
# oproti velikosti dílu, který odděluje“ – u činky ze čtverců 100 m s krčkem 20 m
# je to 0.2, středový řez obdélníku 400×100 m (žádné hrdlo) 0.71 → rovný řez.
NECK_MAX_RATIO = 0.5
# Konce řezu na témže obrysu musí být podél obvodu ≥ 2.5× dál než vzdušnou čarou
# (jinak jde o sousední body schodovitého okraje, ne o protilehlé strany).
NECK_PERIMETER_RATIO = 2.5
# Menší díl musí mít aspoň max(500 m², 10 % z min(plocha, limit plochy)).
NECK_MIN_PIECE_M2 = 500.0
NECK_MIN_PIECE_FRAC = 0.1
# Kolik nejlepších kandidátů se přesně ověří polygonizací (každý ~ms).
NECK_EXACT_CANDIDATES = 8
# Max. bodů obvodu pro hledání párů (větší obrysy se ředí po k-tém bodu).
_MAX_SEARCH_POINTS = 3000
# Max. ověřených kandidátů „covers“ (úsečka uvnitř plochy) na jedno hledání.
_MAX_COVER_CHECKS = 20000
# Kandidátních spojnic na jednu dvojici obrysů (obvod–díra apod.) před testem.
_PAIR_GROUP_POOL = 40
# Pojistka: max. dílů z jednoho polygonu a hloubka rekurze.
MAX_PIECES = 256
_MAX_DEPTH = 24
FALLBACK_POSITIONS = (0.5, 0.4, 0.6, 0.3, 0.7)


def split_area(
    ring: Ring,
    holes: list[Ring],
    max_vertices: int,
    *,
    max_area: float | None = None,
) -> list[tuple[Ring, list[Ring]]]:
    """Plochu nad limity rozdělí na díly (``[(obrys, [díry])]``); jinak vrátí beze změny."""
    n = len(ring) + sum(len(h) for h in holes)
    if n <= max_vertices and (max_area is None or _ring_area(ring) <= max_area):
        return [(ring, holes)]
    from shapely.geometry import Polygon
    from shapely.validation import make_valid

    poly = Polygon(ring, holes)
    if not poly.is_valid:
        poly = make_valid(poly)
    elif not _exceeds(poly, max_vertices, max_area):
        return [(ring, holes)]
    pieces = _split_all(poly, max_vertices, max_area)
    return [
        (list(p.exterior.coords), [list(i.coords) for i in p.interiors]) for p in pieces
    ]


def _ring_area(ring: Ring) -> float:
    if len(ring) < 3:
        return 0.0
    a = np.asarray(ring, dtype=float)
    x, y = a[:, 0], a[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y)))


def _n_vertices(poly) -> int:
    return len(poly.exterior.coords) + sum(len(i.coords) for i in poly.interiors)


def _exceeds(poly, max_vertices: int, max_area: float | None) -> bool:
    return _n_vertices(poly) > max_vertices or (
        max_area is not None and poly.area > max_area
    )


def polygons_of(geom) -> list:
    """Jen plošné kusy (průnik s polorovinou může vrátit i linie/body na hraně)."""
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom] if geom.area > 0 else []
    if hasattr(geom, "geoms"):
        return [p for g in geom.geoms for p in polygons_of(g)]
    return []


def _split_all(poly, max_vertices: int, max_area: float | None) -> list:
    work = [(p, 0) for p in polygons_of(poly)]
    out: list = []
    while work:
        p, depth = work.pop()
        if (
            not _exceeds(p, max_vertices, max_area)
            or depth >= _MAX_DEPTH
            or len(out) + len(work) + 1 >= MAX_PIECES
        ):
            out.append(p)
            continue
        parts = _neck_split(p, max_area) or _straight_split(p, max_area)
        if not parts or len(parts) < 2:
            out.append(p)
            continue
        work.extend((q, depth + 1) for q in parts)
    return [_drop_collinear(p) for p in out]


def _drop_collinear(p):
    """Odstraní body přidané ``segmentize`` (leží přesně na přímce) – bez změny tvaru."""
    q = p.simplify(1e-6, preserve_topology=True)
    if q.geom_type != "Polygon" or q.is_empty or not q.is_valid:
        return p
    if abs(q.area - p.area) > 1e-9 * max(p.area, 1.0) + 1e-6:
        return p
    return q


# ---------------------------------------------------------------------------
# Hrdla
# ---------------------------------------------------------------------------


def _min_piece_area(area: float, max_area: float | None) -> float:
    ref = min(area, max_area) if max_area else area
    return max(NECK_MIN_PIECE_M2, NECK_MIN_PIECE_FRAC * ref)


def _neck_split(p, max_area: float | None) -> list | None:
    import shapely

    area = p.area
    min_piece = _min_piece_area(area, max_area)
    if area < 2.0 * min_piece:
        return None
    # Nejširší smysluplné hrdlo: menší díl má nejvýš polovinu plochy.
    w_max = NECK_MAX_RATIO * math.sqrt(area / 2.0)
    # Dlouhé rovné hrany (okraj mapy) bez bodů → hrdlo by tam nešlo najít.
    step = min(max(math.sqrt(area) / 30.0, 5.0), 50.0)
    dp = shapely.segmentize(p, step)
    if not dp.is_valid or dp.geom_type != "Polygon":
        dp = p
    shapely.prepare(dp)

    rings = [np.asarray(dp.exterior.coords, dtype=float)[:-1]] + [
        np.asarray(i.coords, dtype=float)[:-1] for i in dp.interiors
    ]
    rings = [r for r in rings if len(r) >= 3]
    # Globální pole všech obrysů; prefixové součty po obrysech: délka podél
    # obvodu a „shoelace“ (plocha smyčky oblouk + tětiva v O(1)).
    g_xy = np.concatenate(rings)
    sizes = np.array([len(r) for r in rings])
    off = np.r_[0, np.cumsum(sizes)[:-1]]
    coff = off + np.arange(len(rings))  # posun v prefixových polích (n+1 na obrys)
    cum_len, cum_cross, perim = [], [], []
    for r in rings:
        nxt = np.roll(r, -1, axis=0)
        seg = np.hypot(*(nxt - r).T)
        cross = r[:, 0] * nxt[:, 1] - nxt[:, 0] * r[:, 1]
        cum_len.append(np.r_[0.0, np.cumsum(seg)])
        cum_cross.append(np.r_[0.0, np.cumsum(cross)])
        perim.append(float(seg.sum()))
    g_len = np.concatenate(cum_len)
    g_cross = np.concatenate(cum_cross)
    perim_a = np.array(perim)

    stride = max(1, math.ceil(len(g_xy) / _MAX_SEARCH_POINTS))
    rid = np.concatenate(
        [np.full(len(range(0, n, stride)), k) for k, n in enumerate(sizes)]
    )
    idx = np.concatenate([np.arange(0, n, stride) for n in sizes])
    if len(idx) < 4:
        return None
    xy = g_xy[off[rid] + idx]
    s_pos = g_len[coff[rid] + idx]

    pts = shapely.points(xy)
    a, b = shapely.STRtree(pts).query(pts, predicate="dwithin", distance=w_max)
    keep = a < b
    a, b = a[keep], b[keep]
    if not len(a):
        return None
    d = np.hypot(*(xy[a] - xy[b]).T)
    ok = d > 1e-9
    a, b, d = a[ok], b[ok], d[ok]
    inside = _InsideTest(dp, rings)
    candidates: list[tuple[float, list]] = []  # (šířka, řezy)

    # --- řez jednoho obrysu (konce na témže obrysu) ---
    same = rid[a] == rid[b]
    sa, sb, sd = a[same], b[same], d[same]
    if len(sa):
        rk = rid[sa]
        per = perim_a[rk]
        arc = np.abs(s_pos[sa] - s_pos[sb])
        arc_min = np.minimum(arc, per - arc)
        m = arc_min >= NECK_PERIMETER_RATIO * sd
        sa, sb, sd, rk = sa[m], sb[m], sd[m], rk[m]
    if len(sa):
        ia, ib = idx[sa], idx[sb]
        lo, hi = np.minimum(ia, ib), np.maximum(ia, ib)
        c_lo = g_cross[coff[rk] + lo]
        c_hi = g_cross[coff[rk] + hi]
        c_n = g_cross[coff[rk] + sizes[rk]]
        p_lo = g_xy[off[rk] + lo]
        p_hi = g_xy[off[rk] + hi]
        chord = p_hi[:, 0] * p_lo[:, 1] - p_lo[:, 0] * p_hi[:, 1]
        s1 = c_hi - c_lo + chord
        s2 = c_n - s1
        small = 0.5 * np.minimum(np.abs(s1), np.abs(s2))
        est = sd / np.sqrt(np.maximum(small, 1e-9))
        m = (small >= min_piece) & (est <= NECK_MAX_RATIO)
        order = np.argsort(est[m], kind="stable")
        ca, cb, cd = sa[m][order], sb[m][order], sd[m][order]
        for i in _first_inside(inside, xy, ca, cb, cd, NECK_EXACT_CANDIDATES):
            candidates.append((float(cd[i]), [(xy[ca[i]], xy[cb[i]])]))

    # --- dvojice řezů obvod–díra–obvod ---
    cross_m = ~same
    xa, xb, xd = a[cross_m], b[cross_m], d[cross_m]
    if len(xa):
        candidates.extend(
            _ring_pair_candidates(inside, xy, rid, s_pos, perim, xa, xb, xd, w_max, step)
        )

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    best: tuple[float, list] | None = None
    for width, cuts in candidates[: 2 * NECK_EXACT_CANDIDATES]:
        pieces = _apply_cuts(dp, cuts)
        if not pieces:
            continue
        smallest = min(q.area for q in pieces)
        if smallest < min_piece:
            continue
        score = width / math.sqrt(smallest)
        if score <= NECK_MAX_RATIO and (best is None or score < best[0]):
            best = (score, pieces)
    return best[1] if best else None


class _InsideTest:
    """Rychlý test „úsečka leží uvnitř plochy“ pro tisíce kandidátů.

    ``covers`` na polygonu s tisíci vrcholy a desítkami děr stojí ~50 µs na
    úsečku; tady stačí: úsečka nekříží žádnou hranu obvodu (STRtree hran)
    a její střed je uvnitř. Průchod přesně vrcholem obvodu to nepozná – nevadí,
    řez se pak jen hůř ohodnotí; díly z polygonizace jsou správné vždy.
    """

    def __init__(self, dp, rings) -> None:
        import shapely

        starts = np.concatenate(rings)
        ends = np.concatenate([np.roll(r, -1, axis=0) for r in rings])
        self._dp = dp
        self._tree = shapely.STRtree(
            shapely.linestrings(np.stack([starts, ends], axis=1))
        )

    def __call__(self, p: np.ndarray, q: np.ndarray) -> np.ndarray:
        import shapely

        segs = shapely.linestrings(np.stack([p, q], axis=1))
        ok = shapely.contains_xy(self._dp, (p[:, 0] + q[:, 0]) / 2, (p[:, 1] + q[:, 1]) / 2)
        if ok.any():
            hit, _ = self._tree.query(segs[ok], predicate="crosses")
            sub = np.nonzero(ok)[0]
            ok[sub[np.unique(hit)]] = False
        return ok


def _first_inside(inside, xy, ca, cb, cd, limit: int) -> list[int]:
    """Indexy prvních ``limit`` různých úseček (v pořadí), které leží uvnitř plochy."""
    chosen: list[int] = []
    checked = 0
    chunk = 64
    while checked < len(ca) and checked < _MAX_COVER_CHECKS and len(chosen) < limit:
        sl = slice(checked, min(checked + chunk, len(ca)))
        ok = inside(xy[ca[sl]], xy[cb[sl]])
        for off in np.nonzero(ok)[0]:
            i = checked + int(off)
            if any(_near_cut(xy, ca, cb, cd, i, j) for j in chosen):
                continue
            chosen.append(i)
            if len(chosen) >= limit:
                break
        checked = sl.stop
        chunk = min(chunk * 2, 4096)
    return chosen


def _near_cut(xy, ca, cb, cd, i: int, j: int) -> bool:
    """Řez i je skoro stejný jako j (oba konce blízko) → neověřovat znovu."""
    tol = max(cd[i], cd[j])
    pi, qi, pj, qj = xy[ca[i]], xy[cb[i]], xy[ca[j]], xy[cb[j]]
    straight = np.hypot(*(pi - pj)) < tol and np.hypot(*(qi - qj)) < tol
    swapped = np.hypot(*(pi - qj)) < tol and np.hypot(*(qi - pj)) < tol
    return bool(straight or swapped)


def _ring_pair_candidates(inside, xy, rid, s_pos, perim, xa, xb, xd, w_max, step):
    """Dva řezy mezi obvodem a touž dírou – teprve spolu oddělí díl."""
    # Normalizace: první konec na obrysu s menším id.
    swap = rid[xa] > rid[xb]
    xa, xb = np.where(swap, xb, xa), np.where(swap, xa, xb)
    # Jen nejbližší protějšek každého bodu na druhém obrysu (z obou stran) –
    # jinak by tisíce párů v okruhu w_max stály tisíce testů „uvnitř plochy“.
    keep = np.zeros(len(xa), dtype=bool)
    for src, other in ((xa, xb), (xb, xa)):
        order = np.lexsort((xd, rid[other], src))
        first = np.ones(len(order), dtype=bool)
        first[1:] = (src[order][1:] != src[order][:-1]) | (
            rid[other][order][1:] != rid[other][order][:-1]
        )
        keep[order[first]] = True
    # Dvojice řezů má součet šířek ≤ w_max → jedna spojnice ≤ w_max / 2.
    # Jen obvod–díra: díra–díra by oddělila jen pruh mezi dvěma ostrůvky
    # (obvykle střep) a párů děr je kvadraticky mnoho.
    keep &= (xd <= w_max / 2.0) & (rid[xa] == 0)
    xa, xb, xd = xa[keep], xb[keep], xd[keep]
    # Na každou dvojici obrysů jen nejkratších _PAIR_GROUP_POOL spojnic.
    gkey = rid[xa].astype(np.int64) * (int(rid.max()) + 1) + rid[xb]
    order = np.lexsort((xd, gkey))
    gs = gkey[order]
    start = np.r_[0, np.nonzero(gs[1:] != gs[:-1])[0] + 1]
    rank = np.arange(len(gs)) - np.repeat(start, np.diff(np.r_[start, len(gs)]))
    order = order[rank < _PAIR_GROUP_POOL]
    xa, xb, xd = xa[order], xb[order], xd[order]
    order = np.argsort(xd, kind="stable")
    xa, xb, xd = xa[order], xb[order], xd[order]

    per_group: dict[tuple[int, int], list[int]] = {}
    checked = 0
    chunk = 256
    while checked < len(xa) and checked < _MAX_COVER_CHECKS:
        sl = slice(checked, min(checked + chunk, len(xa)))
        ok = inside(xy[xa[sl]], xy[xb[sl]])
        for off in np.nonzero(ok)[0]:
            i = checked + int(off)
            key = (int(rid[xa[i]]), int(rid[xb[i]]))
            group = per_group.setdefault(key, [])
            if len(group) >= 6:
                continue
            # Různá místa: konce aspoň krok + šířka od už vybraných spojnic.
            if any(
                abs(s_pos[xa[i]] - s_pos[xa[j]]) < step + xd[j]
                and abs(s_pos[xb[i]] - s_pos[xb[j]]) < step + xd[j]
                for j in group
            ):
                continue
            group.append(i)
        checked = sl.stop
        chunk = min(chunk * 2, 4096)

    def arc_between(k: int, s1: float, s2: float) -> float:
        arc = abs(s1 - s2)
        return min(arc, perim[k] - arc)

    out: list[tuple[float, list]] = []
    for (ka, kb), group in per_group.items():
        best: tuple[float, list] | None = None
        for gi, i in enumerate(group):
            for j in group[gi + 1 :]:
                width = float(xd[i] + xd[j])
                if width > w_max:
                    continue
                arc_a = arc_between(ka, s_pos[xa[i]], s_pos[xa[j]])
                arc_b = arc_between(kb, s_pos[xb[i]], s_pos[xb[j]])
                if min(arc_a, arc_b) < step:
                    continue
                if max(arc_a, arc_b) < NECK_PERIMETER_RATIO * width:
                    continue
                if best is None or width < best[0]:
                    best = (
                        width,
                        [(xy[xa[i]], xy[xb[i]]), (xy[xa[j]], xy[xb[j]])],
                    )
        if best:
            out.append(best)
    out.sort(key=lambda c: c[0])
    return out[:NECK_EXACT_CANDIDATES]


def _apply_cuts(dp, cuts) -> list | None:
    """Rozdělí plochu úsečkami (polygonizace obvodu + řezů); ``None`` když nerozdělí."""
    import shapely

    lines = shapely.union_all(
        [dp.boundary, shapely.multilinestrings([np.asarray(c) for c in cuts])]
    )
    faces = shapely.get_parts(shapely.polygonize(shapely.get_parts(lines)))
    if not len(faces):
        return None
    faces = [f for f in faces if f.area > 0]
    probes = shapely.point_on_surface(faces)
    inside = shapely.contains(dp, probes)
    pieces = [f for f, ok in zip(faces, inside) if ok]
    if len(pieces) < 2:
        return None
    if abs(sum(q.area for q in pieces) - dp.area) > 1e-7 * dp.area:
        return None
    return pieces


# ---------------------------------------------------------------------------
# Rovný řez (fallback)
# ---------------------------------------------------------------------------


def _straight_split(p, max_area: float | None = None) -> list | None:
    """Přímka kolmo na delší stranu obálky – poloha s nejkratším řezem, bez úlomků."""
    from shapely.geometry import LineString, box

    minx, miny, maxx, maxy = p.bounds
    area = p.area
    vertical_cut = (maxx - minx) >= (maxy - miny)
    fragment = 0.5 * _min_piece_area(area, max_area)
    best: tuple[float, list] | None = None
    for t in FALLBACK_POSITIONS:
        if vertical_cut:
            c = minx + t * (maxx - minx)
            line = LineString([(c, miny - 1), (c, maxy + 1)])
            halves = (box(minx - 1, miny - 1, c, maxy + 1), box(c, miny - 1, maxx + 1, maxy + 1))
        else:
            c = miny + t * (maxy - miny)
            line = LineString([(minx - 1, c), (maxx + 1, c)])
            halves = (box(minx - 1, miny - 1, maxx + 1, c), box(minx - 1, c, maxx + 1, maxy + 1))
        parts = _merge_fragments(
            [q for h in halves for q in polygons_of(p.intersection(h))], fragment
        )
        if len(parts) < 2:
            continue
        cost = p.intersection(line).length * (1.0 + abs(t - 0.5))
        if best is None or cost < best[0]:
            best = (cost, parts)
    return best[1] if best else None


def _merge_fragments(parts: list, fragment: float) -> list:
    """Úlomky (výběžek useknutý přímkou) připojí k sousednímu dílu přes řez."""
    parts = sorted(parts, key=lambda q: q.area, reverse=True)
    while len(parts) > 1 and parts[-1].area < fragment:
        small = parts.pop()
        shared = [small.boundary.intersection(q.boundary).length for q in parts]
        k = int(np.argmax(shared))
        if shared[k] <= 0:
            parts.append(small)  # nesousedí s ničím – nechat (nemělo by nastat)
            break
        merged = polygons_of(small.union(parts[k]))
        if len(merged) != 1:
            parts.append(small)
            break
        parts[k] = merged[0]
        parts.sort(key=lambda q: q.area, reverse=True)
    return parts