"""Přílivová zóna u moře mimo Česko: nízké holé ploché plochy napojené na moře jako písčitá půda (213).

OSM moře je plocha za pobřežní čárou (``osm_paths.sea_features``), ale pobřeží v zátokách často vede po korytech
a bahnité a písčité plochy odkryté odlivem zůstávaly v mapě jako louka 401 (Culbin, Findhorn Bay). Výška
pobřežní čáry je v OSM zhruba hranice velkého přílivu, proto se referenční výška bere z horní části výšek
DTM v pásu 0–3 m za mořem (``COAST_PERCENTILE``). Zóna = pixely pod ní, s CHM pod 1 m a sklonem pod 4°,
souvisle napojené na moře; mapuje se jako písčitá půda (ISOM, ISSprOM i ISMTBOM 213) – měkký terén bez porostu.

Totéž dělá OSM ``natural=beach / sand / mud`` a ``wetland=tidalflat`` (kind ``sand`` z přípravy OSM);
zóna se k nim přidá jen tam, kde OSM plochu nemá, a vše se ořízne o moře.
"""

from __future__ import annotations

import json
from pathlib import Path

GRID_M = 2.0  # krok výpočtu (DTM 0,5–1 m by byl zbytečně pomalý)
COAST_BAND_M = 3.0  # pás za mořem, ze kterého se bere výška pobřežní čáry
COAST_PERCENTILE = 90.0
MAX_ABOVE_SEA_M = 3.5  # strop referenční výšky nad mediánem DTM na moři (útesy nesmí zaplavit vnitrozemí)
MIN_COAST_SAMPLES = 50
CHM_MAX_M = 1.0
SLOPE_MAX_DEG = 4.0
MIN_AREA_M2 = 500.0
HOLE_MIN_M2 = 200.0


def _sea_geometry(features: list[dict]):
    from shapely.geometry import shape
    from shapely.ops import unary_union

    seas = [shape(f["geometry"]) for f in features if (f.get("properties") or {}).get("sea") and f.get("geometry")]
    return unary_union(seas) if seas else None


def tidal_mask(dem, chm, gt, sea, *, grid_m: float = GRID_M):
    """bool maska přílivové zóny v mřížce ``grid_m`` + (x0, y_top, krok) nebo None.

    ``dem`` / ``chm``: pole stejné mřížky (geotransform ``gt``, sever nahoře), ``sea``: shapely plocha moře.
    """
    import numpy as np
    import shapely
    from scipy import ndimage as ndi

    res = abs(float(gt[1]))
    k = max(1, int(round(grid_m / res)))
    d = np.asarray(dem, dtype="float32")[::k, ::k]
    c = np.asarray(chm, dtype="float32")[::k, ::k] if chm is not None else np.zeros_like(d)
    if c.shape != d.shape:
        return None
    step = res * k
    h, w = d.shape
    xs = gt[0] + (np.arange(w) * k + 0.5 * k) * gt[1]
    ys = gt[3] + (np.arange(h) * k + 0.5 * k) * gt[5]
    X, Y = np.meshgrid(xs, ys)
    shapely.prepare(sea)
    inside = shapely.contains_xy(sea, X, Y)
    band_geom = sea.buffer(COAST_BAND_M).difference(sea)
    shapely.prepare(band_geom)
    band = shapely.contains_xy(band_geom, X, Y)
    ok = np.isfinite(d) & (d > -100)
    coast = d[band & ok]
    water = d[inside & ok]
    if coast.size < MIN_COAST_SAMPLES or not inside.any():
        return None
    ref = float(np.percentile(coast, COAST_PERCENTILE))
    if water.size:
        ref = min(ref, float(np.median(water)) + MAX_ABOVE_SEA_M)
    gy, gx = np.gradient(ndi.uniform_filter(np.where(ok, d, ref), 3), step)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    low_bare = ok & ~inside & (d <= ref) & (np.nan_to_num(c, nan=0.0) < CHM_MAX_M)
    # Sklon u pobřeží kazí skok na hladinu moře v DTM: pás 2 kroky od moře se bere bez sklonu.
    cand = low_bare & ((slope < SLOPE_MAX_DEG) | ndi.binary_dilation(inside, iterations=2))
    lab, _n = ndi.label(cand | inside)
    keep = np.unique(lab[inside])
    keep = keep[keep > 0]
    zone = np.isin(lab, keep) & ~inside
    zone = ndi.binary_opening(zone, iterations=1)
    x0 = float(gt[0])
    y_top = float(gt[3])
    return zone, (x0, y_top, step), ref


def _mask_to_geometry(mask, origin):
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    from app.pipeline.street_parcels import _mask_polygons

    x0, y_top, step = origin
    geom = _mask_polygons(mask, x0, y_top, step)
    if geom is None or geom.is_empty:
        return None
    # Vyhlazení kostiček a vyčištění drobků.
    geom = geom.buffer(step, join_style=1).buffer(-step, join_style=1).simplify(step / 2)
    polys = []
    for p in getattr(geom, "geoms", [geom]):
        if p.area < MIN_AREA_M2:
            continue
        holes = [r for r in p.interiors if Polygon(r).area >= HOLE_MIN_M2]
        polys.append(Polygon(p.exterior, holes))
    return unary_union(polys) if polys else None


def _polygon_features(geom, props: dict) -> list[dict]:
    from shapely.geometry import mapping

    out = []
    for p in getattr(geom, "geoms", [geom]):
        if p.geom_type != "Polygon" or p.is_empty:
            continue
        out.append({"type": "Feature", "properties": dict(props), "geometry": mapping(p)})
    return out


def apply_tidal_zone(work_dir: Path, *, log=None) -> int:
    """Doplní do ``osm_paths/features.geojson`` přílivovou zónu (kind ``sand``, ``intertidal``) a ořízne písek o moře.

    Vrací počet přidaných ploch. Bez moře nebo DTM nic nemění.
    """
    from shapely.geometry import shape
    from shapely.ops import unary_union

    from app.pipeline.gdal_cli_raster import read_float32_geotiff
    from app.pipeline.vegetation_chm import chm_path

    work_dir = Path(work_dir)
    feat_path = work_dir / "osm_paths" / "features.geojson"
    dem_path = work_dir / "dem" / "dem_filled.tif"
    if not feat_path.is_file():
        return 0
    data = json.loads(feat_path.read_text(encoding="utf-8"))
    features = data.get("features") or []
    sea = _sea_geometry(features)
    if sea is None or sea.is_empty:
        return 0
    sand_idx = [i for i, f in enumerate(features) if (f.get("properties") or {}).get("kind") == "sand"]
    sand = unary_union([shape(features[i]["geometry"]) for i in sand_idx]) if sand_idx else None
    # OSM pláž často sahá pod pobřežní čáru: písek se kreslí jen na pevnině.
    kept: list[dict] = []
    for i, f in enumerate(features):
        if i not in sand_idx:
            kept.append(f)
            continue
        geom = shape(f["geometry"]).difference(sea)
        if not geom.is_empty:
            kept.extend(_polygon_features(geom, f.get("properties") or {}))
    added = 0
    if dem_path.is_file():
        dem, gt, _nd = read_float32_geotiff(dem_path)
        chm_file = chm_path(work_dir)
        chm = read_float32_geotiff(chm_file)[0] if chm_file is not None else None
        res = tidal_mask(dem, chm, gt, sea)
        if res is not None:
            zone, origin, ref = res
            geom = _mask_to_geometry(zone, origin)
            if geom is not None and sand is not None:
                geom = geom.difference(sand)
            if geom is not None and not geom.is_empty:
                new = _polygon_features(
                    geom, {"source": "dem", "kind": "sand", "oom_code": "213", "intertidal": True}
                )
                new = [f for f in new if shape(f["geometry"]).area >= MIN_AREA_M2]
                kept.extend(new)
                added = len(new)
                if log:
                    log(
                        f"Přílivová zóna: {added} ploch, {geom.area / 1e4:.1f} ha pod {ref:.2f} m "
                        "(výška pobřeží z DTM) jako písčitá půda"
                    )
            elif log:
                log(f"Přílivová zóna: žádná (pod {ref:.2f} m u moře není holá rovina)")
        elif log:
            log("Přílivová zóna: přeskočeno (u moře chybí výšky)")
    data["features"] = kept
    feat_path.write_text(json.dumps(data), encoding="utf-8")
    return added
