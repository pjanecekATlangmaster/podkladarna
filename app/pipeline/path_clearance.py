"""Odsun objektů od kreslených cest v lese a MTBO (pozdní krok buildu, nad hotovými objekty mapy).

Symboly cest jsou v lese a MTBO na mapě širší než cesta v terénu, takže se jimi překrývají
budovy, oliva, zdi, ploty i bodové objekty kolem. Po sestavení všech objektů (ploty už spojené,
deduplikované a orientované) se proto body objektů odsunou od os cest tak, aby mezi okrajem
symbolu cesty a objektem zbyla mezera: od olivy minimální, od objektů s černou kresbou o něco
větší. Pracuje se v souřadnicích mapy (1 jednotka = 1 µm), šířky symbolů se berou ze sady.

Vegetace (les, louka, houštiny …), terén, voda, cesty a zpevněné plochy se nemění. Plocha,
která by se odsunem zmenšila pod minimální rozměr (nebo o víc než polovinu), zůstane, jak byla.
Sprint se nemění.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from pathlib import Path

from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import MAP_COORD_CLOSE_POINT, MAP_COORD_CURVE_START, OomObjectPart, _fmt

# Odsouvané symboly (hlavní číslo kódu v sadě; ISOM 2017 i ISMTBOM 2022 číslují umělé objekty stejně).
MOVED_MAJORS = frozenset({513, 514, 515, 516, 517, 518, 520, 521, 522, 523, 524, 525, 526, 527, 528, 529, 530, 531})
OLIVE_MAJOR = 520
# Mezera mezi okrajem symbolu cesty a objektem (mm na mapě): oliva / ostatní (černá kresba).
OLIVE_GAP_MM = 0.05
BLACK_GAP_MM = 0.2
# Bodové objekty: poloměr kresby (mm na mapě).
POINT_RADIUS_MM = 0.3
# Plocha musí po odsunu mít aspoň tuto šířku (mm na mapě) a polovinu původní výměry, jinak se nemění.
MIN_AREA_WIDTH_MM = 0.4
MIN_AREA_SHARE = 0.5
# Zhuštění rovných úseků (mm na mapě) a tolerance pro zahození nepotřebných vložených bodů (µm).
DENSIFY_MM = 0.2
COLLINEAR_TOL = 5.0

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

    def displacement(self, x, y, extra, max_shift, crossings=None):
        from shapely.geometry import Point

        pt = Point(x, y)
        best = None
        for k in self.tree.query(pt.buffer(self.reach + extra)):
            k = int(k)
            need = self.half[k] + extra
            if crossings and k in crossings and crossings[k].distance(pt) <= need:
                continue
            g = self.geoms[k]
            deficit = need - g.distance(pt)
            if deficit <= 1e-6 or deficit > max_shift:
                continue
            q = g.interpolate(g.project(pt))
            vx, vy = x - q.x, y - q.y
            n = math.hypot(vx, vy)
            if n < 1e-6:
                continue
            if best is None or deficit > best[0]:
                best = (deficit, vx / n * deficit, vy / n * deficit)
        return (best[1], best[2]) if best else (0.0, 0.0)

    def crossings(self, geom):
        out = {}
        for k in self.tree.query(geom):
            hit = geom.intersection(self.geoms[int(k)])
            if not hit.is_empty:
                out[int(k)] = hit
        return out


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


def _move_ring(ring, paths: _Paths, extra, max_shift, step):
    """Body prstence / linie odsunuté od cest (rovné úseky zhuštěné); vložené body navíc se zahodí."""
    from shapely.geometry import LineString

    curved = any(p[2] & MAP_COORD_CURVE_START for p in ring)
    dense: list[tuple[list[int], bool]] = []  # (bod, vložený)
    for i, p in enumerate(ring):
        dense.append((p, False))
        if curved or i + 1 >= len(ring):
            continue
        q = ring[i + 1]
        n = int(math.hypot(q[0] - p[0], q[1] - p[1]) // step)
        for j in range(1, n + 1):
            t = j / (n + 1)
            dense.append(([round(p[0] + (q[0] - p[0]) * t), round(p[1] + (q[1] - p[1]) * t), 0], True))
    geom = LineString([(p[0], p[1]) for p, _ins in dense]) if len(dense) >= 2 else None
    crossings = paths.crossings(geom) if geom is not None else None
    moved = []
    changed = False
    for p, ins in dense:
        dx, dy = paths.displacement(p[0], p[1], extra, max_shift, crossings)
        if dx or dy:
            changed = True
        moved.append(([round(p[0] + dx), round(p[1] + dy), p[2]], ins))
    if not changed:
        return ring, False
    keep = [moved[0]]
    for i in range(1, len(moved) - 1):
        p, ins = moved[i]
        if ins:
            a, b = keep[-1][0], moved[i + 1][0]
            if _dist_to_segment(p, a, b) <= COLLINEAR_TOL:
                continue
        keep.append(moved[i])
    if len(moved) > 1:
        keep.append(moved[-1])
    return [p for p, _ins in keep], True


def _dist_to_segment(p, a, b) -> float:
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    if L <= 0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


def _polygon(rings):
    from shapely.geometry import Polygon

    try:
        shell = [(p[0], p[1]) for p in rings[0]]
        holes = [[(p[0], p[1]) for p in r] for r in rings[1:] if len(r) >= 3]
        poly = Polygon(shell, holes)
        return poly if poly.is_valid else poly.buffer(0)
    except (ValueError, IndexError):
        return None


def _area_ok(old_rings, new_rings, min_half) -> bool:
    old, new = _polygon(old_rings), _polygon(new_rings)
    if old is None or new is None or new.is_empty or old.area <= 0:
        return False
    if new.area < MIN_AREA_SHARE * old.area:
        return False
    if not old.buffer(-min_half).is_empty and new.buffer(-min_half).is_empty:
        return False
    return True


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
    if not axes:
        return parts

    def to_map(coords):
        return [
            projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg)
            for x, y in coords
        ]

    paths = _Paths([(to_map(c), width_mm * 1000.0 / 2.0) for c, width_mm in axes])
    max_shift = (NONSPRINT_AVOID_MAX_SHIFT_MM if max_shift_mm is None else max_shift_mm) * 1000.0
    step = DENSIFY_MM * 1000.0
    min_half = MIN_AREA_WIDTH_MM * 1000.0 / 2.0

    def move_object(match: re.Match) -> str:
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
        try:
            if otype == "0" or kind == "point":
                if len(pts) != 1:
                    return match.group(0)
                dx, dy = paths.displacement(pts[0][0], pts[0][1], POINT_RADIUS_MM * 1000.0 + gap, max_shift)
                if not (dx or dy):
                    return match.group(0)
                new = [[round(pts[0][0] + dx), round(pts[0][1] + dy), pts[0][2]]]
            elif otype == "1":
                half = widths.get(int(sym), 0.0) * 1000.0 / 2.0
                extra = half + gap
                rings = _rings(pts) if kind == "area" else [pts]
                new_rings, changed = [], False
                for ring in rings:
                    r, ch = _move_ring(ring, paths, extra, max_shift, step)
                    new_rings.append(r)
                    changed = changed or ch
                if not changed:
                    return match.group(0)
                if kind == "area" and not _area_ok(rings, new_rings, min_half):
                    return match.group(0)
                if kind != "area" and len(new_rings[0]) < 2:
                    return match.group(0)
                new = [p for r in new_rings for p in r]
            else:
                return match.group(0)
        except Exception:  # jeden objekt se nepovede → zůstane, jak byl
            return match.group(0)
        return head + body[: cm.start()] + _format_coords(new) + body[cm.end() :] + tail

    out = []
    for part in parts:
        xml = _OBJECT_RE.sub(move_object, part.objects_xml)
        out.append(part if xml == part.objects_xml else OomObjectPart(name=part.name, objects_xml=xml, count=part.count))
    return out
