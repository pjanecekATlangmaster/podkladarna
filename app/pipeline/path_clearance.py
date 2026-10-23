"""Odsun objektů od kreslených cest v lese a MTBO (pozdní krok buildu, nad hotovými objekty mapy).

Symboly cest jsou v lese a MTBO na mapě širší než cesta v terénu, takže se jimi překrývají
budovy, oliva, zdi, ploty i bodové objekty kolem. Po sestavení všech objektů (ploty už spojené,
deduplikované a orientované) se proto objekty odsunou od os cest tak, aby mezi okrajem symbolu
cesty a objektem zbyla mezera: od olivy minimální, od objektů s černou kresbou o něco větší.
Pracuje se v souřadnicích mapy (1 jednotka = 1 µm), šířky symbolů se berou ze sady. Osy cest se
berou z hotové mapy (všechny kreslené cesty bez ohledu na zdroj – OSM i ZABAGED).

- Oliva se ořízne o pás podél cesty (kousky pod minimální šířku se zahodí).
- Ostatní plochy (budovy …) a body se posunou celé, tvar se nemění.
- Čáry (zdi, ploty) se odsunou po bodech; nesmí se tím přiblížit k jiné cestě ani výrazně změnit délku.

Objekt, který cestu kříží nebo by se musel posunout víc než ``NONSPRINT_AVOID_MAX_SHIFT_MM``,
zůstane. Vegetace (les, louka, houštiny …), terén, voda, cesty a zpevněné plochy se nemění.
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
# Mezera mezi okrajem symbolu cesty a objektem (mm na mapě): oliva / ostatní (černá kresba).
OLIVE_GAP_MM = 0.05
BLACK_GAP_MM = 0.2
# Bodové objekty: poloměr kresby (mm na mapě).
POINT_RADIUS_MM = 0.3
# Oliva po ořezu: kousky užší než tato šířka (mm na mapě) se zahodí; zbýt musí aspoň polovina výměry.
MIN_AREA_WIDTH_MM = 0.4
MIN_AREA_SHARE = 0.5
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

    def __init__(self, axes: list[tuple[list[tuple[int, int]], float]]):
        from shapely import STRtree
        from shapely.geometry import LineString

        self.geoms = [LineString(c) for c, _h in axes]
        self.half = [h for _c, h in axes]
        self.tree = STRtree(self.geoms)
        self.reach = max(self.half) if self.half else 0.0

    def near(self, geom, extra):
        return [int(k) for k in self.tree.query(geom.buffer(self.reach + extra))]

    def min_dist(self, geom, ks) -> float:
        return min((self.geoms[k].distance(geom) for k in ks), default=math.inf)

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
    """Olivu ořízne o pás podél cest → seznam ploch (body), None = beze změny."""
    from shapely.geometry import Polygon

    poly = _polygon(_rings(pts))
    if poly is None or poly.is_empty:
        return None
    ks = paths.near(poly, extra)
    if not ks:
        return None
    new = poly.difference(paths.corridor(ks, extra))
    if new.area >= poly.area - 1.0:
        return None
    pieces = [g for g in getattr(new, "geoms", [new]) if isinstance(g, Polygon) and not g.is_empty]
    pieces = [g for g in pieces if not g.buffer(-min_half).is_empty]
    if not pieces or sum(g.area for g in pieces) < MIN_AREA_SHARE * poly.area:
        return None
    out = []
    for g in (g.simplify(EPS) for g in pieces):
        if g.is_empty or not isinstance(g, Polygon):
            continue
        rings = [_ring_coords(ring.coords) for ring in [g.exterior, *g.interiors]]
        # Po zaokrouhlení na celé µm musí plocha zůstat validní, jinak se oliva nechá, jak byla.
        if not Polygon([p[:2] for p in rings[0]], [[p[:2] for p in r] for r in rings[1:]]).is_valid:
            return None
        out.append([p for r in rings for p in r])
    return out or None


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


def _drawn_path_objects(parts, info, widths) -> list[tuple[list[tuple[float, float]], float]]:
    """Čárové objekty cest v mapě: ``[(osa v souřadnicích mapy, půlka šířky symbolu v µm)]``."""
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
                out.append((pts, width * 1000.0 / 2.0))
    return out


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
        paths = _Paths(map_axes)
    elif axes:
        paths = _Paths([(to_map(c), width_mm * 1000.0 / 2.0) for c, width_mm in axes])
    else:
        return parts
    max_shift = (NONSPRINT_AVOID_MAX_SHIFT_MM if max_shift_mm is None else max_shift_mm) * 1000.0
    step = DENSIFY_MM * 1000.0
    min_half = MIN_AREA_WIDTH_MM * 1000.0 / 2.0

    def move_object(match: re.Match) -> str:
        from shapely.geometry import Point

        head, otype, sym, body, tail = match.groups()
        major, kind = info.get(int(sym), (0, "other"))
        if major not in MOVED_MAJORS:
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
            if otype == "0" or kind == "point":
                if len(pts) != 1:
                    return match.group(0)
                v = _shift_vector(Point(pts[0][0], pts[0][1]), paths, POINT_RADIUS_MM * 1000.0 + gap, max_shift)
                if v is None:
                    return match.group(0)
                return rebuilt([[round(pts[0][0] + v[0]), round(pts[0][1] + v[1]), pts[0][2]]])
            if otype != "1":
                return match.group(0)
            extra = widths.get(int(sym), 0.0) * 1000.0 / 2.0 + gap
            if kind == "area" and major == OLIVE_MAJOR:
                pieces = _clip_olive(pts, paths, extra, min_half)
                if pieces is None:
                    return match.group(0)
                return "\n".join(rebuilt(p) for p in pieces)
            if kind == "area":
                poly = _polygon(_rings(pts))
                if poly is None or poly.is_empty:
                    return match.group(0)
                v = _shift_vector(poly, paths, extra, max_shift)
                if v is None:
                    return match.group(0)
                return rebuilt([[round(x + v[0]), round(y + v[1]), f] for x, y, f in pts])
            new = _move_line(pts, paths, extra, max_shift, step)
            return match.group(0) if new is None else rebuilt(new)
        except Exception:  # jeden objekt se nepovede → zůstane, jak byl
            return match.group(0)

    out = []
    for part in parts:
        xml = _OBJECT_RE.sub(move_object, part.objects_xml)
        if xml == part.objects_xml:
            out.append(part)
        else:
            out.append(OomObjectPart(name=part.name, objects_xml=xml, count=len(_OBJECT_RE.findall(xml))))
    return out
