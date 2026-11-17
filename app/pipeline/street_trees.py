"""Sprint v Česku: stromy u ulic bodem místo „bílého lesa“ (Petr 2026-11-17, Most job 4c20a72105c9).

Porost ve sprintu je z výšky porostu (CHM): nízký = 401, vysoký = bez plochy (bílá). Koruna jednoho stromu u silnice
nebo chodníku tak v hotové mapě zůstala bílou dírou v ploše ulice 501 nebo v trávníku 401 – na mapě to vypadá jako
kousek lesa na ulici. Nad hotovou mapou se proto malá bílá díra (do ``HOLE_MAX_M2``), která sousedí se zpevněnou
plochou 501 nebo s čarou chodníku / cesty a jejíž obvod tvoří hlavně 501 a 401, zalije podle převládajícího souseda
(501.1 / 401) a do koruny se dá bod 418 ve vrcholu z CHM (prořídlé na ``MIN_CENTER_MM`` mapy i proti kresleným
417 / 418). Bod, jehož značka by zasáhla vozovku (osa OSM silnice + půlka šířky vozovky jako u pásů ulic), se
nekreslí. Větší bílá plocha (park, remízek) zůstává.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

# Záloha: False = bez výplně korun a bodů (bílé díry zůstanou).
STREET_TREES = True

FILL_PART_NAME = "Koruny u ulic (výplň pod stromem)"
TREE_PART_NAME = "Stromy u ulic (418)"
HOLE_MAX_M2 = 250.0  # koruna jednoho až několika stromů; větší bílá = park, remízek
HOLE_MIN_M2 = 1.0
EDGE_M = 0.3  # díra sousedí s plochou, je-li její obvod do tolika metrů od ní
NEIGHBOUR_SHARE = 0.6  # obvod díry z 501 + 401 aspoň tolik (díra u budovy / vody ne)
PATH_NEAR_M = 2.0  # čára chodníku / cesty do tolika metrů = díra u chodníku
PATH_MAJORS = frozenset({505, 506, 507, 508})
PAVED_MAJOR = 501
OPEN_MAJOR = 401
TREE_CODES = ("417", "418")
TREE_MIN_H_M = 3.0
TREE_SYMBOL_R_MM = 0.4  # poloměr značky 418 s rezervou
MIN_CENTER_MM = 1.0


def _crown_tops(hole_proj, chm, gt) -> list[tuple[float, float, float]]:
    """Vrcholy korun (x, y, výška) uvnitř díry (S-JTSK) z CHM, od nejvyššího."""
    import numpy as np
    import shapely
    from scipy import ndimage

    x0, px, _, y1, _, py = gt
    py = abs(py)
    h, w = chm.shape
    minx, miny, maxx, maxy = hole_proj.bounds
    c0 = max(0, int(math.floor((minx - 2 - x0) / px)))
    c1 = min(w, int(math.ceil((maxx + 2 - x0) / px)))
    r0 = max(0, int(math.floor((y1 - (maxy + 2)) / py)))
    r1 = min(h, int(math.ceil((y1 - (miny - 2)) / py)))
    if c1 - c0 < 1 or r1 - r0 < 1:
        return []
    win = np.nan_to_num(np.asarray(chm[r0:r1, c0:c1], dtype=np.float32), nan=0.0)
    sm = ndimage.gaussian_filter(win, sigma=1.0)
    peaks = (sm == ndimage.maximum_filter(sm, size=5)) & (sm >= TREE_MIN_H_M)
    lab, n = ndimage.label(peaks)
    if n == 0:
        return []
    out = []
    zone = hole_proj.buffer(1.0)
    for rr, cc in ndimage.center_of_mass(peaks, lab, range(1, n + 1)):
        x = x0 + (c0 + cc + 0.5) * px
        y = y1 - (r0 + rr + 0.5) * py
        if shapely.contains_xy(zone, x, y):
            out.append((x, y, float(sm[int(rr), int(cc)])))
    out.sort(key=lambda t: -t[2])
    return out


def _road_axes(work_dir: Path):
    """(STRtree os, [(LineString, půlka vozovky)]) OSM silnic jobu (S-JTSK) nebo (None, [])."""
    from shapely import STRtree
    from shapely.geometry import LineString

    from app.pipeline.street_bands import road_half_width
    from app.pipeline.street_parcels import load_street_osm_lines

    roads = []
    for coords, hw in load_street_osm_lines(work_dir):
        half = road_half_width(hw)
        if half is not None and len(coords) >= 2:
            roads.append((LineString(coords), half))
    if not roads:
        return None, []
    return STRtree([r[0] for r in roads]), roads


def fill_street_crowns(
    parts,
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    log=None,
):
    """Hotová mapa sprintu + výplň malých bílých děr u ulic a body 418 v korunách; jinak / při chybě beze změny."""
    if not STREET_TREES or not str(preset_id).startswith("sprint") or not parts:
        return parts
    from app.pipeline.country_profile import process_uses_cz_sources

    if not process_uses_cz_sources():
        return parts
    try:
        return _fill(parts, Path(work_dir), preset_id=preset_id, scale=scale, ref_x=ref_x, ref_y=ref_y, grivation_deg=grivation_deg, log=log)
    except Exception as exc:  # mapa se kreslí i bez výplně
        logger.warning("street_trees: přeskočeno (%s)", exc)
        if log:
            log(f"Stromy u ulic: přeskočeno ({exc})")
        return parts


def _fill(parts, work_dir: Path, *, preset_id, scale, ref_x, ref_y, grivation_deg, log):
    import time

    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union

    from app.pipeline.oom_coords import map_to_projected, projected_to_map_coord
    from app.pipeline.oom_import import OomObjectPart, _area_object_with_holes, _point_object
    from app.pipeline.oom_symbol_map import symbol_index_for_code
    from app.pipeline.oom_symbols import symbol_set_path
    from app.pipeline.path_clearance import _COORDS_RE, _OBJECT_RE, _flatten, _parse_coords, _polygon, _rings, _symbol_info
    from app.pipeline.tree_rows import _load_chm, _poly_to_projected, thin_points

    t0 = time.monotonic()
    idx_paved = symbol_index_for_code(preset_id, scale, "501.1")
    idx_open = symbol_index_for_code(preset_id, scale, "401")
    idx_tree = symbol_index_for_code(preset_id, scale, "418")
    if idx_paved is None or idx_open is None:
        return parts
    tree_idx = {i for i in (symbol_index_for_code(preset_id, scale, c) for c in TREE_CODES) if i is not None}
    info = _symbol_info(symbol_set_path(preset_id, scale))
    areas, paved, open_, paths = [], [], [], []
    existing: list[tuple[float, float]] = []
    for part in parts:
        for m in _OBJECT_RE.finditer(part.objects_xml):
            sym = int(m.group(3))
            major, kind = info.get(sym, (0, "other"))
            cm = _COORDS_RE.search(m.group(4))
            if not cm:
                continue
            pts = _parse_coords(cm.group(1))
            if not pts:
                continue
            if m.group(2) == "0":
                if sym in tree_idx:
                    existing.append(map_to_projected(pts[0][0], pts[0][1], ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg))
                continue
            if m.group(2) != "1":
                continue
            if kind == "area":
                poly = _polygon(_rings(pts))
                if poly is None or poly.is_empty:
                    continue
                areas.append(poly)
                if major == PAVED_MAJOR:
                    paved.append(poly)
                elif major == OPEN_MAJOR:
                    open_.append(poly)
            elif kind == "line" and major in PATH_MAJORS:
                flat = _flatten(pts)
                if len(flat) >= 2:
                    paths.append(LineString(flat))
    if not areas or not paved:
        return parts
    um = 1e6 / scale  # jednotek mapy na 1 m terénu
    covered = unary_union(areas)
    holes = []
    for g in getattr(covered, "geoms", None) or [covered]:
        if g.geom_type != "Polygon":
            continue
        from shapely.geometry import Polygon

        for ring in g.interiors:
            hole = Polygon(ring)
            if HOLE_MIN_M2 * um * um <= hole.area <= HOLE_MAX_M2 * um * um:
                holes.append(hole)
    if not holes:
        return parts
    paved_u = unary_union(paved).buffer(EDGE_M * um)
    open_u = unary_union(open_).buffer(EDGE_M * um) if open_ else None
    path_u = unary_union(paths).buffer(PATH_NEAR_M * um) if paths else None
    chm = _load_chm(work_dir)
    axes_tree, roads = _road_axes(work_dir)
    r_tree = TREE_SYMBOL_R_MM * scale / 1000.0
    min_center_m = MIN_CENTER_MM * scale / 1000.0
    fills = {idx_paved: [], idx_open: []}
    points: list[tuple[float, float]] = []
    blocked = 0
    for hole in holes:
        ring = hole.exterior
        perim = ring.length
        if perim <= 0:
            continue
        s_paved = ring.intersection(paved_u).length / perim
        s_open = ring.intersection(open_u).length / perim if open_u is not None else 0.0
        near_path = path_u is not None and hole.intersects(path_u)
        if (s_paved <= 0 and not near_path) or s_paved + s_open < NEIGHBOUR_SHARE:
            continue
        sym = idx_paved if s_paved >= s_open else idx_open
        fills[sym].append([(int(round(x)), int(round(y))) for x, y in ring.coords])
        if idx_tree is None or chm is None:
            continue
        hole_proj = _poly_to_projected(hole, ref_x, ref_y, scale, grivation_deg)
        tops = []
        for x, y, _h in _crown_tops(hole_proj, *chm):
            if axes_tree is not None:
                pt = Point(x, y)
                hit = False
                for k in axes_tree.query(pt.buffer(r_tree + 5.0)):
                    ln, half = roads[int(k)]
                    if ln.distance(pt) < half + r_tree:
                        hit = True
                        break
                if hit:
                    blocked += 1
                    continue
            tops.append((x, y))
        points.extend(thin_points(tops, list(existing) + points, min_center_m))
    objects = [_area_object_with_holes(sym, [ring]) for sym, rings in fills.items() for ring in rings]
    n_fill = len(objects)
    if not objects:
        return parts
    out = list(parts) + [OomObjectPart(name=FILL_PART_NAME, objects_xml="\n".join(objects), count=n_fill)]
    if points:
        pts_xml = [
            _point_object(idx_tree, *projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg))
            for x, y in points
        ]
        out.append(OomObjectPart(name=TREE_PART_NAME, objects_xml="\n".join(pts_xml), count=len(pts_xml)))
    if log:
        log(
            f"Stromy u ulic: zalito {len(fills[idx_paved])} korun do 501.1 a {len(fills[idx_open])} do 401, "
            f"bodů 418 {len(points)}, u vozovky vynecháno {blocked} ({'CHM' if chm else 'bez CHM'}, {time.monotonic() - t0:.1f} s)"
        )
    return out
