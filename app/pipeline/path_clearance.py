"""Odsun objektů od kreslených cest v lese a MTBO (pozdní krok buildu, nad hotovými objekty mapy).

Symboly cest jsou v lese a MTBO na mapě širší než cesta v terénu, takže se jimi překrývají
budovy, oliva, zdi, ploty i bodové objekty kolem. Po sestavení všech objektů (ploty už spojené,
deduplikované a orientované) se proto objekty odsunou od os cest tak, aby mezi okrajem symbolu
cesty a objektem zbyla mezera: od olivy minimální, od objektů s černou kresbou o něco větší.
Pracuje se v souřadnicích mapy (1 jednotka = 1 µm), šířky symbolů se berou ze sady. Osy cest se
berou z hotové mapy (všechny kreslené cesty bez ohledu na zdroj – OSM i ZABAGED).

- Oliva se ořízne o pás podél cesty (kousky pod minimální šířku se zahodí; oliva skoro celá pod cestou pryč). Oliva, která by
  jinak zůstala celá, se aspoň o pás silnic (502, 503) ořízne vždy.
- Ostatní plochy (budovy …) a body se posunou celé, tvar se nemění. Bodová budova (521.3) se odsouvá podle skutečné velikosti
  symbolu v sadě (čtverec, ne kolečko); když se nevejde vedle silnice (502, 503), nekreslí se. Budova, kterou nejde odsunout a zasahuje do silnice
  (502, 503), se o pás silnice ořízne; drobné zbytky se zahodí (silnice mají přednost).
- Čáry (zdi, ploty) se odsunou po bodech; nesmí se tím přiblížit k jiné cestě ani výrazně změnit délku. Čára, kterou nejde
  odsunout, se z pásu silnic (502, 503) vyřízne (kusy pod ``CLIFF_MIN_PIECE_MM`` pryč).

Objekt, který cestu kříží nebo by se musel posunout víc než ``NONSPRINT_AVOID_MAX_SHIFT_MM``,
zůstane (budova kříženou silnicí 502 / 503 se ořízne, viz výše). Vegetace (les, louka, houštiny …), terén, voda, cesty a zpevněné plochy se nemění.
Sprint se nemění.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from pathlib import Path

from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import MAP_COORD_CLOSE_POINT, MAP_COORD_CURVE_START, MAP_COORD_HOLE_POINT, OomObjectPart, _fmt

# Odsouvané symboly (hlavní číslo kódu v sadě; ISOM 2017 i ISMTBOM 2022 číslují umělé objekty stejně).
MOVED_MAJORS = frozenset({513, 514, 515, 516, 517, 518, 520, 521, 522, 523, 524, 525, 526, 527, 528, 529, 530, 531})
OLIVE_MAJOR = 520
# Symboly cest, od kterých se odsouvá (silnice, cesty, pěšiny, schody; MTBO navíc 8xx).
PATH_MAJORS = frozenset({502, 503, 504, 505, 506, 507, 508, 532})
# Srázy (ISOM 104 hliněný sráz, 201 nepřekonatelný, 202 překonatelný; ISMTBOM stejně): kvůli čárkám symbolu se nesmí
# kreslit na cestě. Úsek podél cesty se odsune ven (po bodech jako plot), úsek, který cestu kříží nebo se nevejde,
# se vynechá (Petr 2026-10-26, Barrandov u tramvaje). Délka čárek symbolu nad šířku čáry (mm) a nejkratší ponechaný kus.
CLIFF_MAJORS = frozenset({104, 201, 202})
CLIFF_TICK_MM = 0.5
CLIFF_MIN_PIECE_MM = 0.5
CLIFF_MAX_SHIFT_MM = 1.5
PAVED_MAJOR = 501
# Silnice (502 a 503): budova, kterou nejde odsunout, se o jejich pás ořízne (Petr: silnice mají přednost). Ostatní cesty
# (504–508, 532, MTBO 8xx) budovu neořezávají, budova přeťatá pěšinou zůstává.
ROAD_MAJORS = frozenset({502, 503})
BUILDING_MAJOR = 521
# Kus budovy po ořezu o silnici: nejmenší šířka (mm na mapě); nejmenší výměra bere BUILDING_MIN_AREA_MM2 z osm_paths.
MIN_BUILDING_WIDTH_MM = 0.2
# Mezera mezi okrajem symbolu cesty a objektem (mm na mapě): oliva / ostatní (černá kresba).
OLIVE_GAP_MM = 0.05
BLACK_GAP_MM = 0.2
# Bodové objekty: poloměr kresby (mm na mapě).
POINT_RADIUS_MM = 0.3
# Oliva po ořezu: kousky užší než tato šířka (mm na mapě) se zahodí; zbýt musí aspoň polovina výměry.
MIN_AREA_WIDTH_MM = 0.4
MIN_AREA_SHARE = 0.5
# Oliva, ze které po ořezu nezbude kus širší než minimum a ani tento podíl výměry, leží pod cestou → zahodí se.
DROP_OLIVE_SHARE = 0.1
# Čára smí odsunem změnit délku nejvýš o tento podíl.
LINE_LENGTH_TOL = 0.2
# Odsun čar: krok zhuštění (mm na mapě); tolerance (µm).
DENSIFY_MM = 0.2
EPS = 5.0

_OBJECT_RE = re.compile(r'(<object type="(\d)" symbol="(-?\d+)"[^>]*>)(.*?)(</object>)', re.S)
_COORDS_RE = re.compile(r'<coords count="\d+">(.*?)</coords>', re.S)


@lru_cache(maxsize=8)
def _symbol_info(symbol_set: Path) -> dict[int, tuple[int, str]]:
    """``{index symbolu: (hlavní číslo kódu, "area" | "line" | "point" | "other")}`` ze sady."""
    import xml.etree.ElementTree as ET

    out: dict[int, tuple[int, str]] = {}
    root = ET.parse(symbol_set).getroot()
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "symbol" or el.get("id") is None:
            continue
        try:
            major = int(str(el.get("code") or "").split(".", 1)[0])
        except ValueError:
            continue
        t = el.get("type")
        if t == "1":
            kind = "point"
        elif t == "4":
            kind = "area"
        elif t == "2":
            kind = "line"
        elif t == "16":
            tags = {x.tag.rsplit("}", 1)[-1] for x in el.iter()}
            kind = "area" if "area_symbol" in tags else "line"
        else:
            kind = "other"
        out[int(el.get("id"))] = (major, kind)
    return out


@lru_cache(maxsize=8)
def _point_symbol_boxes(symbol_set: Path) -> dict[int, tuple[float, float, float, float]]:
    """``{index bodového symbolu: (minx, miny, maxx, maxy) v µm}`` – obálka všech souřadnic elementů ``<point_symbol>``."""
    import xml.etree.ElementTree as ET

    out: dict[int, tuple[float, float, float, float]] = {}
    root = ET.parse(symbol_set).getroot()
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "symbol" or el.get("id") is None or el.get("type") != "1":
            continue
        xs: list[float] = []
        ys: list[float] = []
        for ps in el.iter():
            if ps.tag.rsplit("}", 1)[-1] != "point_symbol":
                continue
            for c in ps.iter():
                if c.tag.rsplit("}", 1)[-1] != "coords":
                    continue
                for pt in _parse_coords(c.text or ""):
                    xs.append(pt[0])
                    ys.append(pt[1])
        if xs:
            out[int(el.get("id"))] = (min(xs), min(ys), max(xs), max(ys))
    return out


def _parse_coords(text: str) -> list[list[int]]:
    pts = []
    for item in text.split(";"):
        nums = item.split()
        if len(nums) >= 2:
            pts.append([int(nums[0]), int(nums[1]), int(nums[2]) if len(nums) > 2 else 0])
    return pts


def _format_coords(pts: list[list[int]]) -> str:
    return f'<coords count="{len(pts)}">' + "".join(_fmt(x, y, f) + ";" for x, y, f in pts) + "</coords>"


class _Paths:
    """Osy cest v souřadnicích mapy s půlkou šířky symbolu (µm), prostorový index."""

    def __init__(self, axes: list[tuple[list[tuple[int, int]], float]], majors: list[int] | None = None):
        from shapely import STRtree
        from shapely.geometry import LineString

        self.geoms = [LineString(c) for c, _h in axes]
        self.half = [h for _c, h in axes]
        self.majors = majors  # hlavní číslo symbolu u každé osy; None = neznámé (ořez o silnice se nedělá)
        self.tree = STRtree(self.geoms)
        self.reach = max(self.half) if self.half else 0.0

    def near(self, geom, extra):
        return [int(k) for k in self.tree.query(geom.buffer(self.reach + extra))]

    def corridor(self, ks, extra):
        from shapely.ops import unary_union

        return unary_union([self.geoms[k].buffer(self.half[k] + extra, cap_style="flat") for k in ks])


def _rings(pts: list[list[int]]) -> list[list[list[int]]]:
    rings, cur = [], []
    for p in pts:
        cur.append(p)
        if p[2] & MAP_COORD_CLOSE_POINT:
            rings.append(cur)
            cur = []
    if cur:
        rings.append(cur)
    return rings


def _flatten(ring: list[list[int]], steps: int = 8) -> list[tuple[float, float]]:
    """Body prstence s Bézierovými úseky převedenými na lomenou čáru."""
    out: list[tuple[float, float]] = []
    i = 0
    while i < len(ring):
        p = ring[i]
        out.append((p[0], p[1]))
        if p[2] & MAP_COORD_CURVE_START and i + 3 < len(ring):
            c1, c2, q = ring[i + 1], ring[i + 2], ring[i + 3]
            for j in range(1, steps):
                t = j / steps
                a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t**3
                out.append((a * p[0] + b * c1[0] + c * c2[0] + d * q[0], a * p[1] + b * c1[1] + c * c2[1] + d * q[1]))
            i += 3
            continue
        i += 1
    return out


def _polygon(rings):
    from shapely.geometry import Polygon

    try:
        flat = [_flatten(r) for r in rings]
        poly = Polygon(flat[0], [r for r in flat[1:] if len(r) >= 3])
        return poly if poly.is_valid else poly.buffer(0)
    except (ValueError, IndexError):
        return None


def _ring_coords(coords) -> list[list[int]]:
    pts = [[round(x), round(y), 0] for x, y in coords]
    if len(pts) >= 2 and pts[0][:2] != pts[-1][:2]:
        pts.append([pts[0][0], pts[0][1], 0])
    pts[-1][2] = MAP_COORD_CLOSE_POINT | MAP_COORD_HOLE_POINT
    return pts


def _clip_olive(pts, paths: _Paths, extra, min_half):
    """Olivu ořízne o pás podél cest → seznam ploch (body), None = beze změny. Nevalidní obrys se opraví."""
    from shapely.geometry import Polygon

    rings = _rings(pts)
    poly = _polygon(rings)
    if poly is None or poly.is_empty:
        return None
    try:
        flat = [_flatten(r) for r in rings]
        broken = not Polygon(flat[0], [r for r in flat[1:] if len(r) >= 3]).is_valid
    except (ValueError, IndexError):
        broken = True
    ks = paths.near(poly, extra)
    if not ks and not broken:
        return None
    new = poly.difference(paths.corridor(ks, extra)) if ks else poly
    if new.area >= poly.area - 1.0 and not broken:
        return None  # beze změny (sebeprotínající se obrys se opraví vždy)
    pieces = [g for g in getattr(new, "geoms", [new]) if isinstance(g, Polygon) and not g.is_empty]
    if not broken:
        pieces = [g for g in pieces if not g.buffer(-min_half).is_empty]
        if not pieces and new.area < DROP_OLIVE_SHARE * poly.area:
            return []  # oliva leží skoro celá pod cestou → zahodí se
        if not pieces or sum(g.area for g in pieces) < MIN_AREA_SHARE * poly.area:
            return None
    elif not pieces:
        return None
    out = []
    for g in (g.simplify(EPS) for g in pieces):
        for piece in _rounded_pieces(g):
            out.append(piece)
    return out or None


def _clip_by_roads(pts, paths: _Paths, extra, min_half, min_area):
    """Plochu (budova, oliva), kterou nejde odsunout, ořízne o pás silnic (``ROAD_MAJORS``) → seznam ploch (body),
    [] = leží pod silnicí, None = beze změny (osy bez známého symbolu, silnice se jí netýká)."""
    from shapely.geometry import Polygon

    if not paths.majors:
        return None
    poly = _polygon(_rings(pts))
    if poly is None or poly.is_empty:
        return None
    ks = [k for k in paths.near(poly, extra) if paths.majors[k] in ROAD_MAJORS]
    if not ks:
        return None
    new = poly.difference(paths.corridor(ks, extra))
    if new.area >= poly.area - 1.0:
        return None
    pieces = [
        g
        for g in getattr(new, "geoms", [new])
        if isinstance(g, Polygon) and g.area >= min_area and not g.buffer(-min_half).is_empty
    ]
    out = []
    for g in (g.simplify(EPS) for g in pieces):
        out.extend(_rounded_pieces(g))
    return out


def _clip_line_by_roads(pts, paths: _Paths, extra, min_piece):
    """Čáru (plot, zeď), kterou nejde odsunout, vyřízne z pásu silnic (``ROAD_MAJORS``) → seznam kusů (body), [] = celá
    leží v pásu, None = beze změny (křivky / značené body, silnice se jí netýká). Kusy pod ``min_piece`` (µm) pryč."""
    from shapely.geometry import LineString

    if not paths.majors or any(p[2] & MAP_COORD_CURVE_START for p in pts) or any(p[2] for p in pts[:-1]):
        return None
    line = LineString([(p[0], p[1]) for p in pts])
    ks = [k for k in paths.near(line, extra) if paths.majors[k] in ROAD_MAJORS]
    if not ks:
        return None
    corridor = paths.corridor(ks, extra)
    if not line.intersects(corridor):
        return None
    rest = line.difference(corridor.buffer(-EPS))
    pieces = [g for g in getattr(rest, "geoms", [rest]) if g.geom_type == "LineString" and g.length >= min_piece]
    if len(pieces) == 1 and pieces[0].equals_exact(line, EPS):
        return None
    return [[[round(x), round(y), 0] for x, y in g.coords] for g in pieces]


def _rounded_pieces(g) -> list[list[list[int]]]:
    """Plocha jako body na celých µm; nevalidní po zaokrouhlení se opraví (buffer(0)), nepovedené kusy pryč."""
    from shapely.geometry import Polygon

    if g.is_empty or not isinstance(g, Polygon):
        return []
    rings = [_ring_coords(ring.coords) for ring in [g.exterior, *g.interiors]]
    if Polygon([p[:2] for p in rings[0]], [[p[:2] for p in r] for r in rings[1:]]).is_valid:
        return [[p for r in rings for p in r]]
    fixed = Polygon([p[:2] for p in rings[0]], [[p[:2] for p in r] for r in rings[1:]]).buffer(0)
    out = []
    for part in getattr(fixed, "geoms", [fixed]):
        if not isinstance(part, Polygon) or part.is_empty:
            continue
        rr = [_ring_coords(ring.coords) for ring in [part.exterior, *part.interiors]]
        if Polygon([p[:2] for p in rr[0]], [[p[:2] for p in r] for r in rr[1:]]).is_valid:
            out.append([p for r in rr for p in r])
    return out


def _shift_vector(geom, paths: _Paths, extra, max_shift):
    """Posun celého objektu od cest (dx, dy), None = beze změny (kříží cestu, nevejde se, moc daleko)."""
    from shapely.affinity import translate
    from shapely.ops import nearest_points

    ks = paths.near(geom, extra)
    if not ks:
        return None
    before = {k: paths.geoms[k].distance(geom) for k in ks}
    if min(before.values()) <= EPS:
        return None  # dotýká se osy / kříží ji (cesta k budově, do areálu …)
    tx = ty = 0.0
    g = geom
    for _ in range(4):
        worst = None
        for k in ks:
            d = paths.geoms[k].distance(g)
            deficit = paths.half[k] + extra - d
            if deficit > EPS and (worst is None or deficit > worst[0]):
                a, b = nearest_points(g, paths.geoms[k])
                n = math.hypot(a.x - b.x, a.y - b.y)
                if n > 1e-6:
                    worst = (deficit, (a.x - b.x) / n, (a.y - b.y) / n)
        if worst is None:
            break
        tx += worst[1] * (worst[0] + 1.0)
        ty += worst[2] * (worst[0] + 1.0)
        g = translate(geom, tx, ty)
    if (tx == 0 and ty == 0) or math.hypot(tx, ty) > max_shift:
        return None
    # Nikam se nesmí přiblížit víc než předtím (a do pásu jiné cesty).
    for k in paths.near(g, extra):
        need = paths.half[k] + extra
        d = paths.geoms[k].distance(g)
        if d + EPS < min(before.get(k, math.inf), need):
            return None
    return tx, ty


def _shift_point_box(x0, y0, box, paths: _Paths, gap, max_shift):
    """Posun bodové budovy (čtverec ``box`` kolem bodu) od cest (dx, dy), None = beze změny / nevejde se.

    Čtverec často přesahuje osu cesty, i když bod leží vedle ní (symbol je větší než budova), a ``_shift_vector`` objekt
    na ose nechá. Proto se nejdřív odsune samotný bod jako kolečko vepsané do čtverce, pak se dorovná čtverec (rohy)."""
    from shapely.affinity import translate
    from shapely.geometry import Point
    from shapely.geometry import box as shp_box

    geom = shp_box(x0 + box[0], y0 + box[1], x0 + box[2], y0 + box[3])

    def clear(g):
        return all(paths.geoms[k].distance(g) >= paths.half[k] + gap - EPS for k in paths.near(g, gap))

    if clear(geom):
        return None
    r_in = min(-box[0], -box[1], box[2], box[3])
    tx = ty = 0.0
    v1 = _shift_vector(Point(x0, y0), paths, max(r_in, 0.0) + gap, max_shift) if r_in > 0 else None
    if v1 is not None:
        tx, ty = v1
    g = translate(geom, tx, ty)
    if not clear(g):
        v2 = _shift_vector(g, paths, gap, max_shift - math.hypot(tx, ty))
        if v2 is None:
            return None
        tx, ty = tx + v2[0], ty + v2[1]
        g = translate(geom, tx, ty)
    if not clear(g) or math.hypot(tx, ty) > max_shift:
        return None
    return tx, ty


def _move_line(pts, paths: _Paths, extra, max_shift, step):
    """Odsune čáru po bodech (``avoid_paths``); None = beze změny / odsun by čáru zhoršil."""
    from shapely.geometry import LineString

    from app.pipeline import dtm_fences

    if any(p[2] & MAP_COORD_CURVE_START for p in pts) or any(p[2] for p in pts[:-1]):
        return None  # křivky / značené body (zlomy čárkování) se nechají
    line = LineString([(p[0], p[1]) for p in pts])
    ks = paths.near(line, extra)
    if not ks:
        return None
    axes = [(list(paths.geoms[k].coords), paths.half[k] + extra) for k in ks]
    moved = dtm_fences.avoid_paths([("", [(p[0], p[1]) for p in pts])], axes, max_shift=max_shift, step=step, keep_crossings=True)
    coords = moved[0][1]
    if len(coords) < 2:
        return None
    new = LineString(coords)
    if new.equals_exact(line, EPS):
        return None
    if abs(new.length - line.length) > LINE_LENGTH_TOL * line.length:
        return None
    # Čára se nesmí dostat blíž k žádné cestě než předtím (zeď odsunutá na jinou cestu).
    for k in paths.near(new, extra):
        need = paths.half[k] + extra
        if paths.geoms[k].distance(new) + EPS < min(paths.geoms[k].distance(line), need):
            return None
    out = [[round(x), round(y), 0] for x, y in coords]
    out[-1][2] = pts[-1][2]
    return out


def _tick_side_faces(pts, paths: _Paths, ks) -> bool:
    """True, když nejbližší cesta leží na straně čárek srázu (vpravo od směru čáry; ``<coords>0 0;0 800</coords>``
    mid-symbolu 104 / 201 = +y, v mapě y dolů)."""
    from shapely.geometry import LineString

    line = LineString([(p[0], p[1]) for p in pts])
    best = None
    for k in ks:
        g = paths.geoms[k]
        d = g.distance(line)
        if best is None or d < best[0]:
            best = (d, g)
    if best is None:
        return True
    g = best[1]
    # bod čáry nejblíž cestě a směr čáry v něm
    t = line.project(g.interpolate(g.project(line.centroid)))
    p = line.interpolate(t)
    q = g.interpolate(g.project(p))
    p0 = line.interpolate(max(0.0, t - 100.0))
    p1 = line.interpolate(min(line.length, t + 100.0))
    dx, dy = p1.x - p0.x, p1.y - p0.y
    vx, vy = q.x - p.x, q.y - p.y
    return dx * vy - dy * vx > 0  # y dolů: kladný součin = vpravo od směru


def _clear_cliff(pts, paths: _Paths, extra_line, extra_tick, max_shift, step, min_piece, paved=None):
    """Sráz u cesty nebo na zpevněné ploše (``paved``, shapely): odsune se po bodech, co zůstane v pásu cesty /
    na ploše (křížení, moc daleko), se vynechá. Odstup od cesty je ``extra_tick`` (čárky) jen když cesta leží na
    straně čárek, jinak ``extra_line``. Vrací None (beze změny), [] (celý pryč) nebo seznam kusů (body)."""
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    line = LineString([(p[0], p[1]) for p in pts])
    ks = paths.near(line, extra_tick)
    extra = extra_tick if ks and _tick_side_faces(pts, paths, ks) else extra_line
    ks = [k for k in ks if paths.geoms[k].distance(line) <= paths.half[k] + extra]
    zones = [paths.corridor(ks, extra)] if ks else []
    if paved is not None and not paved.is_empty and line.intersects(paved):
        zones.append(paved)  # jen část uvnitř plochy (parkoviště), okraj plochy čárky snesou
    if not zones:
        return None
    corridor = unary_union(zones)
    if not line.intersects(corridor):
        return None
    moved = None
    if ks and not any(p[2] & MAP_COORD_CURVE_START for p in pts):
        moved = _move_line(pts, paths, extra, max_shift, step)
    base = LineString([(p[0], p[1]) for p in moved]) if moved else line
    rest = base.difference(corridor.buffer(-EPS))  # odsunutá čára leží přesně na hranici pásu, ta se nevyřezává
    pieces = [g for g in getattr(rest, "geoms", [rest]) if g.geom_type == "LineString" and g.length >= min_piece]
    if moved is None and len(pieces) == 1 and pieces[0].equals_exact(line, EPS):
        return None
    return [[[round(x), round(y), 0] for x, y in g.coords] for g in pieces]


def _drawn_path_objects(parts, info, widths) -> list[tuple[list[tuple[float, float]], float, int]]:
    """Čárové objekty cest v mapě: ``[(osa v souřadnicích mapy, půlka šířky symbolu v µm, hlavní číslo symbolu)]``."""
    out = []
    for part in parts:
        for m in _OBJECT_RE.finditer(part.objects_xml):
            if m.group(2) != "1":
                continue
            sym = int(m.group(3))
            major, kind = info.get(sym, (0, "other"))
            if kind != "line" or not (major in PATH_MAJORS or 800 <= major < 900):
                continue
            width = widths.get(sym, 0.0)
            cm = _COORDS_RE.search(m.group(4))
            if width <= 0 or not cm:
                continue
            pts = _flatten(_parse_coords(cm.group(1)))
            if len(pts) >= 2:
                out.append((pts, width * 1000.0 / 2.0, major))
    return out


def drawn_path_bands(
    parts: list[OomObjectPart],
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    majors: frozenset[int],
) -> list[tuple[list[tuple[float, float]], float, float]]:
    """Kreslené čáry cest (hlavní číslo kódu v ``majors``) v hotové mapě: ``[(osa v S-JTSK, půlka šířky v m, půlka výplně v m)]``.
    Pás čáry o celé šířce je místo, kde by se okraj plochy s čárou překrýval; půlka výplně (mezi okrajovými linkami
    silnice 502.x) je ústí: okraj plochy končí na okrajové lince silnice a navazuje na ni (Petr 2026-10-26)."""
    from app.pipeline.oom_coords import map_to_projected
    from app.pipeline.oom_symbol_map import _symbol_fill_widths, _symbol_widths
    from app.pipeline.oom_symbols import symbol_set_path

    try:
        set_path = symbol_set_path(preset_id, scale)
        info = _symbol_info(set_path)
        widths = _symbol_widths(set_path)
        fills = _symbol_fill_widths(set_path)
    except Exception:
        return []
    out = []
    for part in parts:
        for m in _OBJECT_RE.finditer(part.objects_xml):
            if m.group(2) != "1":
                continue
            sym = int(m.group(3))
            major, kind = info.get(sym, (0, "other"))
            if kind != "line" or major not in majors:
                continue
            cm = _COORDS_RE.search(m.group(4))
            if not cm:
                continue
            pts = _flatten(_parse_coords(cm.group(1)))
            if len(pts) < 2:
                continue
            half_m = widths.get(sym, 0.0) / 2.0 * scale / 1000.0
            fill_m = fills.get(sym, widths.get(sym, 0.0)) / 2.0 * scale / 1000.0
            out.append(
                ([map_to_projected(mx, my, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg) for mx, my in pts], half_m, fill_m)
            )
    return out


@lru_cache(maxsize=8)
def _symbol_codes(symbol_set: Path) -> dict[int, str]:
    """``{index symbolu: kód}`` ze sady (celý kód, např. ``501.1``)."""
    import xml.etree.ElementTree as ET

    root = ET.parse(symbol_set).getroot()
    return {
        int(el.get("id")): str(el.get("code") or "")
        for el in root.iter()
        if el.tag.rsplit("}", 1)[-1] == "symbol" and el.get("id") is not None
    }


# Bezobrysová výplň zpevněné plochy, která se pod olivou nekreslí (okraj 501.2 zůstává).
PAVED_FILL_CODES = frozenset({"501.1"})
# Zbytek výplně menší než tento čtverec (mm na mapě) se zahodí.
PAVED_FILL_MIN_MM = 0.3
# Výplň ustoupí jen menší olivě (zahrada, oplocení) do této výměry v terénu (m²); ve velkých areálech zůstane vidět
# (vnitřní ulice ateliérů Barrandov, Petr 2026-10-25).
PAVED_FILL_OLIVE_MAX_M2 = 5000.0


def paved_fill_outside_olive(parts: list[OomObjectPart], *, preset_id: str, scale: int) -> list[OomObjectPart]:
    """Les a MTBO: výplň zpevněných ploch (501.1) se ořízne o kreslenou olivu (520) do ``PAVED_FILL_OLIVE_MAX_M2``.

    Výplň 501.1 leží v sadě nad olivou, takže pěší plocha nebo parkoviště uvnitř oplocení olivu
    „vyřízne“. Plocha uvnitř olivy se kreslí jen okrajem 501.2 (vstup může být povolený), oliva
    zůstává celá (Petr 2026-10-25). Sprint beze změny.
    """
    if preset_id.startswith("sprint") or not parts:
        return parts
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    from app.pipeline.oom_symbols import symbol_set_path

    try:
        set_path = symbol_set_path(preset_id, scale)
        info = _symbol_info(set_path)
        codes = _symbol_codes(set_path)
    except Exception:  # kosmetika, mapa se kreslí i bez ní
        return parts
    fill_ids = {i for i, c in codes.items() if c in PAVED_FILL_CODES}
    olive_ids = {i for i, (major, kind) in info.items() if major == OLIVE_MAJOR and kind == "area"}
    if not fill_ids or not olive_ids:
        return parts
    olive = []
    for part in parts:
        for m in _OBJECT_RE.finditer(part.objects_xml):
            if m.group(2) == "1" and int(m.group(3)) in olive_ids:
                cm = _COORDS_RE.search(m.group(4))
                poly = _polygon(_rings(_parse_coords(cm.group(1)))) if cm else None
                if poly is not None and not poly.is_empty:
                    olive.append(poly)
    # Souvislé olivy (sousední objekty sloučené) jen do PAVED_FILL_OLIVE_MAX_M2; 1 m v terénu = 1e6 / scale µm mapy.
    max_area = PAVED_FILL_OLIVE_MAX_M2 * (1e6 / scale) ** 2
    merged = unary_union(olive) if olive else None
    olive = [g for g in getattr(merged, "geoms", [merged]) if isinstance(g, Polygon) and g.area <= max_area] if merged is not None else []
    if not olive:
        return parts
    from shapely import STRtree

    tree = STRtree(olive)
    min_area = (PAVED_FILL_MIN_MM * 1000.0) ** 2
    min_half = PAVED_FILL_MIN_MM * 1000.0 / 2.0

    def clip_fill(match: re.Match) -> str:
        head, otype, sym, body, tail = match.groups()
        if otype != "1" or int(sym) not in fill_ids:
            return match.group(0)
        cm = _COORDS_RE.search(body)
        if not cm:
            return match.group(0)
        try:
            poly = _polygon(_rings(_parse_coords(cm.group(1))))
            if poly is None or poly.is_empty:
                return match.group(0)
            ks = [int(k) for k in tree.query(poly, predicate="intersects")]
            if not ks:
                return match.group(0)
            new = poly.difference(unary_union([olive[k] for k in ks]))
            if new.area >= poly.area - 1.0:
                return match.group(0)
            pieces = [
                g
                for g in getattr(new, "geoms", [new])
                if isinstance(g, Polygon) and g.area >= min_area and not g.buffer(-min_half).is_empty
            ]
            out = []
            for g in (g.simplify(EPS) for g in pieces):
                out.extend(_rounded_pieces(g))
            return "\n".join(head + body[: cm.start()] + _format_coords(p) + body[cm.end() :] + tail for p in out)
        except Exception:  # jeden objekt se nepovede → zůstane, jak byl
            return match.group(0)

    out_parts = []
    for part in parts:
        xml = _OBJECT_RE.sub(clip_fill, part.objects_xml)
        if xml == part.objects_xml:
            out_parts.append(part)
        else:
            count = len(_OBJECT_RE.findall(xml))
            if count:
                out_parts.append(OomObjectPart(name=part.name, objects_xml=xml, count=count))
    return out_parts


def displace_objects_from_paths(
    parts: list[OomObjectPart],
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    path_source: str = "mixed",
    max_shift_mm: float | None = None,
) -> list[OomObjectPart]:
    """Objekty mapy odsunuté od kreslených cest (les a MTBO); sprint a mapa bez cest beze změny."""
    if preset_id.startswith("sprint") or not parts:
        return parts
    from app.pipeline.dtm_import import NONSPRINT_AVOID_MAX_SHIFT_MM, drawn_path_axes
    from app.pipeline.oom_symbol_map import _symbol_widths
    from app.pipeline.oom_symbols import symbol_set_path

    try:
        axes = drawn_path_axes(work_dir, path_source, preset_id=preset_id, scale=scale)
        set_path = symbol_set_path(preset_id, scale)
        info = _symbol_info(set_path)
        widths = _symbol_widths(set_path)
        point_boxes = _point_symbol_boxes(set_path)
    except Exception:  # odsun je kosmetika, mapa se kreslí i bez něj
        return parts

    def to_map(coords):
        return [
            projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg)
            for x, y in coords
        ]

    # Osy cest z hotové mapy (OSM i ZABAGED, silnice, cesty, pěšiny, schody, MTBO 8xx); bez nich z OSM geojsonu.
    map_axes = _drawn_path_objects(parts, info, widths)
    if map_axes:
        paths = _Paths([(c, h) for c, h, _m in map_axes], [m for _c, _h, m in map_axes])
    elif axes:
        paths = _Paths([(to_map(c), width_mm * 1000.0 / 2.0) for c, width_mm in axes])
    else:
        return parts
    max_shift = (NONSPRINT_AVOID_MAX_SHIFT_MM if max_shift_mm is None else max_shift_mm) * 1000.0
    step = DENSIFY_MM * 1000.0
    min_half = MIN_AREA_WIDTH_MM * 1000.0 / 2.0
    from app.pipeline.osm_paths import BUILDING_MIN_AREA_MM2

    building_min_area = BUILDING_MIN_AREA_MM2 * 1e6  # mm² mapy → µm²
    # Zpevněné plochy z mapy (501.x): sráz na nich nemá co dělat (parkoviště u tramvaje, Barrandov).
    paved_polys = []
    for part in parts:
        for m in _OBJECT_RE.finditer(part.objects_xml):
            if m.group(2) != "1" or info.get(int(m.group(3)), (0, ""))[0] != PAVED_MAJOR or info[int(m.group(3))][1] != "area":
                continue
            cm = _COORDS_RE.search(m.group(4))
            poly = _polygon(_rings(_parse_coords(cm.group(1)))) if cm else None
            if poly is not None and not poly.is_empty:
                paved_polys.append(poly)
    try:
        from shapely.ops import unary_union as _uu_paved

        paved_union = _uu_paved(paved_polys) if paved_polys else None
    except Exception:
        paved_union = None

    def move_object(match: re.Match) -> str:
        from shapely.geometry import Point
        from shapely.geometry import box as shp_box

        head, otype, sym, body, tail = match.groups()
        major, kind = info.get(int(sym), (0, "other"))
        if major not in MOVED_MAJORS and major not in CLIFF_MAJORS:
            return match.group(0)
        cm = _COORDS_RE.search(body)
        if not cm:
            return match.group(0)
        pts = _parse_coords(cm.group(1))
        if not pts:
            return match.group(0)
        gap = (OLIVE_GAP_MM if major == OLIVE_MAJOR else BLACK_GAP_MM) * 1000.0

        def rebuilt(new_pts):
            return head + body[: cm.start()] + _format_coords(new_pts) + body[cm.end() :] + tail

        try:
            if major in CLIFF_MAJORS:
                if otype != "1" or kind != "line":
                    return match.group(0)
                extra_l = widths.get(int(sym), 0.0) * 1000.0 / 2.0 + BLACK_GAP_MM * 1000.0
                extra_t = extra_l + CLIFF_TICK_MM * 1000.0
                shift_c = max(max_shift, CLIFF_MAX_SHIFT_MM * 1000.0)  # sráz radši posunout dál než zahodit (Petr: „do lesa posunul“)
                pieces = _clear_cliff(pts, paths, extra_l, extra_t, shift_c, step, CLIFF_MIN_PIECE_MM * 1000.0, paved_union)
                if pieces is None:
                    return match.group(0)
                return "\n".join(rebuilt(p) for p in pieces)
            if otype == "0" or kind == "point":
                if len(pts) != 1:
                    return match.group(0)
                box = point_boxes.get(int(sym)) if major == BUILDING_MAJOR else None
                if box is not None:
                    # bodová budova: čtverec podle skutečné velikosti symbolu (osy symbolu = osy mapy, bez rotace)
                    x0, y0 = pts[0][0], pts[0][1]
                    v = _shift_point_box(x0, y0, box, paths, gap, max_shift)
                    if v is None:
                        if paths.majors:
                            geom = shp_box(x0 + box[0], y0 + box[1], x0 + box[2], y0 + box[3])
                            ks = [k for k in paths.near(geom, 0.0) if paths.majors[k] in ROAD_MAJORS]
                            if ks and geom.intersects(paths.corridor(ks, 0.0)):
                                return ""  # nevejde se vedle silnice → nekreslí se (silnice mají přednost)
                        return match.group(0)
                else:
                    v = _shift_vector(Point(pts[0][0], pts[0][1]), paths, POINT_RADIUS_MM * 1000.0 + gap, max_shift)
                    if v is None:
                        return match.group(0)
                return rebuilt([[round(pts[0][0] + v[0]), round(pts[0][1] + v[1]), pts[0][2]]])
            if otype != "1":
                return match.group(0)
            extra = widths.get(int(sym), 0.0) * 1000.0 / 2.0 + gap
            if kind == "area" and major == OLIVE_MAJOR:
                pieces = _clip_olive(pts, paths, extra, min_half)
                if pieces is None:  # oliva zůstává celá → aspoň ji nesmí překrýt silnice (bez pravidla o podílu výměry)
                    pieces = _clip_by_roads(pts, paths, extra, min_half, 0.0)
                if pieces is None:
                    return match.group(0)
                return "\n".join(rebuilt(p) for p in pieces)
            if kind == "area":
                poly = _polygon(_rings(pts))
                if poly is None or poly.is_empty:
                    return match.group(0)
                v = _shift_vector(poly, paths, extra, max_shift)
                if v is None:
                    if major != BUILDING_MAJOR:
                        return match.group(0)
                    # budovu nejde odsunout → ořízne se o pás silnic (silnice mají přednost), kus pod minimum se zahodí
                    pieces = _clip_by_roads(pts, paths, extra, MIN_BUILDING_WIDTH_MM * 1000.0 / 2.0, building_min_area)
                    if pieces is None:
                        return match.group(0)
                    return "\n".join(rebuilt(p) for p in pieces)
                return rebuilt([[round(x + v[0]), round(y + v[1]), f] for x, y, f in pts])
            new = _move_line(pts, paths, extra, max_shift, step)
            if new is None:  # čáru nejde odsunout → vyřízne se z pásu silnic (silnice mají přednost)
                pieces = _clip_line_by_roads(pts, paths, extra, CLIFF_MIN_PIECE_MM * 1000.0)
                return match.group(0) if pieces is None else "\n".join(rebuilt(p) for p in pieces)
            return rebuilt(new)
        except Exception:  # jeden objekt se nepovede → zůstane, jak byl
            return match.group(0)

    out = []
    for part in parts:
        xml = _OBJECT_RE.sub(move_object, part.objects_xml)
        if xml == part.objects_xml:
            out.append(part)
        else:
            count = len(_OBJECT_RE.findall(xml))
            if count:  # část, ze které nic nezbylo (oliva pod cestou), se vynechá
                out.append(OomObjectPart(name=part.name, objects_xml=xml, count=count))
    return out


# Sprint: cesty v budovách a kryté průchody domem.
# Kreslené čáry cest (zpevněné pěšiny / ulice 501.1x a 501.3x, 505–508, schody 532).
SPRINT_PATH_LINE_CODES = frozenset({"501.10", "501.11", "501.12", "501.13", "501.30", "501.31", "501.32", "501.33"})
SPRINT_PATH_LINE_MAJORS = frozenset({502, 503, 504, 505, 506, 507, 508, 532})
BUILDING_CODE = "521.0"
PASSAGE_CODE = "522.0"
# Čára cesty se v budově nekreslí dál než tolik m od obrysu (u kratší cesty podél zdi zůstane).
SPRINT_BUILDING_INNER_M = 1.0
# Průchod domem: nejmenší šířka pásu (m v terénu), kratší úsek v budově (m) se nekreslí; rezerva při hledání čáry cesty v pásu (m).
SPRINT_PASSAGE_MIN_WIDTH_M = 2.0
SPRINT_PASSAGE_MIN_LEN_M = 1.0
SPRINT_PASSAGE_CUT_MARGIN_M = 0.5
# Zbytek čáry cesty po ořezu kratší než tolik µm na mapě se zahodí.
SPRINT_PATH_PIECE_MIN_UM = 100.0
# Zbytek budovy po vyříznutí průchodu s menší výměrou (µm² na mapě, čtverec 0,3 mm) se zahodí.
MIN_BUILDING_PIECE_UM2 = 300.0**2


def sprint_passage_geometry(buildings, covered_lines, half_widths, *, scale: int):
    """Pásy průchodů domem (shapely, v souřadnicích mapy): části ``covered_lines`` uvnitř ``buildings``
    rozšířené na šířku ``max(2 m, kresba cesty)``; ``half_widths`` je půlka šířky kresby u každé čáry (µm)."""
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    m = 1e6 / scale  # µm na mapě na 1 m v terénu
    strips = []
    for line, half in zip(covered_lines, half_widths):
        if len(line) < 2:
            continue
        inside = LineString(line).intersection(buildings)
        for piece in getattr(inside, "geoms", [inside]):
            if piece.geom_type != "LineString" or piece.length < SPRINT_PASSAGE_MIN_LEN_M * m:
                continue
            strips.append(piece.buffer(max(half, SPRINT_PASSAGE_MIN_WIDTH_M * m / 2.0), cap_style="flat").intersection(buildings))
    return unary_union(strips) if strips else None


def sprint_path_cut_zone(buildings, strips, *, scale: int):
    """Zóna, ve které se čára cesty v budově nekreslí: vnitřek budov dál než 1 m od obrysu + pásy průchodů."""
    from shapely.ops import unary_union

    m = 1e6 / scale
    zones = [buildings.buffer(-SPRINT_BUILDING_INNER_M * m)]
    if strips is not None and not strips.is_empty:
        zones.append(strips.buffer(SPRINT_PASSAGE_CUT_MARGIN_M * m).intersection(buildings))
    return unary_union(zones)


def sprint_building_passages(
    parts: list[OomObjectPart],
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
) -> list[OomObjectPart]:
    """Sprint: čáry cest uvnitř budov se nekreslí; kryté průchody (OSM ``covered=yes`` / ``tunnel=building_passage``,
    ``covered_passages.geojson``) se v budově kreslí jako zastřešení 522 v šířce kresby cesty (min. 2 m) a o jejich pás
    se ořízne budova 521. Les, MTBO a mapa bez budov beze změny."""
    if not preset_id.startswith("sprint") or not parts:
        return parts
    import json

    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union

    from app.pipeline.oom_import import _area_object_with_holes
    from app.pipeline.oom_symbol_map import _symbol_widths
    from app.pipeline.oom_symbols import symbol_set_path

    try:
        set_path = symbol_set_path(preset_id, scale)
        info = _symbol_info(set_path)
        codes = _symbol_codes(set_path)
        widths = _symbol_widths(set_path)
    except Exception:  # kosmetika, mapa se kreslí i bez ní
        return parts
    building_ids = {i for i, c in codes.items() if c == BUILDING_CODE}
    passage_ids = [i for i, c in codes.items() if c == PASSAGE_CODE]
    path_ids = {
        i
        for i, c in codes.items()
        if info.get(i, (0, ""))[1] == "line" and (c in SPRINT_PATH_LINE_CODES or info[i][0] in SPRINT_PATH_LINE_MAJORS)
    }
    if not building_ids or not path_ids:
        return parts
    try:
        polys = []
        axes: list[tuple[LineString, float]] = []  # kreslené cesty: osa, půlka šířky (µm)
        for part in parts:
            for m in _OBJECT_RE.finditer(part.objects_xml):
                if m.group(2) != "1":
                    continue
                sym = int(m.group(3))
                cm = _COORDS_RE.search(m.group(4))
                if not cm:
                    continue
                if sym in building_ids:
                    poly = _polygon(_rings(_parse_coords(cm.group(1))))
                    if poly is not None and not poly.is_empty:
                        polys.append(poly)
                elif sym in path_ids:
                    pts = _flatten(_parse_coords(cm.group(1)))
                    if len(pts) >= 2:
                        axes.append((LineString(pts), widths.get(sym, 0.0) * 1000.0 / 2.0))
        if not polys:
            return parts
        buildings = unary_union(polys)
        covered: list[list[tuple[float, float]]] = []
        gj = work_dir / "osm_paths" / "covered_passages.geojson"
        if gj.is_file():
            for feat in json.loads(gj.read_text(encoding="utf-8")).get("features") or []:
                coords = (feat.get("geometry") or {}).get("coordinates") or []
                if len(coords) >= 2:
                    covered.append(
                        [
                            projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg)
                            for x, y in coords
                        ]
                    )
        # Šířka průchodu = kresba nejbližší cesty (do 3 m od osy průchodu), jinak jen minimum.
        near = 3.0 * 1e6 / scale
        halves = []
        for line in covered:
            ls = LineString(line)
            halves.append(max((h for g, h in axes if g.distance(ls) <= near), default=0.0))
        strips = sprint_passage_geometry(buildings, covered, halves, scale=scale) if covered else None
        zone = sprint_path_cut_zone(buildings, strips, scale=scale)
    except Exception:  # kosmetika, mapa se kreslí i bez ní
        return parts
    has_strips = strips is not None and not strips.is_empty

    def edit(match: re.Match) -> str:
        head, otype, sym, body, tail = match.groups()
        if otype != "1":
            return match.group(0)
        sym = int(sym)
        cm = _COORDS_RE.search(body)
        if not cm:
            return match.group(0)

        def rebuilt(new_pts):
            return head + body[: cm.start()] + _format_coords(new_pts) + body[cm.end() :] + tail

        try:
            if sym in path_ids:
                line = LineString(_flatten(_parse_coords(cm.group(1))))
                if line.is_empty or not line.intersects(zone):
                    return match.group(0)
                rest = line.difference(zone)
                pieces = [g for g in getattr(rest, "geoms", [rest]) if g.geom_type == "LineString" and g.length >= SPRINT_PATH_PIECE_MIN_UM]
                return "\n".join(rebuilt([[round(x), round(y), 0] for x, y in g.coords]) for g in pieces)
            if has_strips and sym in building_ids:
                poly = _polygon(_rings(_parse_coords(cm.group(1))))
                if poly is None or poly.is_empty or not poly.intersects(strips):
                    return match.group(0)
                new = poly.difference(strips)
                out = []
                for g in getattr(new, "geoms", [new]):
                    if isinstance(g, Polygon) and g.area >= MIN_BUILDING_PIECE_UM2:
                        out.extend(_rounded_pieces(g.simplify(EPS)))
                return "\n".join(rebuilt(p) for p in out)
        except Exception:  # jeden objekt se nepovede → zůstane, jak byl
            return match.group(0)
        return match.group(0)

    out_parts = []
    for part in parts:
        xml = _OBJECT_RE.sub(edit, part.objects_xml)
        if xml == part.objects_xml:
            out_parts.append(part)
        else:
            count = len(_OBJECT_RE.findall(xml))
            if count:
                out_parts.append(OomObjectPart(name=part.name, objects_xml=xml, count=count))
    if has_strips and passage_ids:
        objs = []
        for g in getattr(strips, "geoms", [strips]):
            if isinstance(g, Polygon) and not g.is_empty:
                ring = [(round(x), round(y)) for x, y in g.simplify(EPS).exterior.coords]
                obj = _area_object_with_holes(passage_ids[0], [ring])
                if obj:
                    objs.append(obj)
        if objs:
            out_parts.append(OomObjectPart(name="Sprint – kryté průchody domem", objects_xml="\n".join(objs), count=len(objs)))
    return out_parts


# Sprint: slučování navazujících budov (zavírání mezer užších než 0,4 mm mapy, zalití dvorků).
SPRINT_BUILDING_GAP_MM = 0.4
# Mezera / dvůr, kterým vede kreslená čára cesty, se nezavírá: pás kolem čáry (m v terénu).
SPRINT_BUILDING_PATH_BAND_M = 0.5
# Díra ve sloučeném bloku menší než tolik µm² na mapě (1 mm²) zmizí vždy.
SPRINT_BUILDING_HOLE_MIN_UM2 = 1000.0**2
# Dvůr bez cesty se zaleje vždy (ISSprOM 521: plocha zcela uzavřená budovami je součást budovy, Petr 2026-10-28);
# kreslené plochy (oliva, zpevněná, otevřená) se o zalitý dvůr oříznou, ať nezůstanou čtverce na budově.
SPRINT_BUILDING_HOLE_FILL_MAJORS = frozenset({401, 402, 403, 404, 501, 520, 527})


def sprint_merge_buildings(
    parts: list[OomObjectPart],
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
) -> list[OomObjectPart]:
    """Sprint: budovy (521) ze všech částí (OSM, DTM, ZABAGED) se sjednotí a mezery užší než 0,4 mm mapy se zavřou
    (morfologické uzavření). Mezera, kterou vede kreslená čára cesty, zůstane (pás 0,5 m kolem čáry). Díra ve sloučeném
    bloku (dvůr) bez cesty se zaplní (ISSprOM 521) a kreslené plochy (oliva, 501, otevřená) se o něj oříznou; díra
    s cestou zůstane, díry pod 1 mm² mapy zmizí vždy. Výsledek je v části
    s první budovou. Les, MTBO a mapa bez budov beze změny."""
    if not preset_id.startswith("sprint") or not parts:
        return parts
    from shapely import STRtree
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union

    from app.pipeline.oom_symbols import symbol_set_path

    try:
        set_path = symbol_set_path(preset_id, scale)
        info = _symbol_info(set_path)
        codes = _symbol_codes(set_path)
    except Exception:  # kosmetika, mapa se kreslí i bez ní
        return parts
    building_ids = {i for i, c in codes.items() if c == BUILDING_CODE}
    path_ids = {
        i
        for i, c in codes.items()
        if info.get(i, (0, ""))[1] == "line" and (c in SPRINT_PATH_LINE_CODES or info[i][0] in SPRINT_PATH_LINE_MAJORS)
    }
    if not building_ids:
        return parts
    try:
        polys = []
        lines = []
        template = None  # první objekt budovy (hlavička, tělo, konec)
        for part in parts:
            for m in _OBJECT_RE.finditer(part.objects_xml):
                if m.group(2) != "1":
                    continue
                sym = int(m.group(3))
                cm = _COORDS_RE.search(m.group(4))
                if not cm:
                    continue
                if sym in building_ids:
                    poly = _polygon(_rings(_parse_coords(cm.group(1))))
                    if poly is not None and not poly.is_empty:
                        polys.append(poly)
                        if template is None:
                            template = m
                elif sym in path_ids:
                    pts = _flatten(_parse_coords(cm.group(1)))
                    if len(pts) >= 2:
                        lines.append(LineString(pts))
        if not polys:
            return parts
        union = unary_union(polys)
        half_gap = SPRINT_BUILDING_GAP_MM * 1000.0 / 2.0
        closed = union.buffer(half_gap, join_style="mitre").buffer(-half_gap, join_style="mitre")
        if lines:
            band = unary_union(lines).buffer(SPRINT_BUILDING_PATH_BAND_M * 1e6 / scale)
            closed = closed.difference(band).union(union)
        tree = STRtree(lines) if lines else None
        merged = []
        filled = []  # zalité dvory – kreslené plochy se o ně oříznou
        for g in getattr(closed, "geoms", [closed]):
            if not isinstance(g, Polygon) or g.is_empty:
                continue
            keep = []
            for hole in g.interiors:
                hp = Polygon(hole)
                if hp.area < SPRINT_BUILDING_HOLE_MIN_UM2:
                    filled.append(hp)
                elif tree is not None and len(tree.query(hp, predicate="intersects")):
                    keep.append(hole)
                else:
                    filled.append(hp)
            merged.append(Polygon(g.exterior, keep))
        head, body, tail = template.group(1), template.group(4), template.group(5)
        cm0 = _COORDS_RE.search(body)
        new_objs = [
            head + body[: cm0.start()] + _format_coords(p) + body[cm0.end() :] + tail
            for g in merged
            for p in _rounded_pieces(g.simplify(EPS))
        ]
        filled_union = unary_union(filled) if filled else None
    except Exception:  # kosmetika, mapa se kreslí i bez ní
        return parts
    if not new_objs:
        return parts
    placed = False
    fill_ids = {i for i, (major, kind) in info.items() if kind == "area" and major in SPRINT_BUILDING_HOLE_FILL_MAJORS}

    def cut_fill(match: re.Match) -> str:
        """Kreslená plocha zasahující do zalitého dvora se o něj ořízne (celá uvnitř zmizí)."""
        cm = _COORDS_RE.search(match.group(4))
        if filled_union is None or not cm:
            return match.group(0)
        try:
            poly = _polygon(_rings(_parse_coords(cm.group(1))))
            if poly is None or poly.is_empty or not poly.intersects(filled_union):
                return match.group(0)
            rest = poly.difference(filled_union)
        except Exception:
            return match.group(0)
        head, body, tail = match.group(1), match.group(4), match.group(5)
        return "\n".join(
            head + body[: cm.start()] + _format_coords(p) + body[cm.end() :] + tail
            for g in getattr(rest, "geoms", [rest])
            if isinstance(g, Polygon) and g.area >= SPRINT_BUILDING_HOLE_MIN_UM2
            for p in _rounded_pieces(g.simplify(EPS))
        )

    def edit(match: re.Match) -> str:
        nonlocal placed
        if match.group(2) != "1":
            return match.group(0)
        sym = int(match.group(3))
        if sym in fill_ids:
            return cut_fill(match)
        if sym not in building_ids:
            return match.group(0)
        if placed:
            return ""
        placed = True
        return "\n".join(new_objs)

    out_parts = []
    for part in parts:
        xml = _OBJECT_RE.sub(edit, part.objects_xml)
        if xml == part.objects_xml:
            out_parts.append(part)
            continue
        count = len(_OBJECT_RE.findall(xml))
        if count:
            out_parts.append(OomObjectPart(name=part.name, objects_xml=xml, count=count))
    return out_parts


# Sprint: kupky (109), prohlubně (111) a jámy (112) jen v lese a na loukách.
SPRINT_KNOLL_MAJORS = frozenset({109, 111, 112})
SPRINT_KNOLL_BUILDING_MAJORS = frozenset({521, 522})
SPRINT_KNOLL_TOWN_AREA_MAJORS = frozenset({501, 520, 527})
SPRINT_KNOLL_BUILDING_DIST_M = 5.0


def sprint_knolls_outside_town(parts: list[OomObjectPart], *, preset_id: str, scale: int) -> list[OomObjectPart]:
    """Sprint: kupky, prohlubně a jámy (109, 111, 112) uvnitř nebo do 5 m od budovy 521 / zastřešení 522, nebo uvnitř
    zpevněné plochy (501) či olivy (520, 527) se nekreslí. DMR 5G je terén bez budov; v zástavbě a úzkých ulicích je
    dopočítaný z mála bodů, takže u obrysů domů vznikají falešné kupky a prohlubně. V lese a na loukách zůstávají.
    Les a MTBO beze změny."""
    if not preset_id.startswith("sprint") or not parts:
        return parts
    from shapely.geometry import Point
    from shapely.ops import unary_union
    from shapely.prepared import prep

    from app.pipeline.oom_symbols import symbol_set_path

    try:
        info = _symbol_info(symbol_set_path(preset_id, scale))
    except Exception:  # kosmetika, mapa se kreslí i bez ní
        return parts
    knoll_ids = {i for i, (major, kind) in info.items() if major in SPRINT_KNOLL_MAJORS and kind == "point"}
    if not knoll_ids:
        return parts
    near = SPRINT_KNOLL_BUILDING_DIST_M * 1e6 / scale
    try:
        zones = []
        for part in parts:
            for m in _OBJECT_RE.finditer(part.objects_xml):
                if m.group(2) != "1":
                    continue
                major, kind = info.get(int(m.group(3)), (0, "other"))
                if kind != "area" or not (major in SPRINT_KNOLL_BUILDING_MAJORS or major in SPRINT_KNOLL_TOWN_AREA_MAJORS):
                    continue
                cm = _COORDS_RE.search(m.group(4))
                poly = _polygon(_rings(_parse_coords(cm.group(1)))) if cm else None
                if poly is None or poly.is_empty:
                    continue
                zones.append(poly.buffer(near) if major in SPRINT_KNOLL_BUILDING_MAJORS else poly)
        if not zones:
            return parts
        town = prep(unary_union(zones))
    except Exception:  # kosmetika, mapa se kreslí i bez ní
        return parts

    def edit(match: re.Match) -> str:
        if int(match.group(3)) not in knoll_ids:
            return match.group(0)
        try:
            cm = _COORDS_RE.search(match.group(4))
            pts = _parse_coords(cm.group(1)) if cm else []
            if len(pts) == 1 and town.intersects(Point(pts[0][0], pts[0][1])):
                return ""
        except Exception:  # jeden objekt se nepovede → zůstane, jak byl
            pass
        return match.group(0)

    out_parts = []
    for part in parts:
        xml = _OBJECT_RE.sub(edit, part.objects_xml)
        if xml == part.objects_xml:
            out_parts.append(part)
            continue
        count = len(_OBJECT_RE.findall(xml))
        if count:
            out_parts.append(OomObjectPart(name=part.name, objects_xml=xml, count=count))
    return out_parts
