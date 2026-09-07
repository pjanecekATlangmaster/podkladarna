"""Ořez geometrie na obdélník objednané mapy.

Karttapullautin běží na širším výřezu, než uživatel objednal – kvůli chybám na
okraji heightmapy se ořez padduje a při pádu se opakuje na celých listech SM5.
Jeho výstup proto přetéká daleko za mapu a na hraně LiDARových dat generuje
artefakty. Bez ořezu se do OOM dostanou stovky objektů, které tam nepatří.

Čáry se řežou po segmentech (Liang–Barsky) a rozpadají se na části uvnitř,
plochy jedním průchodem Sutherland–Hodgman. Obdélník je konvexní, takže na to
oboje stačí a není potřeba shapely.
"""

from __future__ import annotations

Bounds = tuple[float, float, float, float]
_Point = tuple[float, float]


def expand(bounds: Bounds, margin_m: float) -> Bounds:
    xmin, ymin, xmax, ymax = bounds
    return (xmin - margin_m, ymin - margin_m, xmax + margin_m, ymax + margin_m)


def point_inside(x: float, y: float, bounds: Bounds) -> bool:
    xmin, ymin, xmax, ymax = bounds
    return xmin <= x <= xmax and ymin <= y <= ymax


def clip_polyline(pts: list[_Point], bounds: Bounds) -> list[list[_Point]]:
    """Rozřeže čáru na souvislé části ležící uvnitř obdélníku."""
    if len(pts) < 2:
        return [list(pts)] if pts and point_inside(pts[0][0], pts[0][1], bounds) else []

    out: list[list[_Point]] = []
    cur: list[_Point] = []
    for i in range(1, len(pts)):
        seg = _clip_segment(pts[i - 1], pts[i], bounds)
        if seg is None:
            if len(cur) >= 2:
                out.append(cur)
            cur = []
            continue
        a, b = seg
        if cur and _same(cur[-1], a):
            cur.append(b)
        else:
            if len(cur) >= 2:
                out.append(cur)
            cur = [a, b]
    if len(cur) >= 2:
        out.append(cur)
    return out


def clip_ring(pts: list[_Point], bounds: Bounds) -> list[_Point]:
    """Ořeže plochu; vrátí prázdný seznam, když z ní uvnitř nic nezbude."""
    if len(pts) < 3:
        return []
    xmin, ymin, xmax, ymax = bounds
    poly = list(pts)
    if _same(poly[0], poly[-1]):
        poly.pop()
    for inside, cut in (
        (lambda p: p[0] >= xmin, lambda a, b: _cut_x(a, b, xmin)),
        (lambda p: p[0] <= xmax, lambda a, b: _cut_x(a, b, xmax)),
        (lambda p: p[1] >= ymin, lambda a, b: _cut_y(a, b, ymin)),
        (lambda p: p[1] <= ymax, lambda a, b: _cut_y(a, b, ymax)),
    ):
        poly = _clip_half(poly, inside, cut)
        if len(poly) < 3:
            return []
    return poly


def _clip_half(poly: list[_Point], inside, cut) -> list[_Point]:
    out: list[_Point] = []
    for i, cur in enumerate(poly):
        prev = poly[i - 1]
        cur_in, prev_in = inside(cur), inside(prev)
        if cur_in:
            if not prev_in:
                out.append(cut(prev, cur))
            out.append(cur)
        elif prev_in:
            out.append(cut(prev, cur))
    return out


def _cut_x(a: _Point, b: _Point, x: float) -> _Point:
    dx = b[0] - a[0]
    t = 0.0 if dx == 0 else (x - a[0]) / dx
    return (x, a[1] + t * (b[1] - a[1]))


def _cut_y(a: _Point, b: _Point, y: float) -> _Point:
    dy = b[1] - a[1]
    t = 0.0 if dy == 0 else (y - a[1]) / dy
    return (a[0] + t * (b[0] - a[0]), y)


def _clip_segment(a: _Point, b: _Point, bounds: Bounds) -> tuple[_Point, _Point] | None:
    xmin, ymin, xmax, ymax = bounds
    x0, y0 = a
    dx, dy = b[0] - x0, b[1] - y0
    t0, t1 = 0.0, 1.0
    for p, q in (
        (-dx, x0 - xmin),
        (dx, xmax - x0),
        (-dy, y0 - ymin),
        (dy, ymax - y0),
    ):
        if p == 0:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return None
            t0 = max(t0, r)
        else:
            if r < t0:
                return None
            t1 = min(t1, r)
    if t1 <= t0:
        return None
    return (
        (x0 + t0 * dx, y0 + t0 * dy),
        (x0 + t1 * dx, y0 + t1 * dy),
    )


def _same(a: _Point, b: _Point) -> bool:
    return abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9
