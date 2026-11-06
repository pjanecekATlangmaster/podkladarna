"""Sprint: samostatný strom na zpevněné ploše (náměstí, pěší zóna, parkoviště) jako bod 417 ve středu koruny.

Petr 2026-11-06: strom na dlažbě je orientační bod pro kontrolu; kreslí se jen kmen (bod), ne koruna. Zdroj je CHM
jobu (DMP OK − DMR 5G), potvrzení ortofotem (zelená koruna, nezelené okolí). Průzkum Krumlov centrum a Barrandov
(0,6 km² každý): 7 / 4 stromy, ručně ověřeno ~80 % skutečných; střed koruny proti OSM bodu stromu ~2 m.
Výřezy a skript: ``/mnt/project-files/stromy-dmpok/``. Les a MTBO beze změny.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

SPRINT_PAVED_TREES = True
TREE_MIN_CHM_M = 3.0  # koruna = výška nad terénem
TREE_MIN_HEIGHT_M = 5.0  # nejvyšší bod koruny
CROWN_MIN_M2 = 12.0
CROWN_MAX_M2 = 250.0  # větší skvrna = skupina stromů / porost
CROWN_MIN_COMPACT = 0.45  # plocha / kruh opsaný delší straně obálky (protáhlé stromořadí / živý plot ne)
ISOLATION_M = 10.0  # okolí koruny bez další vegetace (2,5 mm v 1:4000)
ISOLATION_MAX_SHARE = 0.10
OTHER_VEG_M = 2.0
BUILDING_BUFFER_M = 1.5
CROWN_GREEN_MIN = 0.30  # podíl zelených pixelů ortofota v koruně (střecha, věž, socha ne)
GROUND_GREEN_MAX = 0.25  # podíl zelených pixelů v okolí koruny: méně = zpevněná plocha
EXISTING_TREE_NEAR_M = 8.0  # strom už nakreslený z OSM / AOPK / ZABAGED
OPEN_AROUND_MIN = 0.5  # aspoň tolik okolí koruny mimo budovy (strom na volné ploše, ne ve dvoře; Tilleho nám. 0,57)
PAVED_KINDS = frozenset({"pedestrian_area", "parking", "platform"})
OPEN_AROUND_MIN_PAVED = 0.4  # střed koruny na OSM pěší zóně / náměstí / parkovišti (Tilleho nám.: okolí 0,46–0,49 kvůli fasádám)
GREEN_REACH_M = 2.0  # výška je vegetace jen do této vzdálenosti od zeleně v ortofotu (posun ortofota proti LiDARu)
NONGREEN_MASS_M = 2.0  # nezelená výška širší než 2× tolik (nezmapovaná budova) vadí v izolaci; užší pás u fasády ne


def _rasterize(geom, shape: tuple[int, int], x0: float, y1: float, px: float, py: float):
    """bool maska (Multi)Polygonu na mřížce rastru (sever nahoře)."""
    import numpy as np

    h, w = shape
    if geom is None or geom.is_empty:
        return np.zeros((h, w), dtype=bool)
    from PIL import Image, ImageDraw

    im = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(im)
    for g in getattr(geom, "geoms", None) or [geom]:
        if g.is_empty or g.geom_type != "Polygon":
            continue
        draw.polygon([((x - x0) / px, (y1 - y) / py) for x, y in g.exterior.coords], fill=1)
        for hole in g.interiors:
            draw.polygon([((x - x0) / px, (y1 - y) / py) for x, y in hole.coords], fill=0)
    return np.asarray(im, dtype=bool)


def _green_on_grid(ortho_green, ortho_gt: tuple, shape: tuple[int, int], x0: float, y1: float, px: float, py: float):
    """bool maska buněk rastru výšky do ``GREEN_REACH_M`` od zelené v ortofotu; mimo ortofoto True (beze změny)."""
    import numpy as np
    from scipy import ndimage

    og = np.asarray(ortho_green, dtype=bool)
    oh, ow = og.shape
    ox0, opx, _, oy1, _, opy = ortho_gt
    opy = abs(opy)
    reach = max(1, int(round(GREEN_REACH_M / opx)))
    yy, xx = np.mgrid[-reach : reach + 1, -reach : reach + 1]
    near = ndimage.binary_dilation(og, structure=(xx**2 + yy**2) <= reach**2)
    h, w = shape
    cols = np.floor((x0 + (np.arange(w) + 0.5) * px - ox0) / opx).astype(int)
    rows = np.floor((oy1 - (y1 - (np.arange(h) + 0.5) * py)) / opy).astype(int)
    inside = (rows[:, None] >= 0) & (rows[:, None] < oh) & (cols[None, :] >= 0) & (cols[None, :] < ow)
    out = near[np.clip(rows, 0, oh - 1)[:, None], np.clip(cols, 0, ow - 1)[None, :]]
    return np.where(inside, out, True)


def detect_paved_trees(
    chm,
    chm_gt: tuple,
    ortho_green,
    ortho_gt: tuple,
    buildings=None,
    existing: list[tuple[float, float]] | None = None,
    water=None,
    paved=None,
) -> list[tuple[float, float]]:
    """Středy korun samostatných stromů na zpevněné ploše (S-JTSK).

    ``chm`` – výška nad terénem (NaN = nic), ``chm_gt`` / ``ortho_gt`` – GDAL geotransform (sever nahoře),
    ``ortho_green`` – bool maska zelených pixelů ortofota, ``buildings`` / ``water`` – shapely (Multi)Polygon budov / vody,
    ``paved`` – OSM pěší zóny / náměstí / parkoviště: strom na nich smí mít volné okolí jen ``OPEN_AROUND_MIN_PAVED``.
    """
    import numpy as np
    from scipy import ndimage

    arr = np.nan_to_num(np.asarray(chm, dtype=np.float32), nan=0.0)
    h, w = arr.shape
    x0, px, _, y1, _, py = chm_gt
    py = abs(py)
    cell = px * py
    bmask = _rasterize(buildings.buffer(BUILDING_BUFFER_M) if buildings is not None else None, (h, w), x0, y1, px, py)
    wmask = _rasterize(water, (h, w), x0, y1, px, py)
    # Výška bez zeleně v ortofotu (přesah patra, podloubí, markýza u fasády) není koruna: jinak spojí samostatné
    # stromy do jedné skvrny a vadí v izolaci (Tilleho nám., Petr 2026-11-06).
    # Nezelená výška široká jako budova (nezmapovaná střecha, stavba) ale v izolaci vadí dál, jinak by se zelené
    # drobky na střeše nebo vedle ní tvářily jako samostatný strom.
    greenish = _green_on_grid(ortho_green, ortho_gt, (h, w), x0, y1, px, py)
    veg = (arr > TREE_MIN_CHM_M) & ~bmask & greenish
    tall_grey = (arr > OTHER_VEG_M) & ~bmask & ~greenish
    mr = max(1, int(round(NONGREEN_MASS_M / px)))
    my, mx = np.mgrid[-mr : mr + 1, -mr : mr + 1]
    mass = ndimage.binary_opening(tall_grey, structure=(mx**2 + my**2) <= mr**2)
    other = (arr > OTHER_VEG_M) & ~bmask & (greenish | mass)
    lab, _n = ndimage.label(veg)
    ring_px = max(1, int(round(ISOLATION_M / px)))
    og = np.asarray(ortho_green, dtype=bool)
    oh, ow = og.shape
    ox0, opx, _, oy1, _, opy = ortho_gt
    opy = abs(opy)
    near2 = EXISTING_TREE_NEAR_M**2
    paved_prep = None
    if paved is not None and not paved.is_empty:
        from shapely.prepared import prep

        paved_prep = prep(paved)
    out: list[tuple[float, float]] = []
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        own = lab[sl] == i
        area = float(own.sum()) * cell
        if not (CROWN_MIN_M2 <= area <= CROWN_MAX_M2):
            continue
        if float(arr[sl][own].max()) < TREE_MIN_HEIGHT_M:
            continue
        rr, cc = np.nonzero(own)
        ext = max(int(np.ptp(rr)) + 1, int(np.ptp(cc)) + 1) * px
        if area / (math.pi * (ext / 2) ** 2) < CROWN_MIN_COMPACT:
            continue
        r0, r1 = max(sl[0].start - ring_px, 0), min(sl[0].stop + ring_px, h)
        c0, c1 = max(sl[1].start - ring_px, 0), min(sl[1].stop + ring_px, w)
        mine = lab[r0:r1, c0:c1] == i
        ring = ndimage.binary_dilation(mine, iterations=ring_px) & ~ndimage.binary_dilation(mine, iterations=1)
        if ring.any() and (other[r0:r1, c0:c1] & ring).sum() / ring.sum() > ISOLATION_MAX_SHARE:
            continue
        cy = rr.mean() + sl[0].start + 0.5
        cx = cc.mean() + sl[1].start + 0.5
        x, y = x0 + cx * px, y1 - cy * py
        if existing and any((x - ex) ** 2 + (y - ey) ** 2 <= near2 for ex, ey in existing):
            continue
        # Ortofoto: koruna zelená, okolí (1,6–3 poloměry) nezelené = dlažba / asfalt.
        rad = math.sqrt(area / math.pi)
        oc, orow = (x - ox0) / opx, (oy1 - y) / opy
        r_out = rad * 3 / opx
        q0, q1 = int(max(orow - r_out, 0)), int(min(orow + r_out + 1, oh))
        p0, p1 = int(max(oc - r_out, 0)), int(min(oc + r_out + 1, ow))
        if q1 <= q0 or p1 <= p0:
            continue
        yy, xx = np.mgrid[q0:q1, p0:p1]
        dist = np.hypot(yy + 0.5 - orow, xx + 0.5 - oc) * opx
        win = og[q0:q1, p0:p1]
        crown = dist <= rad * 0.7
        ring_all = (dist > rad * 1.6) & (dist <= rad * 3)
        # Okolí bez budov a vody: strom ve dvoře mezi střechami ani na břehu není strom na náměstí.
        bx = np.clip(((ox0 + (xx + 0.5) * opx - x0) / px).astype(int), 0, w - 1)
        by = np.clip(((y1 - (oy1 - (yy + 0.5) * opy)) / py).astype(int), 0, h - 1)
        around = ring_all & ~bmask[by, bx] & ~wmask[by, bx]
        open_min = OPEN_AROUND_MIN
        if paved_prep is not None:
            from shapely.geometry import Point

            if paved_prep.contains(Point(x, y)):
                open_min = OPEN_AROUND_MIN_PAVED
        if not crown.any() or around.sum() < open_min * max(int(ring_all.sum()), 1):
            continue
        if win[crown].mean() < CROWN_GREEN_MIN or win[around].mean() >= GROUND_GREEN_MAX:
            continue
        out.append((float(x), float(y)))
    return out


def _building_union(work_dir: Path):
    from shapely.geometry import shape
    from shapely.ops import unary_union

    gj = Path(work_dir) / "osm_paths" / "features.geojson"
    try:
        feats = json.loads(gj.read_text(encoding="utf-8")).get("features") or []
    except (OSError, json.JSONDecodeError):
        feats = []
    try:
        from app.pipeline.dtm_import import dtm_owns_buildings, merge_dtm_buildings

        if dtm_owns_buildings(work_dir):
            feats = merge_dtm_buildings(work_dir, feats)
    except Exception as exc:  # bez DTM jen OSM budovy
        logger.warning("sprint_trees: budovy z DTM nedostupné (%s)", exc)
    polys = []
    for f in feats:
        geom = f.get("geometry") or {}
        if (f.get("properties") or {}).get("kind") in {"building", "water_well_building"} and geom.get("type") in (
            "Polygon",
            "MultiPolygon",
        ):
            try:
                g = shape(geom)
                polys.append(g if g.is_valid else g.buffer(0))
            except (ValueError, TypeError):
                continue
    return unary_union(polys) if polys else None


def _water_union(work_dir: Path):
    from shapely.geometry import shape
    from shapely.ops import unary_union

    gj = Path(work_dir) / "osm_paths" / "features.geojson"
    try:
        feats = json.loads(gj.read_text(encoding="utf-8")).get("features") or []
    except (OSError, json.JSONDecodeError):
        return None
    polys = []
    for f in feats:
        geom = f.get("geometry") or {}
        if (f.get("properties") or {}).get("kind") in {"water_body", "fountain"} and geom.get("type") in ("Polygon", "MultiPolygon"):
            try:
                g = shape(geom)
                polys.append(g if g.is_valid else g.buffer(0))
            except (ValueError, TypeError):
                continue
    return unary_union(polys) if polys else None


def _paved_union(work_dir: Path):
    """OSM pěší zóny, náměstí, parkoviště a nástupiště (kind ve ``features.geojson``)."""
    from shapely.geometry import shape
    from shapely.ops import unary_union

    gj = Path(work_dir) / "osm_paths" / "features.geojson"
    try:
        feats = json.loads(gj.read_text(encoding="utf-8")).get("features") or []
    except (OSError, json.JSONDecodeError):
        return None
    polys = []
    for f in feats:
        geom = f.get("geometry") or {}
        if (f.get("properties") or {}).get("kind") in PAVED_KINDS and geom.get("type") in ("Polygon", "MultiPolygon"):
            try:
                g = shape(geom)
                polys.append(g if g.is_valid else g.buffer(0))
            except (ValueError, TypeError):
                continue
    return unary_union(polys) if polys else None


def _osm_tree_points(work_dir: Path) -> list[tuple[float, float]]:
    gj = Path(work_dir) / "osm_paths" / "features.geojson"
    try:
        feats = json.loads(gj.read_text(encoding="utf-8")).get("features") or []
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for f in feats:
        geom = f.get("geometry") or {}
        if (f.get("properties") or {}).get("kind") == "landmark_tree" and geom.get("type") == "Point":
            out.append(tuple(float(c) for c in geom["coordinates"][:2]))
    return out


def _zabaged_tree_points(work_dir: Path) -> list[tuple[float, float]]:
    """Body ZABAGED ``VyznamnyStromLesik`` (od 3.20 se kreslí 417) z ``zabaged_clean.zip``; bez archivu prázdný seznam."""
    import io
    import zipfile

    archive = Path(work_dir) / "zabaged_clean.zip"
    stem = "VyznamnyStromLesik"
    if not archive.is_file():
        return []
    try:
        import shapefile

        out: list[tuple[float, float]] = []
        with zipfile.ZipFile(archive) as zf:
            if not {f"{stem}.shp", f"{stem}.shx", f"{stem}.dbf"} <= set(zf.namelist()):
                return []
            reader = shapefile.Reader(
                shp=io.BytesIO(zf.read(f"{stem}.shp")),
                shx=io.BytesIO(zf.read(f"{stem}.shx")),
                dbf=io.BytesIO(zf.read(f"{stem}.dbf")),
            )
            for sh in reader.iterShapes():
                out.extend((float(p[0]), float(p[1])) for p in (sh.points or []))
        return out
    except Exception as exc:  # bez ZABAGED stromů jen dedup proti OSM / AOPK
        logger.warning("sprint_trees: stromy ZABAGED nedostupné (%s)", exc)
        return []


def sprint_paved_tree_points(
    work_dir: Path,
    bbox_wgs84,
    *,
    existing: list[tuple[float, float]] | None = None,
    log=None,
) -> list[tuple[float, float]]:
    """Stromy na zpevněné ploše pro sprint; bez CHM nebo ortofota prázdný seznam."""
    if not SPRINT_PAVED_TREES or not bbox_wgs84:
        return []
    try:
        import numpy as np

        from app.pipeline.gdal_cli_raster import read_float32_geotiff
        from app.pipeline.orthophoto import fetch_orthophoto, green_mask
        from app.pipeline.street_parcels import ORTHO_MPP, street_fetch_bounds_5514
        from app.pipeline.vegetation_chm import chm_path

        src = chm_path(Path(work_dir))
        if src is None:
            return []
        chm, gt, nodata = read_float32_geotiff(src)
        if nodata is not None:
            chm = np.where(chm == nodata, np.nan, chm)
        got = fetch_orthophoto(work_dir, street_fetch_bounds_5514(bbox_wgs84), ORTHO_MPP, log=log)
        if got is None:
            if log:
                log("Stromy na zpevněné ploše: bez ortofota přeskočeno")
            return []
        rgb, ogt = got
        pts = detect_paved_trees(
            chm,
            gt,
            green_mask(rgb),
            ogt,
            buildings=_building_union(work_dir),
            existing=list(existing or []) + _osm_tree_points(work_dir) + _zabaged_tree_points(work_dir),
            water=_water_union(work_dir),
            paved=_paved_union(work_dir),
        )
    except Exception as exc:  # mapa se kreslí i bez stromů
        logger.warning("sprint_trees: přeskočeno (%s)", exc)
        if log:
            log(f"Stromy na zpevněné ploše: přeskočeno ({exc})")
        return []
    if log:
        log(f"Stromy na zpevněné ploše (417 z DMP OK + ortofota): {len(pts)}")
    return pts


def build_sprint_tree_parts(
    points: list[tuple[float, float]],
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    clip_bounds=None,
):
    """Body 417 „Výrazný velký strom“ pro sprint."""
    from app.pipeline.geom_clip import point_inside
    from app.pipeline.oom_coords import projected_to_map_coord
    from app.pipeline.oom_import import OomObjectPart, _point_object
    from app.pipeline.oom_symbol_map import symbol_index_for_code

    if not points or not str(preset_id).startswith("sprint"):
        return []
    symbol_index = symbol_index_for_code(preset_id, scale, "417")
    if symbol_index is None:
        return []
    objects = []
    for x, y in points:
        if clip_bounds is not None and not point_inside(x, y, clip_bounds):
            continue
        mx, my = projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg)
        objects.append(_point_object(symbol_index, mx, my))
    if not objects:
        return []
    return [OomObjectPart(name="Stromy na zpevněné ploše (DMP OK)", objects_xml="\n".join(objects), count=len(objects))]
