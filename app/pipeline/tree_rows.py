"""Aleje (stromořadí): čára 416 se nekreslí, místo ní body 418 ve středech stromů, jen kde je alej opravdu vidět.

Petr 2026-11-08, Lochkov, ZABAGED 10532653912555520 / OSM way 1284855877, pokus ``/mnt/project-files/aleje-418/``:
alej (ZABAGED ``LiniovaVegetace``, OSM ``natural=tree_row``) se nad hotovou mapou
1. nekreslí vůbec, pokud je z ≥ ``SUPPORT_SHARE`` vzorků podél linie v zelené ploše (406–410) nebo mimo každou plochu
   (bílá: louka, silnice – stromy nejsou co zdůraznit); vzorky v pásu kreslené cesty se do podílu nepočítají;
2. jinak se kreslí body 418 ve středech korun z CHM jobu (lokální maxima do ``ROW_BAND_M`` od linie, řada kolem mediánu
   odsazení), prořídlé na ``MIN_CENTER_MM`` mapy i proti už kresleným stromům 417 / 418 (AOPK, OSM, sprint);
3. bez CHM jen záložní body v pravidelném rozestupu; s CHM bez stromů nic (alej v terénu už není).
Univerzální pravidlo (sprint, les, MTBO). Body 418 aleje se v lese a MTBO odsouvají od cest (``path_clearance``).
"""

from __future__ import annotations

import io
import json
import logging
import math
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

TREE_ROW_PART_NAME = "Aleje (stromořadí, 418)"
GREEN_MAJORS = frozenset({406, 407, 408, 409, 410})  # hlavní čísla zelených ploch (porost)
SUPPORT_SHARE = 0.75  # zelená + bílá ≥ tolik vzorků → alej se nekreslí
SAMPLE_STEP_M = 1.0
PATH_SAMPLES_MIN_SHARE = 0.20  # zbyde-li mimo pásy cest méně vzorků, počítají se všechny
TREE_MIN_H_M = 3.0  # koruna = výška nad terénem
ROW_BAND_M = 8.0  # vrcholy dál od linie se neberou
ROW_TOL_M = 3.0  # odchylka odsazení vrcholu od mediánu odsazení řady
ROW_MIN_TREES = 3  # od tolika vrcholů se řada drží kolem mediánu
MIN_CENTER_MM = 1.0  # nejmenší vzdálenost středů bodů 418 (a od kreslených stromů) na mapě
FALLBACK_SPACING_M = 8.0  # záloha bez CHM
TREE_CODES = ("417", "418")
LINE_LAYER = "LiniovaVegetace"
OSM_KIND = "tree_row"


def sample_line(line: list[tuple[float, float]], step: float = SAMPLE_STEP_M) -> list[tuple[float, float]]:
    """Body podél lomené čáry po ``step`` metrech (včetně obou konců)."""
    from shapely.geometry import LineString

    if len(line) < 2:
        return list(line)
    ls = LineString(line)
    n = max(1, int(math.ceil(ls.length / step)))
    return [(p.x, p.y) for p in (ls.interpolate(ls.length * i / n) for i in range(n + 1))]


def row_support(samples, green_geom, other_geom, path_band_geom=None) -> tuple[float, float]:
    """(podíl zelených, podíl bílých) vzorků pod alejí.

    Zelený = vzorek v ``green_geom`` (plochy 406–410), bílý = vzorek v žádné ploše (mimo ``other_geom``, ta zahrnuje
    i zelené). Vzorky v ``path_band_geom`` (pás kreslené cesty) se nepočítají; zbyde-li jich méně než
    ``PATH_SAMPLES_MIN_SHARE``, počítají se všechny. Bez vzorků (0, 0)."""
    import numpy as np
    import shapely

    if not len(samples):
        return 0.0, 0.0
    xs = np.array([p[0] for p in samples], dtype=float)
    ys = np.array([p[1] for p in samples], dtype=float)
    use = np.ones(len(xs), dtype=bool)
    if path_band_geom is not None and not path_band_geom.is_empty:
        free = ~shapely.contains_xy(path_band_geom, xs, ys)
        if free.sum() >= PATH_SAMPLES_MIN_SHARE * len(xs):
            use = free
    xs, ys = xs[use], ys[use]
    green = shapely.contains_xy(green_geom, xs, ys) if green_geom is not None and not green_geom.is_empty else np.zeros(len(xs), bool)
    anyarea = shapely.contains_xy(other_geom, xs, ys) if other_geom is not None and not other_geom.is_empty else np.zeros(len(xs), bool)
    white = ~(anyarea | green)
    n = float(len(xs))
    return float(green.sum()) / n, float(white.sum()) / n


def detect_row_trees(line: list[tuple[float, float]], chm, chm_gt: tuple) -> list[tuple[float, float]]:
    """Středy korun aleje podél linie (S-JTSK), seřazené podél linie.

    ``chm`` – výška nad terénem (NaN = nic), ``chm_gt`` – GDAL geotransform (sever nahoře). Čte se jen okno kolem linie."""
    import numpy as np
    from scipy import ndimage
    from shapely.geometry import LineString, Point

    if len(line) < 2:
        return []
    ls = LineString(line)
    x0, px, _, y1, _, py = chm_gt
    py = abs(py)
    h, w = chm.shape
    minx, miny, maxx, maxy = ls.bounds
    c0 = max(0, int(math.floor((minx - ROW_BAND_M - x0) / px)))
    c1 = min(w, int(math.ceil((maxx + ROW_BAND_M - x0) / px)))
    r0 = max(0, int(math.floor((y1 - (maxy + ROW_BAND_M)) / py)))
    r1 = min(h, int(math.ceil((y1 - (miny - ROW_BAND_M)) / py)))
    if c1 - c0 < 1 or r1 - r0 < 1:
        return []
    win = np.nan_to_num(np.asarray(chm[r0:r1, c0:c1], dtype=np.float32), nan=0.0)
    sm = ndimage.gaussian_filter(win, sigma=1.0)
    peaks = (sm == ndimage.maximum_filter(sm, size=5)) & (sm >= TREE_MIN_H_M)
    lab, n = ndimage.label(peaks)  # plató vrcholu = jeden strom
    if n == 0:
        return []
    cents = ndimage.center_of_mass(peaks, lab, range(1, n + 1))
    cand = []
    for rr, cc in cents:
        x = x0 + (c0 + cc + 0.5) * px
        y = y1 - (r0 + rr + 0.5) * py
        pt = Point(x, y)
        d = ls.distance(pt)
        if d > ROW_BAND_M:
            continue
        s = ls.project(pt)
        a = ls.interpolate(max(0.0, s - 0.5))
        b = ls.interpolate(min(ls.length, s + 0.5))
        q = ls.interpolate(s)
        side = (b.x - a.x) * (y - q.y) - (b.y - a.y) * (x - q.x)
        cand.append((s, math.copysign(d, side) if side else d, (x, y)))
    if len(cand) >= ROW_MIN_TREES:
        med = float(np.median([c[1] for c in cand]))
        cand = [c for c in cand if abs(c[1] - med) <= ROW_TOL_M]
    cand.sort(key=lambda c: c[0])
    return [c[2] for c in cand]


def fallback_points(line: list[tuple[float, float]], spacing_m: float) -> list[tuple[float, float]]:
    """Body v pravidelném rozestupu podél linie (bez CHM); vždy aspoň jeden uprostřed."""
    from shapely.geometry import LineString

    if len(line) < 2:
        return []
    ls = LineString(line)
    n = max(1, int(ls.length // spacing_m))
    step = ls.length / n
    return [(p.x, p.y) for p in (ls.interpolate(step * (i + 0.5)) for i in range(n))]


def thin_points(candidates, existing, min_dist_m: float) -> list[tuple[float, float]]:
    """Ponechá kandidáty (v pořadí), kteří jsou aspoň ``min_dist_m`` od ``existing`` i od dříve ponechaných."""
    import numpy as np

    kept: list[tuple[float, float]] = []
    pool = np.array(list(existing), dtype=float).reshape(-1, 2)
    for x, y in candidates:
        if len(pool) and float(np.min(np.hypot(pool[:, 0] - x, pool[:, 1] - y))) < min_dist_m:
            continue
        kept.append((x, y))
        pool = np.vstack([pool, [x, y]])
    return kept


def _osm_row_lines(work_dir: Path) -> list[list[tuple[float, float]]]:
    gj = Path(work_dir) / "osm_paths" / "features.geojson"
    try:
        feats = json.loads(gj.read_text(encoding="utf-8")).get("features") or []
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for f in feats:
        geom = f.get("geometry") or {}
        if (f.get("properties") or {}).get("kind") != OSM_KIND or geom.get("type") != "LineString":
            continue
        pts = [(float(c[0]), float(c[1])) for c in geom.get("coordinates") or []]
        if len(pts) >= 2:
            out.append(pts)
    return out


def _zabaged_row_lines(work_dir: Path) -> list[list[tuple[float, float]]]:
    """Linie ZABAGED ``LiniovaVegetace`` ze ``zabaged_clean.zip``; bez archivu / vrstvy prázdný seznam."""
    archive = Path(work_dir) / "zabaged_clean.zip"
    if not archive.is_file():
        return []
    try:
        import shapefile

        out = []
        with zipfile.ZipFile(archive) as zf:
            if not {f"{LINE_LAYER}.shp", f"{LINE_LAYER}.shx", f"{LINE_LAYER}.dbf"} <= set(zf.namelist()):
                return []
            reader = shapefile.Reader(
                shp=io.BytesIO(zf.read(f"{LINE_LAYER}.shp")),
                shx=io.BytesIO(zf.read(f"{LINE_LAYER}.shx")),
                dbf=io.BytesIO(zf.read(f"{LINE_LAYER}.dbf")),
            )
            for sh in reader.iterShapes():
                pts = [(float(p[0]), float(p[1])) for p in (sh.points or [])]
                parts = list(sh.parts or [0]) + [len(pts)]
                for a, b in zip(parts[:-1], parts[1:]):
                    if b - a >= 2:
                        out.append(pts[a:b])
        return out
    except Exception as exc:  # bez linií ZABAGED jen OSM
        logger.warning("tree_rows: aleje ZABAGED nedostupné (%s)", exc)
        return []


def _load_chm(work_dir: Path):
    """(chm, gt) nebo None, když job CHM nemá / nejde přečíst."""
    try:
        import numpy as np

        from app.pipeline.gdal_cli_raster import read_float32_geotiff
        from app.pipeline.vegetation_chm import chm_path

        src = chm_path(Path(work_dir))
        if src is None:
            return None
        chm, gt, nodata = read_float32_geotiff(src)
        if nodata is not None:
            chm = np.where(chm == nodata, np.nan, chm)
        return chm, gt
    except Exception as exc:
        logger.warning("tree_rows: CHM nelze přečíst (%s)", exc)
        return None


def _map_content(parts, set_path, tree_idx, lines, *, ref_x, ref_y, scale, grivation_deg):
    """(zelené plochy, ostatní plochy, existující stromy 417 / 418) v S-JTSK z hotových částí mapy.

    Plochy se převádějí jen v okolí linií (ať práce neroste s plochou mapy)."""
    from shapely.geometry import LineString, box
    from shapely.ops import unary_union
    from shapely.strtree import STRtree

    from app.pipeline.oom_coords import map_to_projected, projected_to_map_coord
    from app.pipeline.path_clearance import _COORDS_RE, _OBJECT_RE, _flatten, _parse_coords, _polygon, _rings, _symbol_info

    info = _symbol_info(set_path)
    margin = 3.0 * 1e6 / scale  # 3 m terénu v jednotkách mapy
    near = STRtree(
        [
            LineString([projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg) for x, y in ln])
            for ln in lines
        ]
    )
    green, anyarea = [], []
    trees: list[tuple[float, float]] = []
    for part in parts:
        for m in _OBJECT_RE.finditer(part.objects_xml):
            sym = int(m.group(3))
            major, kind = info.get(sym, (0, "other"))
            is_tree = m.group(2) == "0" and sym in tree_idx
            if not is_tree and not (m.group(2) == "1" and kind == "area"):
                continue
            cm = _COORDS_RE.search(m.group(4))
            if not cm:
                continue
            pts = _parse_coords(cm.group(1))
            if not pts:
                continue
            if is_tree:
                trees.append(map_to_projected(pts[0][0], pts[0][1], ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg))
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            bb = box(min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)
            if len(near.query(bb)) == 0:
                continue
            poly = _polygon(_rings(pts))
            if poly is None or poly.is_empty:
                continue
            # polygon v souřadnicích mapy → S-JTSK
            conv = _poly_to_projected(poly, ref_x, ref_y, scale, grivation_deg)
            (green if major in GREEN_MAJORS else anyarea).append(conv)
    return (unary_union(green) if green else None), (unary_union(anyarea) if anyarea else None), trees


def _poly_to_projected(poly, ref_x, ref_y, scale, grivation_deg):
    from shapely.geometry import Polygon

    from app.pipeline.oom_coords import map_to_projected

    def conv(coords):
        return [map_to_projected(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg) for x, y in coords]

    out = []
    for g in getattr(poly, "geoms", None) or [poly]:
        if g.geom_type != "Polygon":
            continue
        p = Polygon(conv(g.exterior.coords), [conv(r.coords) for r in g.interiors])
        out.append(p if p.is_valid else p.buffer(0))
    from shapely.ops import unary_union

    return unary_union(out)


def add_tree_row_parts(
    parts,
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    clip_bounds=None,
    log=None,
):
    """Hotová mapa + body 418 aleje (OSM ``tree_row`` a ZABAGED ``LiniovaVegetace``); bez linií nebo při chybě beze změny."""
    import time

    t0 = time.monotonic()
    try:
        lines_all = _osm_row_lines(work_dir) + _zabaged_row_lines(work_dir)  # OSM je základ, jde první
        if not lines_all:
            return parts
        from app.pipeline.geom_clip import clip_polyline
        from app.pipeline.oom_coords import projected_to_map_coord
        from app.pipeline.oom_import import OomObjectPart, _point_object
        from app.pipeline.oom_symbol_map import symbol_index_for_code
        from app.pipeline.oom_symbols import symbol_set_path
        from app.pipeline.path_clearance import PATH_MAJORS, drawn_path_bands

        symbol_index = symbol_index_for_code(preset_id, scale, "418")
        if symbol_index is None:
            return parts
        lines = []
        for ln in lines_all:
            lines.extend(p for p in (clip_polyline(ln, clip_bounds) if clip_bounds else [ln]) if len(p) >= 2)
        if not lines:
            return parts
        set_path = symbol_set_path(preset_id, scale)
        tree_idx = {i for i in (symbol_index_for_code(preset_id, scale, c) for c in TREE_CODES) if i is not None}
        green, anyarea, existing = _map_content(
            parts, set_path, tree_idx, lines, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg
        )
        from shapely.geometry import LineString
        from shapely.ops import unary_union

        bands = drawn_path_bands(
            parts, preset_id=preset_id, scale=scale, ref_x=ref_x, ref_y=ref_y, grivation_deg=grivation_deg, majors=PATH_MAJORS
        )
        band = unary_union([LineString(ax).buffer(half) for ax, half, _fill in bands if len(ax) >= 2]) if bands else None
        chm = _load_chm(work_dir)
        min_center_m = MIN_CENTER_MM * scale / 1000.0
        stats = {"rows": len(lines), "support": 0, "empty": 0, "drawn": 0}
        points: list[tuple[float, float]] = []
        for ln in lines:
            g_share, w_share = row_support(sample_line(ln), green, anyarea, band)
            if g_share + w_share >= SUPPORT_SHARE:
                stats["support"] += 1
                continue
            cand = detect_row_trees(ln, *chm) if chm else fallback_points(ln, max(FALLBACK_SPACING_M, min_center_m))
            kept = thin_points(cand, list(existing) + points, min_center_m)
            if not kept:
                stats["empty"] += 1
                continue
            stats["drawn"] += 1
            points.extend(kept)
    except Exception as exc:  # mapa se kreslí i bez alejí
        logger.warning("tree_rows: přeskočeno (%s)", exc)
        if log:
            log(f"Aleje: přeskočeno ({exc})")
        return parts
    if log:
        log(
            f"Aleje (418): {stats['rows']} linií, nekreslených kvůli zeleni / bílé {stats['support']}, "
            f"bez stromů {stats['empty']}, kreslených {stats['drawn']}, bodů {len(points)} "
            f"({'CHM' if chm else 'záloha bez CHM'}, {time.monotonic() - t0:.1f} s)"
        )
    objects = [
        _point_object(symbol_index, *projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg))
        for x, y in points
    ]
    if not objects:
        return parts
    return list(parts) + [OomObjectPart(name=TREE_ROW_PART_NAME, objects_xml="\n".join(objects), count=len(objects))]
