"""Sprint v Česku: stromy u ulic bodem místo „bílého lesa“ (Petr 2026-11-17, Most job 4c20a72105c9).

Porost ve sprintu je z výšky porostu (CHM): nízký = 401, vysoký = bez plochy (bílá). Koruna jednoho stromu u silnice
nebo chodníku tak v hotové mapě zůstala bílou dírou v ploše ulice 501 nebo v trávníku 401 – na mapě to vypadá jako
kousek lesa na ulici. Nad hotovou mapou se proto malá bílá díra (do ``HOLE_MAX_M2``), která sousedí se zpevněnou
plochou 501 nebo s čarou chodníku / cesty a jejíž obvod tvoří hlavně 501 a 401, zalije (401, 501.1 jen u díry skoro celé
obklopené 501) a do koruny se dá bod 418 ve vrcholu z CHM (prořídlé na ``MIN_CENTER_MM`` mapy i proti kresleným
417 / 418). Bod, jehož značka by zasáhla vozovku (osa OSM silnice + půlka šířky vozovky jako u pásů ulic), se
nekreslí. Větší bílá plocha (park, remízek) zůstává.

Petr 2026-11-17 (Most, koruna + trávník u domu): budova v díře ani na jejím okraji se do plochy a obvodu nepočítá
a s CHM rozhoduje plocha porostu v díře (CHM ≥ ``VEG_MIN_H_M`` do ``HOLE_MAX_M2``), holá zem do ``HOLE_TOTAL_MAX_M2``
se zaleje jako louka 401 (chodník se nedetekuje).
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
HOLE_MAX_M2 = 250.0  # vegetace (CHM ≥ VEG_MIN_H_M) v díře nejvýš tolik = koruna jednoho až několika stromů; víc = park, remízek
HOLE_TOTAL_MAX_M2 = 1500.0  # celá díra (bez budov) nejvýš tolik; zbytek mimo korunu je holá zem (trávník u domu)
HOLE_MIN_M2 = 1.0
VEG_MIN_H_M = 1.0  # i keře se počítají do vegetace díry (holá zem pod 1 m = louka)
EDGE_M = 0.3  # díra sousedí s plochou, je-li její obvod do tolika metrů od ní
NEIGHBOUR_SHARE = 0.6  # obvod díry (bez hran budov) z 501 + 401 aspoň tolik (díra u vody / olivy ne)
PAVED_FILL_SHARE = 0.8  # 501.1 jen u díry skoro celé obklopené 501; jinak 401 (louka, Petr 2026-11-17)
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


def _poly_rings(g) -> list[list[tuple[int, int]]]:
    return [[(int(round(x)), int(round(y))) for x, y in r.coords] for r in [g.exterior, *g.interiors]]


def _merge_into_open(parts, open_objs, fill_polys, idx_open: int, near: float):
    """Výplně 401 sousedící s kreslenou loukou 401 sloučí s ní do jednoho objektu (Petr 2026-11-17).

    Louky a výplně spojené dotykem se sjednotí do jednoho objektu (křivky louky se převedou na lomenou čáru), první
    objekt skupiny se nahradí sjednocením, ostatní se z části odeberou. Vrací (části, výplně bez souseda, počet
    sloučených výplní)."""
    from shapely import STRtree
    from shapely.ops import unary_union

    from app.pipeline.oom_import import OomObjectPart, _area_object_with_holes

    if not open_objs or not fill_polys:
        return parts, fill_polys, 0
    tree = STRtree([o[2] for o in open_objs])
    n_open = len(open_objs)
    parent = list(range(n_open + len(fill_polys)))  # union-find: louky 0…n_open−1, výplně za nimi

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    rest = []
    for fi, g in enumerate(fill_polys):
        hits = [int(k) for k in tree.query(g, predicate="dwithin", distance=near)]
        if not hits:
            rest.append(g)
        for k in hits:
            parent[find(n_open + fi)] = find(k)
    groups: dict[int, tuple[list[int], list[int]]] = {}
    for k in range(n_open):
        groups.setdefault(find(k), ([], []))[0].append(k)
    for fi in range(len(fill_polys)):
        r = find(n_open + fi)
        if r != n_open + fi and r in groups:
            groups[r][1].append(fi)
    edits: dict[int, list[tuple[int, int, str]]] = {}
    merged = 0
    for objs, fis in groups.values():
        if not fis:
            continue
        geom = unary_union([open_objs[k][2] for k in objs] + [fill_polys[fi] for fi in fis])
        geom = geom.buffer(near / 4, join_style="mitre").buffer(-near / 4, join_style="mitre")  # švy po sjednocení pryč
        polys = [p for p in (getattr(geom, "geoms", None) or [geom]) if p.geom_type == "Polygon" and not p.is_empty]
        if not polys:
            rest.extend(fill_polys[fi] for fi in fis)
            continue
        merged += len(fis)
        xml = "\n".join(_area_object_with_holes(idx_open, _poly_rings(p)) for p in polys)
        for j, k in enumerate(sorted(objs, key=lambda k: (open_objs[k][0], open_objs[k][1][0]))):
            pi, (a, b), _ = open_objs[k]
            edits.setdefault(pi, []).append((a, b, xml if j == 0 else ""))
    out = list(parts)
    for pi, ed in edits.items():
        txt = out[pi].objects_xml
        count = out[pi].count
        for a, b, new in sorted(ed, reverse=True):
            txt = txt[:a] + new + txt[b:]
            count += (new.count("<object ") if new else 0) - 1
        out[pi] = OomObjectPart(name=out[pi].name, objects_xml=txt, count=count)
    return out, rest, merged


def _veg_area(hole_proj, chm, gt) -> float:
    """Plocha (m²) pixelů CHM ≥ ``VEG_MIN_H_M`` se středem v díře (S-JTSK)."""
    import numpy as np
    import shapely

    x0, px, _, y1, _, py = gt
    py = abs(py)
    h, w = chm.shape
    minx, miny, maxx, maxy = hole_proj.bounds
    c0 = max(0, int(math.floor((minx - x0) / px)))
    c1 = min(w, int(math.ceil((maxx - x0) / px)))
    r0 = max(0, int(math.floor((y1 - maxy) / py)))
    r1 = min(h, int(math.ceil((y1 - miny) / py)))
    if c1 - c0 < 1 or r1 - r0 < 1:
        return 0.0
    win = np.nan_to_num(np.asarray(chm[r0:r1, c0:c1], dtype=np.float32), nan=0.0)
    rr, cc = np.nonzero(win >= VEG_MIN_H_M)
    if not len(rr):
        return 0.0
    xs = x0 + (c0 + cc + 0.5) * px
    ys = y1 - (r0 + rr + 0.5) * py
    return float(shapely.contains_xy(hole_proj, xs, ys).sum()) * px * py


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
    from app.pipeline.path_clearance import (
        _COORDS_RE,
        _OBJECT_RE,
        BUILDING_CODE,
        _flatten,
        _parse_coords,
        _polygon,
        _rings,
        _symbol_codes,
        _symbol_info,
    )
    from app.pipeline.tree_rows import _load_chm, _poly_to_projected, thin_points

    t0 = time.monotonic()
    idx_paved = symbol_index_for_code(preset_id, scale, "501.1")
    idx_open = symbol_index_for_code(preset_id, scale, "401")
    idx_tree = symbol_index_for_code(preset_id, scale, "418")
    if idx_paved is None or idx_open is None:
        return parts
    tree_idx = {i for i in (symbol_index_for_code(preset_id, scale, c) for c in TREE_CODES) if i is not None}
    set_path = symbol_set_path(preset_id, scale)
    info = _symbol_info(set_path)
    building_ids = {i for i, c in _symbol_codes(set_path).items() if c == BUILDING_CODE}
    areas, paved, open_, paths, buildings = [], [], [], [], []
    existing: list[tuple[float, float]] = []
    open_objs = []  # (část, rozsah objektu v objects_xml, plocha) louky 401 pro sloučení s výplní
    for pi, part in enumerate(parts):
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
                if sym in building_ids:
                    buildings.append(poly if poly.is_valid else poly.buffer(0))
                elif major == PAVED_MAJOR:
                    paved.append(poly)
                elif major == OPEN_MAJOR:
                    open_.append(poly)
                    if sym == idx_open:
                        open_objs.append((pi, m.span(), poly if poly.is_valid else poly.buffer(0)))
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
            if HOLE_MIN_M2 * um * um <= hole.area:
                holes.append(hole)
    if not holes:
        return parts
    import numpy as np
    import shapely
    from shapely import STRtree

    b_tree = STRtree(buildings) if buildings else None
    chm = _load_chm(work_dir)

    def _edge_tree(polys):
        # hrany ploch jako krátké úsečky: dotaz kolem díry je lokální (ne průnik s celou plochou mapy)
        if not polys:
            return None
        segs = []
        for ln in shapely.get_parts(shapely.boundary(np.asarray(polys, dtype=object))):
            for part in shapely.get_parts(ln):
                c = shapely.get_coordinates(part)
                if len(c) >= 2:
                    segs.append(shapely.linestrings(np.stack([c[:-1], c[1:]], axis=1)))
        return STRtree(np.concatenate(segs)) if segs else None

    paved_t = _edge_tree(paved)
    open_t = _edge_tree(open_)
    build_t = _edge_tree(buildings)
    path_tree = STRtree(paths) if paths else None
    step = 0.5 * um

    def _near(tree, pts):
        if tree is None:
            return np.zeros(len(pts), dtype=bool)
        mask = np.zeros(len(pts), dtype=bool)
        mask[np.unique(tree.query(pts, predicate="dwithin", distance=EDGE_M * um)[0])] = True
        return mask

    axes_tree, roads = _road_axes(work_dir)
    r_tree = TREE_SYMBOL_R_MM * scale / 1000.0
    min_center_m = MIN_CENTER_MM * scale / 1000.0
    fills = {idx_paved: [], idx_open: []}
    points: list[tuple[float, float]] = []
    blocked = 0
    for hole in holes:
        # Budova uvnitř díry (ostrov) nebo na jejím okraji do plochy díry ani obvodu nepatří (Petr 2026-11-17, Most:
        # koruna + trávník u domu bílé, holá zem v CHM).
        inside = [buildings[int(k)] for k in b_tree.query(hole)] if b_tree is not None else []
        free = hole.difference(unary_union(inside)) if inside else hole
        if free.is_empty or not HOLE_MIN_M2 * um * um <= free.area <= HOLE_TOTAL_MAX_M2 * um * um:
            continue
        ring = hole.exterior
        perim = ring.length
        if perim <= 0:
            continue
        pts = shapely.line_interpolate_point(ring, np.arange(0.0, perim, min(step, perim / 8)))
        keep = ~_near(build_t, pts)
        if keep.sum() < 4:
            continue
        s_paved = float(_near(paved_t, pts)[keep].mean())
        s_open = float(_near(open_t, pts)[keep].mean())
        near_path = path_tree is not None and len(path_tree.query(hole, predicate="dwithin", distance=PATH_NEAR_M * um)) > 0
        if (s_paved <= 0 and not near_path) or s_paved + s_open < NEIGHBOUR_SHARE:
            continue
        hole_proj = _poly_to_projected(free, ref_x, ref_y, scale, grivation_deg) if chm is not None else None
        # Velikost se posuzuje podle vegetace: s CHM jen plocha s porostem (koruna), holá zem se zaleje; bez CHM celá díra.
        veg = _veg_area(hole_proj, *chm) if chm is not None else free.area / (um * um)
        if veg > HOLE_MAX_M2:
            continue
        sym = idx_paved if s_paved >= PAVED_FILL_SHARE else idx_open
        for g in getattr(free, "geoms", None) or [free]:
            if g.geom_type == "Polygon" and g.area >= HOLE_MIN_M2 * um * um:
                fills[sym].append(g)
        if idx_tree is None or chm is None:
            continue
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
    n_paved, n_open = len(fills[idx_paved]), len(fills[idx_open])
    if not n_paved and not n_open:
        return parts
    # Louka v díře se slije se sousední loukou 401 do jednoho objektu (Petr 2026-11-17), zbytek zůstane samostatnou výplní.
    out, fills[idx_open], merged = _merge_into_open(list(parts), open_objs, fills[idx_open], idx_open, EDGE_M * um)
    objects = [_area_object_with_holes(sym, _poly_rings(g)) for sym, polys in fills.items() for g in polys]
    if objects:
        out.append(OomObjectPart(name=FILL_PART_NAME, objects_xml="\n".join(objects), count=len(objects)))
    if points:
        pts_xml = [
            _point_object(idx_tree, *projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg))
            for x, y in points
        ]
        out.append(OomObjectPart(name=TREE_PART_NAME, objects_xml="\n".join(pts_xml), count=len(pts_xml)))
    if log:
        log(
            f"Stromy u ulic: zalito {n_paved} korun do 501.1 a {n_open} do 401 (sloučeno s loukou {merged}), "
            f"bodů 418 {len(points)}, u vozovky vynecháno {blocked} ({'CHM' if chm else 'bez CHM'}, {time.monotonic() - t0:.1f} s)"
        )
    return out
