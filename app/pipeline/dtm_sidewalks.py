"""Plochy z DTM kraje (ZPS_OPL: chodníky, silnice, budovy, schodiště, parkoviště …) sjednocené s OSM.

Načtení typů ploch (úroveň 0), jejich sloučení s OSM / ZABAGED a geometrické pomocné funkce
(ořez o silnice, střední osa schodiště, přeřazení chybně zařazených ploch). Souhrn viz DEV.md.

DTM kreslí chodníky jako plochy (detail sídlišť, dlažby), OSM jako čáry (aktuálnější, ale
hrubší). Postup: DTM plochy (úroveň 0) se vyčistí a sloučí; čáry chodníků z OSM / ZABAGED se
o DTM plochy (+ ``OSM_COVER_M``) ořežou; zbytky delší než ``OSM_MIN_REMAINDER_M`` (to, co DTM
nezná) se rozšíří na šířku chodníku a sloučí s DTM do jedné plochy. Tvar má přednost z DTM.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.pipeline.jvf_reader import iter_records

SIDEWALK_TYPES = ("ChodnikPlocha",)
CACHE_SUFFIX = ".sidewalk1.json"
# Silnice: provozní plocha pozemní komunikace a manipulační plocha (sprint 501.20, těžký provoz).
ROAD_TYPES = ("ProvozniPlochaPozemniKomunikacePlocha", "ManipulacniPlochaPlocha")
ROAD_CACHE_SUFFIX = ".road1.json"
BUILDING_TYPES = ("BudovaPlocha",)
# Parkoviště a odstavné plochy (sprint 501.0, samostatná část).
# Udržovaná zeleň: z OSM zpevněných ploch (parkoviště, hřiště …) se vystřihne (DTM má přednost).
GREEN_TYPES = ("UdrzovanaPlochaZelenePlocha",)
GREEN_CACHE_SUFFIX = ".green1.json"
PARKING_TYPES = ("ParkovisteOdstavnaPlochaPlocha",)
PARKING_CACHE_SUFFIX = ".park1.json"
# Škvíry užší než tato hodnota (m) mezi chodníkem a silnicí se zacelí (hranice je jedna čára).
ROAD_EDGE_GAP_M = 0.5
# MTBO koridor silnice (silnice + přilehlé chodníky jedním polygonem): pruh zeleně mezi silnicí a chodníkem
# do ``CORRIDOR_GAP_M`` m se přeskočí, chodník se bere do ``CORRIDOR_NEAR_M`` m od silnice, chodník se
# sdíleným provozem (kolo povoleno) se nesloučí (okolí ``CORRIDOR_BIKE_PROTECT_M`` m od jeho čáry).
# Koridor se kreslí jen tam, kde je širší než ``CORRIDOR_MIN_WIDTH_MM`` mm mapy a delší než
# ``CORRIDOR_MIN_LEN_M`` m (plochou i délkou kusu – krátký uzel u rozbočky se nekreslí); užší silnice zůstávají čárou 502.x.
CORRIDOR_GAP_M = 3.0
CORRIDOR_NEAR_M = 8.0
CORRIDOR_BIKE_PROTECT_M = 1.5
# Parkoviště přiléhající k silnici (do ``CORRIDOR_PARKING_NEAR_M`` m od ní) patří ke koridoru: bývá mezi
# silnicí a chodníkem. Chodník se pak bere do ``CORRIDOR_NEAR_M`` m od silnice nebo takového parkoviště.
CORRIDOR_PARKING_NEAR_M = 12.0
# Koridor vzniká jen kolem skutečné jízdní plochy široké aspoň ``CORRIDOR_MIN_ROADWAY_M`` m (užší vedlejší
# silnice zůstávají čárou 502.x, jinak by se k nim přilepily chodníčky ke vchodům) a výsledek se vyhladí
# (výběžky užší než 2 × ``CORRIDOR_SMOOTH_M`` m se odříznou).
CORRIDOR_MIN_ROADWAY_M = 5.0
CORRIDOR_SMOOTH_M = 1.5
CORRIDOR_MIN_WIDTH_MM = 0.9
CORRIDOR_MIN_LEN_M = 50.0
BUILDING_CACHE_SUFFIX = ".bld1.json"
# Schody, rampy, zápraží u vchodů: v DTM bez atributu druhu, kreslí se jako zpevněná plocha 501.0.
SMALL_PAVED_TYPES = ("StavbaProZpevneniPovrchuPlocha",)
SMALL_PAVED_CACHE_SUFFIX = ".stavba1.json"
STAIRS_TYPES = ("SchodistePlocha",)
STAIRS_CACHE_SUFFIX = ".stairs1.json"
# Schody kratší než toto (m) se nekreslí; schodiště z DTM se do ``STAIRS_ZONE_M`` m rozšíří a
# OSM čára schodů se v něm nekreslí (schodiště kreslí DTM).
STAIRS_MIN_LEN_M = 1.0
STAIRS_ZONE_M = 0.5
# Cesta, která končí v zpevněné ploše, se ukončí přesně na její hraně; cesta, která k ploše nedosáhne
# o méně než ``EDGE_SNAP_M`` m, se k hraně prodlouží.
EDGE_SNAP_M = 1.5
# Les a MTBO (1:7500–1:15000): schodiště z DTM kratší než toto (m) se nekreslí.
STAIRS_MIN_LEN_NONSPRINT_M = 3.0
# Směr schodů se bere z nejbližší cesty do této vzdálenosti (m); osa se na každé straně prodlouží.
STAIRS_REF_NEAR_M = 3.0
# Schodiště protáhlejší než tento poměr stran se kreslí po střední ose (zachová zahnutí).
STAIRS_LONG_ELONGATION = 6.0
# ChodnikPlocha, ve které leží OSM silnice, je ve skutečnosti silnice (chyba v DTM); viz
# ``reclassify_sidewalks_as_roads``.
RECLASS_MIN_WIDTH_M = 4.0
RECLASS_MIN_ROAD_M = 30.0
RECLASS_ROAD_ONLY_M = 150.0
CENTERLINE_STEP_M = 0.25
STAIRS_AXIS_EXTRA_M = 0.3
# Sloučení sousedních ploch a zacelení škvír užších než 2 × tato hodnota (m).
CLOSE_M = 0.5
# Díry menší než tato plocha (m²) se zacelí (drobné záhonky, sloupky).
MIN_HOLE_M2 = 2.0
SIMPLIFY_M = 0.1
# OSM / ZABAGED čára blíž než tato vzdálenost (m) k ploše z DTM je duplicitní.
OSM_COVER_M = 0.5
# Zbytek OSM čáry kratší než toto (m) se zahodí (střepy po ořezu).
OSM_MIN_REMAINDER_M = 3.0
# Zbytek OSM čáry, jehož aspoň ``PARALLEL_SHARE`` délky leží do ``PARALLEL_NEAR_M`` od plochy z DTM,
# je souběžný duplikát (OSM chodník posunutý proti DTM) a zahodí se. Krátké pokračování chodníku
# za koncem plochy (míří pryč) tímto filtrem neprojde, protože se od plochy rychle vzdaluje.
PARALLEL_NEAR_M = 2.5
PARALLEL_SHARE = 0.75
# OSM zpevněná plocha (stejný symbol 501.0) překrývající chodníky z DTM aspoň takto (m²)
# se s nimi sloučí do jedné plochy, aby se nekreslila dvakrát.
ABSORB_MIN_OVERLAP_M2 = 1.0
# Šířka chodníku z OSM čáry (m) – jako symbol 501.10.
OSM_WIDTH_M = 1.4
# Plochy menší než tato (m²) se po sloučení zahodí.
MIN_AREA_M2 = 1.0


def load_sidewalk_polygons(opl_zip: str | Path, *, use_cache: bool = True) -> list[list[list[tuple[float, float]]]]:
    """Plochy chodníků na povrchu (úroveň 0) z balíku ZPS_OPL: ``[[vnější obrys, díra, …], …]``.

    Chodníky na mostech (úroveň > 0) a pod povrchem se nepoužijí – ty kreslí cesty z OSM.
    Výsledek se cachuje u balíku.
    """
    return load_polygons(opl_zip, SIDEWALK_TYPES, CACHE_SUFFIX, use_cache=use_cache)


def load_road_polygons(opl_zip: str | Path, *, use_cache: bool = True) -> list[list[list[tuple[float, float]]]]:
    """Plochy silnic / manipulačních ploch na povrchu (úroveň 0) z balíku ZPS_OPL."""
    return load_polygons(opl_zip, ROAD_TYPES, ROAD_CACHE_SUFFIX, use_cache=use_cache)


def load_building_polygons(opl_zip: str | Path, *, use_cache: bool = True) -> list[list[list[tuple[float, float]]]]:
    """Půdorysy budov na povrchu (``BudovaPlocha``, úroveň 0) z balíku ZPS_OPL."""
    return load_polygons(opl_zip, BUILDING_TYPES, BUILDING_CACHE_SUFFIX, use_cache=use_cache)


def load_green_polygons(opl_zip: str | Path, *, use_cache: bool = True) -> list[list[list[tuple[float, float]]]]:
    """Udržovaná zeleň na povrchu (úroveň 0) z balíku ZPS_OPL."""
    return load_polygons(opl_zip, GREEN_TYPES, GREEN_CACHE_SUFFIX, use_cache=use_cache)


def load_parking_polygons(opl_zip: str | Path, *, use_cache: bool = True) -> list[list[list[tuple[float, float]]]]:
    """Parkoviště a odstavné plochy na povrchu (úroveň 0) z balíku ZPS_OPL."""
    return load_polygons(opl_zip, PARKING_TYPES, PARKING_CACHE_SUFFIX, use_cache=use_cache)


def separate_from_roads(geom, roads, gap_m: float = ROAD_EDGE_GAP_M):
    """Plocha ``geom`` (chodník) po ořezu o silnice ``roads`` a zacelení škvír užších než ``gap_m``.

    Překryv se odebere ploše, škvíra mezi plochou a silnicí se přidá ploše, takže obě sousedí na
    jedné hraně (jedna čára obrysu místo dvou těsně vedle sebe).
    """
    from shapely.ops import unary_union

    if geom is None or roads is None or roads.is_empty:
        return geom
    both = unary_union([geom, roads])
    # „zacelení“ obou ploch dohromady; z toho, co přibylo, jen kusy dotýkající se chodníku i silnice
    # (ne rohy na koncích ploch a ne škvíry uvnitř jedné z nich)
    fill = both.buffer(gap_m / 2, join_style=2).buffer(-gap_m / 2, join_style=2).difference(both)
    pieces = [fill] if fill.geom_type == "Polygon" else list(getattr(fill, "geoms", []))
    near_geom, near_roads = geom.buffer(0.02), roads.buffer(0.02)
    gaps = [p for p in pieces if p.geom_type == "Polygon" and p.intersects(near_geom) and p.intersects(near_roads)]
    return unary_union([geom, *gaps]).difference(roads)


def _long_side(poly) -> float:
    """Delší strana opsaného (otočeného) obdélníku polygonu (m) – délka kusu koridoru."""
    rect = poly.minimum_rotated_rectangle
    if rect.is_empty or rect.geom_type != "Polygon":
        return 0.0
    pts = list(rect.exterior.coords)[:4]
    a = ((pts[1][0] - pts[0][0]) ** 2 + (pts[1][1] - pts[0][1]) ** 2) ** 0.5
    b = ((pts[2][0] - pts[1][0]) ** 2 + (pts[2][1] - pts[1][1]) ** 2) ** 0.5
    return max(a, b)


def road_corridor(
    roads, sidewalks, bike_lines=None, *, scale: int, min_width_mm: float = CORRIDOR_MIN_WIDTH_MM, parking=None
):
    """Koridor silnice pro MTBO: silnice + přilehlé chodníky jedním polygonem (shapely) nebo None.

    Chodník se bere jen do ``CORRIDOR_NEAR_M`` m od silnice a ne tam, kde se smí jezdit na kole
    (``bike_lines`` = čáry OSM chodníků se sdíleným provozem). ``parking``: plocha parkovišť (DTM + OSM);
    část do ``CORRIDOR_PARKING_NEAR_M`` m od silnice patří ke koridoru (chodník za parkovištěm se tak
    taky sloučí). Mezera do ``CORRIDOR_GAP_M`` m (pruh zeleně) se zacelí. Zůstanou jen části širší než
    ``min_width_mm`` mm mapy (v ``scale``) a delší než ``CORRIDOR_MIN_LEN_M`` m.
    """
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    if roads is None or roads.is_empty:
        return None
    # jízdní plocha: jen části silnice široké aspoň CORRIDOR_MIN_ROADWAY_M (otevření plochy)
    h = CORRIDOR_MIN_ROADWAY_M / 2
    roads = roads.buffer(-h, join_style=2).buffer(h, join_style=2).intersection(roads)
    if roads.is_empty:
        return None
    parts = [roads]
    base = roads
    if parking is not None and not parking.is_empty:
        park_near = parking.intersection(roads.buffer(CORRIDOR_PARKING_NEAR_M))
        if not park_near.is_empty:
            parts.append(park_near)
            base = unary_union([roads, park_near])
    if sidewalks is not None and not sidewalks.is_empty:
        near = sidewalks.intersection(base.buffer(CORRIDOR_NEAR_M))
        if bike_lines:
            protect = unary_union([LineString(c) for c in bike_lines if len(c) >= 2]).buffer(CORRIDOR_BIKE_PROTECT_M)
            near = near.difference(protect)
        if not near.is_empty:
            parts.append(near)
    union = unary_union(parts)
    g = CORRIDOR_GAP_M / 2
    corridor = union.buffer(g, join_style=2).buffer(-g, join_style=2)
    s = CORRIDOR_SMOOTH_M
    corridor = corridor.buffer(-s).buffer(s)  # odříznutí úzkých výběžků (chodníčky ke vchodům)
    corridor = corridor.simplify(0.15, preserve_topology=True)
    width = min_width_mm * scale / 1000.0
    wide = corridor.buffer(-width / 2, join_style=2).buffer(width / 2, join_style=2)
    wide = wide.intersection(corridor)
    pieces = [wide] if wide.geom_type == "Polygon" else [x for x in getattr(wide, "geoms", []) if x.geom_type == "Polygon"]
    keep = [x for x in pieces if x.area >= width * CORRIDOR_MIN_LEN_M and _long_side(x) >= CORRIDOR_MIN_LEN_M]
    return unary_union(keep) if keep else None


def load_small_paved_polygons(opl_zip: str | Path, *, use_cache: bool = True) -> list[list[list[tuple[float, float]]]]:
    """Malé stavby pro zpevnění povrchu (schody, rampy, zápraží u vchodů), úroveň 0."""
    return load_polygons(opl_zip, SMALL_PAVED_TYPES, SMALL_PAVED_CACHE_SUFFIX, use_cache=use_cache)


def load_stairs_polygons(opl_zip: str | Path, *, use_cache: bool = True) -> list[list[list[tuple[float, float]]]]:
    """Plochy schodišť na povrchu (``SchodistePlocha``, úroveň 0) z balíku ZPS_OPL."""
    return load_polygons(opl_zip, STAIRS_TYPES, STAIRS_CACHE_SUFFIX, use_cache=use_cache)


def end_at_edge(coords: list[tuple[float, float]], area, snap_m: float = EDGE_SNAP_M, min_len_m: float = STAIRS_MIN_LEN_M):
    """Čára ukončená přesně na hraně zpevněné plochy ``area`` (shapely): seznam souřadnic (0–1 čára).

    - Začíná-li / končí-li čára uvnitř plochy, odřízne se část uvnitř po první / poslední hranu.
    - Končí-li čára mimo plochu do ``snap_m`` m od její hrany, prodlouží se na nejbližší bod hrany.
    - Čára procházející plochou (oba konce venku) se nemění. Čára celá uvnitř zmizí.
    """
    from shapely.geometry import LineString, Point
    from shapely.ops import nearest_points, substring

    if area is None or area.is_empty or len(coords) < 2:
        return [coords]
    line = LineString(coords)
    if line.length <= 0:
        return [coords]
    boundary = area.boundary
    start_in = area.contains(Point(coords[0]))
    end_in = area.contains(Point(coords[-1]))
    s, e = 0.0, line.length
    if start_in or end_in:
        crossing = line.intersection(boundary)
        pts = [crossing] if crossing.geom_type == "Point" else [g for g in getattr(crossing, "geoms", []) if g.geom_type == "Point"]
        if not pts:
            return []  # celá uvnitř
        params = sorted(line.project(p) for p in pts)
        if start_in:
            s = params[0]
        if end_in:
            e = params[-1]
        if e - s < min_len_m:
            return []
        line = substring(line, s, e)
    pts_out = [(float(x), float(y)) for x, y in line.coords]
    for idx in (0, -1):
        p = Point(pts_out[idx])
        if area.contains(p) or area.touches(p):
            continue
        d = p.distance(area)
        if 1e-6 < d <= snap_m:
            q = nearest_points(boundary, p)[0]
            new = (float(q.x), float(q.y))
            if idx == 0:
                pts_out.insert(0, new)
            else:
                pts_out.append(new)
    return [pts_out]


def steps_keep(coords: list[tuple[float, float]], stair_zone, cover, min_len_m: float = STAIRS_MIN_LEN_M):
    """Části OSM čáry schodů, které se mají kreslit, jako seznamy souřadnic.

    Zůstává jen to, co leží mimo schodiště z DTM (``stair_zone``; ta se kreslí z DTM) a mimo
    plochy chodníků a silnic z DTM (``cover``; uvnitř nich DTM o schodech nic neví, takže OSM
    schody „přes“ celý chodník odpadnou – DTM má přednost).
    """
    from shapely.geometry import LineString
    from shapely.ops import linemerge, unary_union

    line = LineString(coords)
    blocked = stair_zone if cover is None else unary_union([stair_zone, cover])
    merged = line.difference(blocked)
    if merged.is_empty:
        return []
    if merged.geom_type == "MultiLineString":
        merged = linemerge(merged)  # unary_union uzlí čáru v místech průniku → zpět na souvislé kusy
    pieces = [merged] if merged.geom_type == "LineString" else [g for g in getattr(merged, "geoms", []) if g.geom_type == "LineString"]
    return [[(float(x), float(y)) for x, y in g.coords] for g in pieces if g.length >= min_len_m]


def polygon_centerline(g, step_m: float = CENTERLINE_STEP_M):
    """Střední čára protáhlého polygonu (i zakřiveného): ``[(x, y), …]`` nebo None.

    Voronoiův diagram hustě vzorkovaného obrysu → hrany uvnitř polygonu tvoří kostru (strom);
    střední čára je nejdelší cesta v ní. Konce se prodlouží až k obrysu polygonu.
    """
    import heapq
    import math

    from shapely import MultiPoint
    from shapely.geometry import LineString, Point
    from shapely.ops import voronoi_diagram

    if g.is_empty or g.geom_type != "Polygon":
        return None
    ring = g.exterior
    n = max(8, int(ring.length / step_m))
    pts = [ring.interpolate(ring.length * i / n) for i in range(n)]
    try:
        edges = voronoi_diagram(MultiPoint(pts), edges=True)
    except Exception:
        return None
    inner = g.buffer(1e-6)
    adj: dict[tuple[float, float], list[tuple[tuple[float, float], float]]] = {}
    def lines_of(geom):
        if geom.geom_type == "LineString":
            yield geom
        else:
            for part in getattr(geom, "geoms", []):
                yield from lines_of(part)

    for e in lines_of(edges):
        if not inner.contains(e):
            continue
        a, b = e.coords[0], e.coords[-1]
        a, b = (round(a[0], 4), round(a[1], 4)), (round(b[0], 4), round(b[1], 4))
        w = math.dist(a, b)
        adj.setdefault(a, []).append((b, w))
        adj.setdefault(b, []).append((a, w))
    if len(adj) < 2:
        return None

    def far(start):
        dist = {start: 0.0}
        prev: dict = {}
        heap = [(0.0, start)]
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist.get(u, math.inf):
                continue
            for v, w in adj[u]:
                nd = d + w
                if nd < dist.get(v, math.inf):
                    dist[v], prev[v] = nd, u
                    heapq.heappush(heap, (nd, v))
        end = max(dist, key=dist.get)
        return end, prev

    u, _ = far(next(iter(adj)))
    v, prev = far(u)
    path = [v]
    while path[-1] != u:
        path.append(prev[path[-1]])
    if len(path) < 2:
        return None
    line = LineString(path).simplify(0.05)
    coords = list(line.coords)
    # prodloužení obou konců až k obrysu (kostra končí o půl šířky před koncem polygonu)
    for idx, sign in ((0, 1), (-1, -1)):
        p = coords[idx]
        q = coords[1] if idx == 0 else coords[-2]
        dx, dy = p[0] - q[0], p[1] - q[1]
        nrm = math.hypot(dx, dy)
        if nrm < 1e-9:
            continue
        ray = LineString([p, (p[0] + dx / nrm * 3.0, p[1] + dy / nrm * 3.0)]).intersection(inner)
        segs = [ray] if ray.geom_type == "LineString" else [x for x in getattr(ray, "geoms", []) if x.geom_type == "LineString"]
        segs = [s for s in segs if s.distance(Point(p)) < 1e-6]
        if segs:
            far_pt = max(segs[0].coords, key=lambda c: math.dist(c, p))
            if idx == 0:
                coords.insert(0, far_pt)
            else:
                coords.append(far_pt)
    return [(float(x), float(y)) for x, y in coords]


def stairs_axis(poly: list[list[tuple[float, float]]], ref_lines: list | None = None):
    """Osa schodiště z DTM: ``[(x, y), (x, y)]`` nebo None.

    Vede středem schodiště ve směru nejbližší cesty z ``ref_lines`` (shapely čáry; nejdřív OSM
    schody, pak ostatní cesty do ``STAIRS_REF_NEAR_M`` m), takže navazuje na cestu před a za
    schodištěm. Bez cesty v okolí po delší straně opsaného obdélníku. Délka = průměr schodiště
    ve zvoleném směru (+ ``STAIRS_AXIS_EXTRA_M`` na každé straně).
    """
    from shapely.geometry import LineString, Point, Polygon

    try:
        g = Polygon(poly[0], poly[1:])
        g = g if g.is_valid else g.buffer(0)
        rect = g.minimum_rotated_rectangle
    except (ValueError, IndexError):
        return None
    if g.is_empty or rect.is_empty or rect.geom_type != "Polygon":
        return None
    # protáhlost (délka / šířka)^~: (půl obvodu)² / plocha; čtverec 4, obdélník 4 : 1 ≈ 6,25
    elongation = (g.length / 2.0) ** 2 / max(g.area, 1e-6)
    if elongation >= STAIRS_LONG_ELONGATION and g.geom_type == "Polygon":
        # Dlouhé schodiště (i zahnuté): čára po jeho střední ose, ne přímka.
        center = polygon_centerline(g)
        if center is not None and len(center) >= 2 and LineString(center).length >= STAIRS_MIN_LEN_M:
            return center
    c = g.centroid
    ux = uy = None
    best = None
    for rank, lines in enumerate(ref_lines or []):  # skupiny: 0 = schody, 1 = ostatní cesty
        for ln in lines:
            d = ln.distance(g)
            if d <= STAIRS_REF_NEAR_M and (best is None or (rank, d) < best[:2]):
                best = (rank, d, ln)
        if best is not None:
            break
    if best is not None:
        ln = best[2]
        s = ln.project(Point(c.x, c.y))
        a, b = ln.interpolate(max(0.0, s - 1.0)), ln.interpolate(min(ln.length, s + 1.0))
        dx, dy = b.x - a.x, b.y - a.y
        n = (dx * dx + dy * dy) ** 0.5
        if n > 1e-6:
            ux, uy = dx / n, dy / n
    if ux is None:
        pts = list(rect.exterior.coords)[:4]
        e1 = (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
        e2 = (pts[2][0] - pts[1][0], pts[2][1] - pts[1][1])
        l1, l2 = (e1[0] ** 2 + e1[1] ** 2) ** 0.5, (e2[0] ** 2 + e2[1] ** 2) ** 0.5
        ex, ey, ln_ = (e1[0], e1[1], l1) if l1 >= l2 else (e2[0], e2[1], l2)
        if ln_ < 1e-6:
            return None
        ux, uy = ex / ln_, ey / ln_
    # průměr schodiště ve směru (ux, uy) středem těžiště
    probe = LineString([(c.x - ux * 100, c.y - uy * 100), (c.x + ux * 100, c.y + uy * 100)]).intersection(g)
    chords = [probe] if probe.geom_type == "LineString" else [x for x in getattr(probe, "geoms", []) if x.geom_type == "LineString"]
    if not chords:
        return None
    chord = max(chords, key=lambda x: x.length)
    if chord.length < STAIRS_MIN_LEN_M - 2 * STAIRS_AXIS_EXTRA_M:
        return None
    (x0, y0), (x1, y1) = chord.coords[0], chord.coords[-1]
    e = STAIRS_AXIS_EXTRA_M
    return [(x0 - ux * e, y0 - uy * e), (x1 + ux * e, y1 + uy * e)]


def load_polygons(
    opl_zip: str | Path, types: tuple[str, ...], cache_suffix: str, *, use_cache: bool = True
) -> list[list[list[tuple[float, float]]]]:
    opl_zip = Path(opl_zip)
    cache = opl_zip.with_name(opl_zip.name + cache_suffix)
    if use_cache and cache.is_file() and cache.stat().st_mtime >= opl_zip.stat().st_mtime:
        try:
            raw = json.loads(cache.read_text(encoding="utf-8"))
            return [[[(x, y) for x, y in ring] for ring in poly] for poly in raw]
        except (OSError, ValueError, TypeError):
            pass
    out: list[list[list[tuple[float, float]]]] = []
    for rec in iter_records(opl_zip, set(types)):
        if rec.geom_type != "Polygon" or not rec.coords or rec.level != 0:
            continue
        poly = [[(float(c[0]), float(c[1])) for c in ring] for ring in rec.coords if len(ring) >= 4]
        if poly:
            out.append(poly)
    if use_cache:
        try:
            cache.write_text(
                json.dumps([[[list(p) for p in ring] for ring in poly] for poly in out]), encoding="utf-8"
            )
        except OSError:
            pass
    return out


def _clean(geom, *, close_m: float, min_hole_m2: float, simplify_m: float):
    """Zacelí škvíry, odstraní drobné díry a zjednoduší tvar."""
    from shapely.geometry import MultiPolygon, Polygon

    if geom.is_empty:
        return geom
    if close_m > 0:
        geom = geom.buffer(close_m, join_style=2).buffer(-close_m, join_style=2)
    polys = [geom] if geom.geom_type == "Polygon" else [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
    cleaned = []
    for p in polys:
        holes = [r for r in p.interiors if Polygon(r).area >= min_hole_m2]
        cleaned.append(Polygon(p.exterior, holes))
    geom = MultiPolygon(cleaned) if len(cleaned) != 1 else cleaned[0]
    if simplify_m > 0 and not geom.is_empty:
        geom = geom.simplify(simplify_m, preserve_topology=True)
    return geom


def reclassify_sidewalks_as_roads(
    sidewalk_polys: list[list[list[tuple[float, float]]]],
    road_lines: list[list[tuple[float, float]]],
    *,
    min_width_m: float = RECLASS_MIN_WIDTH_M,
    min_road_m: float = RECLASS_MIN_ROAD_M,
    road_only_m: float = RECLASS_ROAD_ONLY_M,
):
    """Chybně zařazené ``ChodnikPlocha`` (ve skutečnosti silnice) → ``(chodníky, silnice)``.

    Polygon se přeřadí mezi silnice, když uvnitř leží OSM silnice: aspoň ``road_only_m`` m délky,
    nebo aspoň ``min_road_m`` m a střední šířka polygonu (2 × plocha / obvod) ≥ ``min_width_m``.
    Chodník, který silnici jen kříží, má uvnitř málo silnice a je úzký, takže se nepřeřadí.
    """
    from shapely import STRtree
    from shapely.geometry import LineString, Polygon

    if not road_lines or not sidewalk_polys:
        return sidewalk_polys, []
    lines = [LineString(c) for c in road_lines if len(c) >= 2]
    if not lines:
        return sidewalk_polys, []
    tree = STRtree(lines)
    keep, moved = [], []
    for poly in sidewalk_polys:
        try:
            g = Polygon(poly[0], poly[1:])
            g = g if g.is_valid else g.buffer(0)
        except (ValueError, IndexError):
            keep.append(poly)
            continue
        if g.is_empty or g.geom_type != "Polygon" or g.length <= 0:
            keep.append(poly)
            continue
        inside = sum(lines[int(k)].intersection(g).length for k in tree.query(g))
        width = 2.0 * g.area / g.length
        if inside >= road_only_m or (inside >= min_road_m and width >= min_width_m):
            moved.append(poly)
        else:
            keep.append(poly)
    return keep, moved


def dtm_union(dtm_polys: list[list[list[tuple[float, float]]]]):
    """Vyčištěné sjednocení ploch chodníků z DTM (shapely) nebo None."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    parts = []
    for poly in dtm_polys:
        try:
            g = Polygon(poly[0], poly[1:])
        except (ValueError, IndexError):
            continue
        g = g if g.is_valid else g.buffer(0)
        if not g.is_empty:
            parts.append(g)
    if not parts:
        return None
    return _clean(unary_union(parts), close_m=CLOSE_M, min_hole_m2=MIN_HOLE_M2, simplify_m=SIMPLIFY_M)


def clip_outside(coords: list[tuple[float, float]], cover, min_len_m: float = OSM_MIN_REMAINDER_M):
    """Části čáry mimo ``cover`` (kratší než ``min_len_m`` se zahodí) jako seznamy souřadnic."""
    from shapely.geometry import LineString

    rest = LineString(coords).difference(cover)
    pieces = [rest] if rest.geom_type == "LineString" else [g for g in getattr(rest, "geoms", []) if g.geom_type == "LineString"]
    return [[(float(x), float(y)) for x, y in g.coords] for g in pieces if g.length >= min_len_m]


def unify_sidewalks(
    dtm_polys: list[list[list[tuple[float, float]]]],
    osm_lines: list[list[tuple[float, float]]],
    *,
    osm_width_m: float = OSM_WIDTH_M,
    cover_m: float = OSM_COVER_M,
    min_remainder_m: float = OSM_MIN_REMAINDER_M,
    extra_areas: list | None = None,
    widths: list[float] | None = None,
):
    """Jedna plocha chodníků: DTM (tvar) + zbytky OSM čar (aktuálnost); shapely geometrie nebo None."""
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    dtm = dtm_union(dtm_polys)
    covered = dtm.buffer(cover_m) if dtm is not None and not dtm.is_empty else None
    near_zone = dtm.buffer(PARALLEL_NEAR_M) if covered is not None else None
    extra = []
    for i, coords in enumerate(osm_lines):
        if len(coords) < 2:
            continue
        width = widths[i] if widths else osm_width_m
        line = LineString(coords)
        rest = line.difference(covered) if covered is not None else line
        pieces = [rest] if rest.geom_type == "LineString" else [g for g in getattr(rest, "geoms", []) if g.geom_type == "LineString"]
        for piece in pieces:
            if piece.length < min_remainder_m:
                continue
            if near_zone is not None and piece.intersection(near_zone).length >= PARALLEL_SHARE * piece.length:
                continue  # souběžná čára vedle plochy z DTM = tentýž chodník, jen posunutý
            # Zbytek leží ≥ cover_m od DTM; kulatý konec široký ``width`` do něj
            # kousek zasáhne, takže se po sjednocení napojí.
            extra.append(piece.buffer(width / 2.0, cap_style=1, join_style=1))
    items = ([dtm] if dtm is not None and not dtm.is_empty else []) + extra + list(extra_areas or [])
    if not items:
        return None
    # Sloučení škvír mezi OSM zbytkem a DTM (≤ 2 × cover_m − šířka) + stejné čištění jako u DTM.
    merged = _clean(unary_union(items), close_m=0.2, min_hole_m2=MIN_HOLE_M2, simplify_m=SIMPLIFY_M)
    polys = [merged] if merged.geom_type == "Polygon" else list(getattr(merged, "geoms", []))
    polys = [p for p in polys if p.geom_type == "Polygon" and p.area >= MIN_AREA_M2]
    if not polys:
        return None
    from shapely.geometry import MultiPolygon

    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def polygon_rings(geom) -> list[list[list[tuple[float, float]]]]:
    """``[[vnější obrys, díra, …], …]`` z (Multi)Polygonu."""
    if geom is None or geom.is_empty:
        return []
    polys = [geom] if geom.geom_type == "Polygon" else [g for g in geom.geoms if g.geom_type == "Polygon"]
    return [
        [[(float(x), float(y)) for x, y in p.exterior.coords]]
        + [[(float(x), float(y)) for x, y in r.coords] for r in p.interiors]
        for p in polys
    ]
