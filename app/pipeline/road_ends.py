"""Les a MTBO: dotažení volných konců silnic a zahození pahýlů (Petr 2026-10-27: „stačí kresbu o kousek protáhnout,
napojení pak dotáhne OOMapper“; 2026-10-28: čistý průběh obcí, vynechat zbytečné detaily).

Po ořezech (plochy, pásy silnic, rozdělená vozovka, ZABAGED proti OSM) končí část silnic pár metrů před jinou silnicí
a zbývají krátké pahýly (Barrandov, les bez DTM: 98 konců 4–15 m od jiné silnice, 140 kusů pod 2 mm).

- **Dotažení:** volný konec silnice, od kterého do ``EXTEND_MAX_MM`` mm mapy vede k jiné kreslené silnici OSM silnice (po ní),
  nebo – u čáry bez OSM opory (ZABAGED) – přímé prodloužení. Silnice s výplní jde do výplně cíle až k jeho ose: výplň je
  v sadách nad okrajovými linkami, takže Mapper ústí otevře. Černá silnice (les ``503``) končí na okraji silnice s výplní
  (přes výplň by se kreslila), na černou silnici jde k ose. Přes budovu, vodu ani zpevněnou plochu (``blockers``) se nedotahuje.
- **Pahýl:** kus silnice kratší než ``STUB_MAX_MM`` mm mapy, který jedním koncem nikam nevede (ani k ploše / budově
  z ``blockers``), se nekreslí; osamělý kus (oba konce volné) se nekreslí vždy.

Ostatní čáry (cesty, pěšiny) se nemění, jen se počítají jako napojení. Sprint se nevolá.
"""

from __future__ import annotations

import math

# Konec silnice se dotáhne k jiné silnici nejvýš o tolik mm mapy (15 m v 1:10 000, 22,5 m v 1:15 000).
EXTEND_MAX_MM = 1.5
# Kus silnice kratší než tolik mm mapy s volným koncem se nekreslí.
STUB_MAX_MM = 1.5
# Konec do tolika m od jiné čáry je napojený.
END_TOUCH_M = 0.5
# Konec leží na OSM silnici, je-li od ní do tolika m.
OSM_SUPPORT_M = 1.5
# Volný konec pahýlu do tolika m od plochy / budovy (``blockers``) vede k ní – pahýl zůstává.
STUB_TARGET_M = 1.0
# Přímé prodloužení (čára bez OSM opory) jen na silnici, kterou protne pod úhlem aspoň tolik stupňů.
RAY_MIN_ANGLE_DEG = 30.0
# Blokující plochy se pro test průchodu zmenší o tolik m (dotyk hrany nevadí).
BLOCKER_SHRINK_M = 0.3


def _is_road(code: str, preset_id: str, scale: int) -> bool:
    from app.pipeline.osm_paths import is_major_road_code

    return code == "503" or code.startswith("502") or is_major_road_code(code, preset_id, scale)


def _half_width_m(code: str, preset_id: str, scale: int, cache: dict) -> float:
    if code not in cache:
        from app.pipeline.oom_symbol_map import symbol_line_width_mm
        from app.pipeline.osm_paths import mm_to_ground_m

        mm = symbol_line_width_mm(preset_id, scale, code)
        cache[code] = mm_to_ground_m(mm, scale) / 2.0 if mm else 0.0
    return cache[code]


def tidy_road_ends(
    pieces_by_code: dict[str, list[list[tuple[float, float]]]],
    osm_roads: list[list[tuple[float, float]]],
    *,
    preset_id: str,
    scale: int,
    blockers=None,
    fixed_by_code: dict[str, list[list[tuple[float, float]]]] | None = None,
) -> dict[str, list[list[tuple[float, float]]]]:
    """Dotáhne volné konce silnic a zahodí pahýly (viz modul). Klíč = kód symbolu, hodnoty čáry v metrech (S-JTSK).

    ``fixed_by_code``: čáry kreslené jinde (ZABAGED ulice z ``oom_import``) – počítají se jako napojení a cíl dotažení,
    samy se nemění a nevrací.
    """
    try:
        from shapely import STRtree
        from shapely.geometry import LineString, Point
        from shapely.ops import substring
    except ImportError:
        return pieces_by_code
    from app.pipeline.osm_paths import is_major_road_code, mm_to_ground_m

    extend_max = mm_to_ground_m(EXTEND_MAX_MM, scale)
    stub_max = mm_to_ground_m(STUB_MAX_MM, scale)
    halves: dict[str, float] = {}
    items: list[list] = []  # [kód, souřadnice, je silnice, pevná]
    for fixed, src in ((False, pieces_by_code), (True, fixed_by_code or {})):
        for code, lines in src.items():
            road = _is_road(code, preset_id, scale)
            for ln in lines:
                if len(ln) >= 2:
                    items.append([code, [(float(x), float(y)) for x, y in ln], road, fixed])
    if not any(it[2] for it in items):
        return pieces_by_code
    geoms = [LineString(it[1]) for it in items]
    tree = STRtree(geoms)
    osm = [LineString(c) for c in osm_roads if len(c) >= 2]
    osm = [g for g in osm if g.length > 0]
    osm_tree = STRtree(osm) if osm else None
    blk = None
    if blockers is not None and not blockers.is_empty:
        from shapely.prepared import prep

        _b = blockers.buffer(-BLOCKER_SHRINK_M)
        blk = prep(_b) if not _b.is_empty else None
    blockers_prep = None
    if blockers is not None and not blockers.is_empty:
        from shapely.prepared import prep

        blockers_prep = prep(blockers.buffer(STUB_TARGET_M))

    def _touches(i: int, p) -> bool:
        for j in tree.query(p.buffer(END_TOUCH_M)):
            j = int(j)
            if j != i and geoms[j].distance(p) <= END_TOUCH_M:
                return True
        return False

    def _free_end(i: int, at_start: bool) -> bool:
        c = items[i][1]
        if c[0] == c[-1]:
            return False
        return not _touches(i, Point(c[0] if at_start else c[-1]))

    def _stop_on(i: int, target: int, dist_axis: float) -> float | None:
        """Kde má prodloužení skončit (m od konce), nebo None."""
        code, tcode = items[i][0], items[target][0]
        if is_major_road_code(tcode, preset_id, scale) and not is_major_road_code(code, preset_id, scale):
            stop = dist_axis - _half_width_m(tcode, preset_id, scale, halves)
        else:
            stop = dist_axis
        reach = dist_axis - _half_width_m(tcode, preset_id, scale, halves)  # mezera do kresby cíle
        if reach > extend_max or stop <= END_TOUCH_M:
            return None
        return stop

    def _first_target(i: int, path) -> tuple[int, float] | None:
        """První jiná silnice, kterou ``path`` (od konce čáry) protne: (index, vzdálenost po ``path``)."""
        best = None
        for j in tree.query(path):
            j = int(j)
            if j == i or not items[j][2]:
                continue
            hit = path.intersection(geoms[j])
            if hit.is_empty:
                continue
            pts = [hit] if hit.geom_type == "Point" else [g for g in getattr(hit, "geoms", []) if g.geom_type == "Point"]
            if not pts and hit.geom_type in ("LineString", "MultiLineString"):
                pts = [Point(hit.coords[0])] if hit.geom_type == "LineString" else [Point(g.coords[0]) for g in hit.geoms]
            for q in pts:
                d = path.project(q)
                if d > END_TOUCH_M and (best is None or d < best[1]):
                    best = (j, d)
        return best

    def _blocked(path) -> bool:
        return blk is not None and blk.intersects(path)

    def _extend(i: int, at_start: bool) -> list[tuple[float, float]] | None:
        c = items[i][1]
        p = Point(c[0] if at_start else c[-1])
        ln = geoms[i]
        inner = ln.interpolate(min(3.0, ln.length / 2) if at_start else max(0.0, ln.length - 3.0))
        reach_all = extend_max + max(
            (_half_width_m(it[0], preset_id, scale, halves) for it in items if it[2]), default=0.0
        )
        support = []
        if osm_tree is not None:
            support = [osm[int(k)] for k in osm_tree.query(p.buffer(OSM_SUPPORT_M))]
            support = [g for g in support if g.distance(p) <= OSM_SUPPORT_M]
        if support:
            for g in sorted(support, key=lambda g: g.distance(p)):
                s0, s_in = g.project(p), g.project(inner)
                if abs(s_in - s0) < 0.5:
                    continue  # čára jde napříč OSM silnice – není to její pokračování
                if s_in < s0:
                    path = substring(g, s0, min(g.length, s0 + reach_all))
                else:
                    path = substring(g, s0, max(0.0, s0 - reach_all))
                if path.is_empty or path.length <= END_TOUCH_M or path.geom_type != "LineString":
                    continue
                hit = _first_target(i, path)
                if hit is None:
                    continue
                stop = _stop_on(i, hit[0], hit[1])
                if stop is None:
                    continue
                ext = substring(path, 0.0, stop)
                if _blocked(ext):
                    continue
                return [(float(x), float(y)) for x, y in ext.coords]
            return None
        # Bez OSM opory (ZABAGED): přímé prodloužení ve směru konce.
        dx, dy = p.x - inner.x, p.y - inner.y
        n = math.hypot(dx, dy)
        if n < 0.01:
            return None
        ray = LineString([(p.x, p.y), (p.x + dx / n * reach_all, p.y + dy / n * reach_all)])
        hit = _first_target(i, ray)
        if hit is None:
            return None
        q = ray.interpolate(hit[1])
        tg = geoms[hit[0]]
        s = tg.project(q)
        a, b = tg.interpolate(max(0.0, s - 2.0)), tg.interpolate(min(tg.length, s + 2.0))
        tl = math.hypot(b.x - a.x, b.y - a.y)
        if tl > 0:
            cos = abs((b.x - a.x) * dx + (b.y - a.y) * dy) / (tl * n)
            if math.degrees(math.acos(min(1.0, cos))) < RAY_MIN_ANGLE_DEG:
                return None
        stop = _stop_on(i, hit[0], hit[1])
        if stop is None:
            return None
        ext = substring(ray, 0.0, stop)
        if _blocked(ext):
            return None
        return [(float(x), float(y)) for x, y in ext.coords]

    # 1. Dotažení volných konců (konce se posuzují proti původní kresbě).
    new_coords: dict[int, list[tuple[float, float]]] = {}
    for i, it in enumerate(items):
        if not it[2] or it[3]:
            continue
        c = it[1]
        for at_start in (True, False):
            if not _free_end(i, at_start):
                continue
            ext = _extend(i, at_start)
            if not ext:
                continue
            cur = new_coords.get(i, c)
            new_coords[i] = [*ext[::-1], *cur[1:]] if at_start else [*cur[:-1], *ext]
    for i, c in new_coords.items():
        items[i][1] = c
        geoms[i] = LineString(c)
    if new_coords:
        tree = STRtree(geoms)

    # 2. Pahýly.
    drop: set[int] = set()
    for i, it in enumerate(items):
        if not it[2] or it[3] or geoms[i].length >= stub_max:
            continue
        free = [at_start for at_start in (True, False) if _free_end(i, at_start)]
        if len(free) == 2:
            drop.add(i)  # osamělý kus (úlomek OSM, zbytek po ořezu) – nikam nevede
            continue
        for at_start in free:
            p = Point(it[1][0] if at_start else it[1][-1])
            if blockers_prep is not None and blockers_prep.contains(p):
                continue  # vede k ploše / budově
            drop.add(i)
    out: dict[str, list[list[tuple[float, float]]]] = {code: [] for code in pieces_by_code}
    for i, it in enumerate(items):
        if i not in drop and not it[3]:
            out[it[0]].append(it[1])
    return out
