"""Sprint: ulice historických center jako zpevněná plocha 501 v celé šířce mezi fasádami.

Tvar dává katastr (RÚIAN parcely se způsobem využití dálnice / silnice / ostatní komunikace), povrch potvrzuje OSM
(kus parcely se bere, jen když jím vede aspoň ``MIN_OSM_LENGTH_M`` zpevněné ulice), budovy se odečtou a zeleň
z ortofota se vyřízne. Chyba sítě = mapa se kreslí dál bez toho (funkce vrací ``None``).
"""

from __future__ import annotations

import json
import math
import urllib.parse
from pathlib import Path

from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, FetchError, crop_bounds_5514, expand_bbox_wgs84
from app.pipeline.fetch_zabaged import _http_json

# Záloha: False = ulice se kreslí jako dřív (čáry / OSM plochy).
SPRINT_STREET_PARCELS = True

RUIAN_PARCEL_QUERY = "https://ags.cuzk.gov.cz/arcgis/rest/services/RUIAN/MapServer/5/query"
# ZpusobyVyuzitiPozemku: 15 dálnice, 16 silnice, 17 ostatní komunikace.
PARCEL_USAGE_CODES = (15, 16, 17)
PARCEL_PAGE_SIZE = 1000
PARCEL_MAX_PAGES = 50
PARCEL_TIMEOUT_S = 90
CACHE_SUBDIR = "ruian_parcels"
PARCELS_NAME = "komunikace.geojson"
GREEN_PNG_NAME = "zelen.png"
GREEN_META_NAME = "zelen.json"

ORTHO_MPP = 0.5
EXG_MIN = 0.02
SAT_MIN = 0.12
STRIP_ROWS = 1024

# Zpevněná ulice: syntetické ``highway`` z přípravy OSM (road_1…4 = silnice, sidewalk = chodník / zpevněná pěšina,
# pedestrian, cycleway_paved) i surové názvy. Pěšina (path, footway bez surface), track a cycleway se nepočítají.
PAVED_HIGHWAYS = frozenset(
    {
        "sidewalk",
        "pedestrian",
        "cycleway_paved",
        "living_street",
        "residential",
        "service",
        "unclassified",
        "tertiary",
        "secondary",
        "primary",
        "trunk",
    }
)
MIN_OSM_LENGTH_M = 10.0
# Kusy menší než 0,5 mm² mapy a výběžky užší než 0,4 mm se zahodí.
MIN_AREA_MM2 = 0.5
MIN_WIDTH_MM = 0.4


def is_paved_street_highway(highway: str) -> bool:
    hw = (highway or "").lower()
    return hw.startswith("road_") or hw in PAVED_HIGHWAYS


def fetch_street_parcels(work_dir: Path, bbox_wgs84, log=None) -> Path | None:
    """Parcely komunikací (RÚIAN vrstva Parcela) v obálce výřezu → ``work_dir/ruian_parcels/komunikace.geojson``."""
    dest = Path(work_dir) / CACHE_SUBDIR / PARCELS_NAME
    if dest.is_file() and dest.stat().st_size > 20:
        return dest
    try:
        xmin, ymin, xmax, ymax = crop_bounds_5514(*expand_bbox_wgs84(tuple(bbox_wgs84), VECTOR_FETCH_BUFFER_M), buffer_m=0.0)
        features: list[dict] = []
        for page in range(PARCEL_MAX_PAGES):
            params = {
                "geometry": f"{xmin},{ymin},{xmax},{ymax}",
                "geometryType": "esriGeometryEnvelope",
                "inSR": "5514",
                "outSR": "5514",
                "spatialRel": "esriSpatialRelIntersects",
                "where": "zpusobyvyuzitipozemku IN (%s)" % ",".join(str(c) for c in PARCEL_USAGE_CODES),
                "outFields": "zpusobyvyuzitipozemku",
                "returnGeometry": "true",
                "f": "geojson",
                "resultOffset": str(page * PARCEL_PAGE_SIZE),
                "resultRecordCount": str(PARCEL_PAGE_SIZE),
            }
            data = _http_json(RUIAN_PARCEL_QUERY + "?" + urllib.parse.urlencode(params), PARCEL_TIMEOUT_S)
            if data.get("error"):
                raise FetchError(f"RÚIAN parcely: {data['error']}")
            batch = data.get("features") or []
            features.extend(batch)
            if not batch or not data.get("exceededTransferLimit"):
                break
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    except (FetchError, OSError, ValueError, TimeoutError) as exc:
        if log:
            log(f"⚠ Parcely komunikací z katastru: přeskočeno ({exc})")
        return None
    if log:
        log(f"Parcely komunikací z katastru: {len(features)} parcel")
    return dest


def _prepare_green_mask(work_dir: Path, bounds_5514, log=None) -> Path | None:
    """Maska zeleně z ortofota (``orthophoto.green_mask`` + morfologické otevření 1 px) → ``zelen.png`` + ``zelen.json``."""
    out_dir = Path(work_dir) / CACHE_SUBDIR
    dest = out_dir / GREEN_PNG_NAME
    if dest.is_file() and (out_dir / GREEN_META_NAME).is_file():
        return dest
    try:
        import numpy as np
        from PIL import Image
        from scipy import ndimage

        from app.pipeline.orthophoto import fetch_orthophoto, green_mask

        got = fetch_orthophoto(work_dir, bounds_5514, ORTHO_MPP, log=log)
        if got is None:
            return None
        rgb, gt = got
        height, width = rgb.shape[:2]
        mask = np.zeros((height, width), dtype=bool)
        for y0 in range(0, height, STRIP_ROWS):
            mask[y0 : y0 + STRIP_ROWS] = green_mask(rgb[y0 : y0 + STRIP_ROWS], EXG_MIN, SAT_MIN)
        mask = ndimage.binary_opening(mask, structure=np.ones((3, 3), dtype=bool))
        out_dir.mkdir(parents=True, exist_ok=True)
        Image.fromarray((mask * 255).astype(np.uint8), "L").save(dest)
        xmin, ymax = gt[0], gt[3]
        (out_dir / GREEN_META_NAME).write_text(
            json.dumps({"bounds": [xmin, ymax + gt[5] * height, xmin + gt[1] * width, ymax], "width": width, "height": height}),
            encoding="utf-8",
        )
    except Exception as exc:  # mapa se kreslí i bez zeleně
        if log:
            log(f"⚠ Zeleň z ortofota: přeskočeno ({exc})")
        return None
    return dest


def _mask_polygons(mask, x0: float, y_top: float, mpp: float, region=None):
    """bool maska → sjednocení čtverců pixelů (S-JTSK); sousední řádky se stejným během se slučují do vyšších obdélníků.
    ``region``: jen část masky (sloupce a řádky obálky), ať se nepolygonizuje celý výřez."""
    import numpy as np
    from shapely.geometry import box
    from shapely.ops import unary_union

    if region is not None:
        c0, r0, c1, r1 = region
    else:
        r0, c0 = 0, 0
        r1, c1 = mask.shape
    sub = mask[r0:r1, c0:c1]
    open_runs: dict[tuple[int, int], int] = {}
    boxes = []

    def flush(key, start, end):
        a, b = key
        boxes.append(
            box(
                x0 + (c0 + a) * mpp,
                y_top - (r0 + end) * mpp,
                x0 + (c0 + b) * mpp,
                y_top - (r0 + start) * mpp,
            )
        )

    for row in range(sub.shape[0] + 1):
        runs: set[tuple[int, int]] = set()
        if row < sub.shape[0]:
            line = np.concatenate(([False], sub[row], [False])).astype(np.int8)
            d = np.diff(line)
            starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
            runs = set(zip(starts.tolist(), ends.tolist()))
        for key in [k for k in open_runs if k not in runs]:
            flush(key, open_runs.pop(key), row)
        for key in runs:
            open_runs.setdefault(key, row)
    return unary_union(boxes) if boxes else None


def _load_green(work_dir: Path, bounds):
    """Polygonizovaná zeleň z uložené masky v obálce ``bounds`` (S-JTSK) nebo None."""
    import numpy as np
    from PIL import Image

    out_dir = Path(work_dir) / CACHE_SUBDIR
    png, meta_path = out_dir / GREEN_PNG_NAME, out_dir / GREEN_META_NAME
    if not png.is_file() or not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    xmin, ymin, xmax, ymax = meta["bounds"]
    w, h = int(meta["width"]), int(meta["height"])
    mpp_x, mpp_y = (xmax - xmin) / w, (ymax - ymin) / h
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(png) as im:
        mask = np.asarray(im.convert("L")) > 127
    bx0, by0, bx1, by1 = bounds
    c0, c1 = max(0, int((bx0 - xmin) / mpp_x)), min(w, int(math.ceil((bx1 - xmin) / mpp_x)))
    r0, r1 = max(0, int((ymax - by1) / mpp_y)), min(h, int(math.ceil((ymax - by0) / mpp_y)))
    if c1 <= c0 or r1 <= r0:
        return None
    return _mask_polygons(mask, xmin, ymax, mpp_x, region=(c0, r0, c1, r1))


def load_street_osm_lines(work_dir: Path) -> list[tuple[list[tuple[float, float]], str]]:
    """OSM čáry z přípravy (S-JTSK) jako ``(souřadnice, highway)``; ``paths_osm.geojson`` (všechny cesty) má přednost."""
    base = Path(work_dir) / "osm_paths"
    path = base / "paths_osm.geojson"
    if not path.is_file():
        path = base / "paths.geojson"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for feat in data.get("features") or []:
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") == "LineString" and len(coords) >= 2:
            out.append(([(float(c[0]), float(c[1])) for c in coords], str((feat.get("properties") or {}).get("highway") or "")))
    return out


def street_paved_geometry(work_dir: Path, buildings, osm_lines, *, scale: int, green=None):
    """Plocha ulic (S-JTSK) nebo None: parcely komunikací − budovy − zeleň, jen souvislé kusy s ≥ 10 m zpevněné OSM ulice.

    ``osm_lines``: ``[(souřadnice, highway)]``; ``green``: polygon zeleně (jinak z uložené masky ortofota)."""
    from shapely.geometry import LineString, Polygon, shape
    from shapely.ops import unary_union

    path = Path(work_dir) / CACHE_SUBDIR / PARCELS_NAME
    if not path.is_file():
        return None
    try:
        feats = json.loads(path.read_text(encoding="utf-8")).get("features") or []
    except (OSError, ValueError):
        return None
    parcels = []
    for feat in feats:
        try:
            geom = shape(feat["geometry"])
        except Exception:
            continue
        if not geom.is_empty:
            parcels.append(geom if geom.is_valid else geom.buffer(0))
    if not parcels:
        return None
    cand = unary_union(parcels)
    if buildings is not None and not buildings.is_empty:
        cand = cand.difference(buildings)
    if green is None:
        try:
            green = _load_green(work_dir, cand.bounds)
        except Exception:  # bez zeleně se kreslí dál
            green = None
    if green is not None and not green.is_empty:
        cand = cand.difference(green)
    mm = scale / 1000.0  # metrů v terénu na 1 mm mapy
    half = MIN_WIDTH_MM * mm / 2.0
    cand = cand.buffer(-half).buffer(half)
    lines = [LineString(c) for c, hw in osm_lines if is_paved_street_highway(hw) and len(c) >= 2]
    pieces = [g for g in getattr(cand, "geoms", [cand]) if isinstance(g, Polygon) and g.area >= MIN_AREA_MM2 * mm * mm]
    if not pieces or not lines:
        return None
    from shapely import STRtree

    tree = STRtree(lines)
    keep = []
    for piece in pieces:
        length = 0.0
        for k in tree.query(piece, predicate="intersects"):
            length += lines[int(k)].intersection(piece).length
            if length >= MIN_OSM_LENGTH_M:
                keep.append(piece)
                break
    return unary_union(keep) if keep else None


def load_buildings_5514(work_dir: Path):
    """Budovy z OSM (``osm_paths/buildings.geojson``) doplněné budovami z DTM, mají-li je (S-JTSK) nebo None."""
    from shapely.geometry import shape
    from shapely.ops import unary_union

    gj = Path(work_dir) / "osm_paths" / "buildings.geojson"
    if not gj.is_file():
        return None
    try:
        feats = list(json.loads(gj.read_text(encoding="utf-8")).get("features") or [])
        from app.pipeline.dtm_import import dtm_owns_buildings, merge_dtm_buildings

        if dtm_owns_buildings(work_dir):
            feats = merge_dtm_buildings(work_dir, feats)
    except Exception:
        return None
    polys = []
    for feat in feats:
        try:
            g = shape(feat["geometry"])
        except Exception:
            continue
        if not g.is_empty and g.geom_type in {"Polygon", "MultiPolygon"}:
            polys.append(g if g.is_valid else g.buffer(0))
    return unary_union(polys) if polys else None


def sprint_street_parcel_part(
    parts,
    work_dir: Path,
    bbox_wgs84,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    clip_bounds=None,
    log=None,
):
    """Sprint: seznam částí s přidanou plochou ulic z katastru (501) na začátku (pod ostatními objekty).

    Od plochy se odečte už kreslená voda (301) a oliva (520). Bez sítě / dat / symbolu vrací ``parts`` beze změny."""
    if not SPRINT_STREET_PARCELS or not preset_id.startswith("sprint"):
        return parts
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union

        from app.pipeline.oom_coords import projected_to_map_coord
        from app.pipeline.oom_import import OomObjectPart, _area_object_with_holes
        from app.pipeline.oom_symbol_map import symbol_index_for_code
        from app.pipeline.oom_symbols import symbol_set_path
        from app.pipeline.osm_paths import feature_oom_code
        from app.pipeline.path_clearance import (
            _COORDS_RE,
            _OBJECT_RE,
            EPS,
            _parse_coords,
            _polygon,
            _rings,
            _rounded_pieces,
            _symbol_info,
        )

        sym = symbol_index_for_code(preset_id, scale, feature_oom_code("pedestrian_area", preset_id, "501"))
        if sym is None:
            return parts
        if fetch_street_parcels(work_dir, bbox_wgs84, log=log) is None:
            return parts
        xmin, ymin, xmax, ymax = crop_bounds_5514(*expand_bbox_wgs84(tuple(bbox_wgs84), VECTOR_FETCH_BUFFER_M), buffer_m=0.0)
        _prepare_green_mask(work_dir, (xmin, ymin, xmax, ymax), log=log)
        geom = street_paved_geometry(
            work_dir, load_buildings_5514(work_dir), load_street_osm_lines(work_dir), scale=scale
        )
        if geom is not None and clip_bounds:
            from shapely.geometry import box as _box

            geom = geom.intersection(_box(*clip_bounds))  # parcely se stahují s přesahem
        if geom is None or geom.is_empty:
            return parts

        def to_map(g):
            from shapely.ops import transform

            return transform(
                lambda x, y, z=None: tuple(
                    zip(*[projected_to_map_coord(a, b, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg) for a, b in zip(x, y)])
                ),
                g,
            )

        area = to_map(geom)
        info = _symbol_info(symbol_set_path(preset_id, scale))
        covered = []
        for part in parts:
            for m in _OBJECT_RE.finditer(part.objects_xml):
                if m.group(2) != "1" or info.get(int(m.group(3)), (0, ""))[0] not in {301, 520}:
                    continue
                cm = _COORDS_RE.search(m.group(4))
                poly = _polygon(_rings(_parse_coords(cm.group(1)))) if cm else None
                if poly is not None and not poly.is_empty:
                    covered.append(poly if poly.is_valid else poly.buffer(0))
        if covered:
            # objekty z mapy bývají po zaokrouhlení na µm mírně neplatné (TopologyException v difference)
            area = (area if area.is_valid else area.buffer(0)).difference(unary_union(covered).buffer(0))
        objs = []
        for g in getattr(area, "geoms", [area]):
            if isinstance(g, Polygon) and not g.is_empty:
                for pts in _rounded_pieces(g.simplify(EPS)):
                    rings = [[(p[0], p[1]) for p in ring] for ring in _rings(pts)]
                    obj = _area_object_with_holes(sym, rings)
                    if obj:
                        objs.append(obj)
    except Exception as exc:  # kosmetika, mapa se kreslí i bez ní
        if log:
            log(f"⚠ Ulice z katastru: přeskočeno ({exc})")
        return parts
    if not objs:
        return parts
    if log:
        log(f"Ulice z katastru (501): {len(objs)} ploch")
    return [OomObjectPart(name="Sprint – ulice z katastru (501)", objects_xml="\n".join(objs), count=len(objs))] + list(parts)
