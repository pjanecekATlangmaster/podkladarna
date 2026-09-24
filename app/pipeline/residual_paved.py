"""Inverzní zpevněná plocha: landuse=residential − VŠECHNY OSM objekty → 501.

Maska se bere samostatným Overpass dumpem všech way / multipolygon / tagovaných
node v AOI — i objektů, které Podkladárna jinak nestahuje ani nekreslí
(např. amenity=school). Cesty mají buffer s rezervou kvůli černému obrysu.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app.pipeline.crs_5514 import wgs84_to_projected
from app.pipeline.fetch_openzu import USER_AGENT, VECTOR_FETCH_BUFFER_M, expand_bbox_wgs84
from app.pipeline.geom_diff import (
    difference_polygon_wkb,
    rings_to_polygon_wkb,
    union_polygon_wkbs,
)
from app.pipeline.oom_import import OomObjectPart, _geom_parts_to_objects, _wkb_parts
from app.pipeline.oom_symbol_map import symbol_index_for_code
from app.pipeline.osm_paths import (
    OVERPASS_URLS,
    ROAD_DRAW_HIGHWAYS,
    osm_area_polygons_5514,
    osm_perimeter_lines_5514,
    path_draw_highway,
    sprint_line_highway,
)

logger = logging.getLogger(__name__)

_PART_NAME = "OSM – residential zbytek (501)"
_CACHE_NAME = "residual_osm_all.json"
_WKB_CACHE_NAME = "residual_subject_mask_wkb.json"

# Rezerva (m) navíc k půlšířce footprintu – černý okraj značky.
_ROAD_OUTLINE_RESERVE_M = 1.0
_NARROW_OUTLINE_RESERVE_M = 0.4
_DEFAULT_LINE_HALF_M = 0.5
_POINT_BUFFER_M = 1.2
_RESIDUAL_OVERPASS_TIMEOUT_S = 300
_RESIDUAL_HTTP_TIMEOUT_S = 320
# Když po odečtu OSM zbývá víc než tento podíl residential, oblast je
# špatně zmapovaná → 501 by přikryla KP. (Černý Most: dobrá ~18 %, špatná ~45 %.)
_MAX_RESIDUAL_FRACTION = 0.25

# Max. plocha jednoho zbytku do auto .omap (SHP má vždy všechna pásma).
RESIDUAL_SMALL_MAX_M2 = 500.0
RESIDUAL_MEDIUM_MAX_M2 = 2_000.0
RESIDUAL_SIZE_CHOICES = frozenset({"small", "medium", "large"})
RESIDUAL_SHP_BANDS: tuple[tuple[str, float, float], ...] = (
    ("OSM_residential_zbytek_mensi", 0.0, RESIDUAL_SMALL_MAX_M2),
    (
        "OSM_residential_zbytek_stredni",
        RESIDUAL_SMALL_MAX_M2,
        RESIDUAL_MEDIUM_MAX_M2,
    ),
    ("OSM_residential_zbytek_velke", RESIDUAL_MEDIUM_MAX_M2, float("inf")),
)
RESIDUAL_SPARSE_STEM = "OSM_residential_zbytek_ridke"
_RESIDUAL_BANDS_DIR = "_residual_bands"

# Tagované plochy (uzavřený way / multipolygon) → plný polygon do masky.
_AREA_TAG_KEYS = frozenset(
    {
        "building",
        "landuse",
        "amenity",
        "leisure",
        "natural",
        "water",
        "waterway",
        "shop",
        "tourism",
        "historic",
        "man_made",
        "aeroway",
        "military",
        "office",
        "craft",
        "healthcare",
        "public_transport",
        "place",
        "boundary",
    }
)


def path_mask_half_width_m(highway: str) -> float:
    """Půlšířka bufferu kolem střednice (fill + rezerva na černý obrys)."""
    hw = path_draw_highway(highway or "path")
    if hw in ROAD_DRAW_HIGHWAYS or (highway or "").startswith("road_"):
        raw = highway or hw
        try:
            rank = int(str(raw).split("_", 1)[1])
        except (IndexError, ValueError):
            rank = 2
        fill = {1: 2.0, 2: 2.5, 3: 3.0, 4: 3.5}.get(max(1, min(4, rank)), 2.5)
        return fill + _ROAD_OUTLINE_RESERVE_M
    if hw == "cycleway_paved":
        return 2.0 + 0.8
    if hw in {"sidewalk", "pedestrian"}:
        return 0.7 + _ROAD_OUTLINE_RESERVE_M
    if hw in {"cycleway", "bridleway"}:
        return 0.8 + _NARROW_OUTLINE_RESERVE_M
    if hw in {"footway", "path", "steps"}:
        return 0.5 + _NARROW_OUTLINE_RESERVE_M
    if hw in {"track", "track_fast", "track_slow"} or str(hw).startswith("track"):
        return 1.5 + _ROAD_OUTLINE_RESERVE_M
    return 1.0 + 0.5


def resolve_residual_size(options: dict | None) -> str:
    raw = str((options or {}).get("sprint_residual_size") or "small").strip().lower()
    return raw if raw in RESIDUAL_SIZE_CHOICES else "small"


def residual_max_m2(choice: str) -> float:
    """Horní limit plochy zbytku do auto .omap (large = bez limitu)."""
    c = (choice or "small").strip().lower()
    if c == "large":
        return float("inf")
    if c == "medium":
        return RESIDUAL_MEDIUM_MAX_M2
    return RESIDUAL_SMALL_MAX_M2


def residual_band_stem(area_m2: float) -> str:
    if area_m2 <= RESIDUAL_SMALL_MAX_M2:
        return RESIDUAL_SHP_BANDS[0][0]
    if area_m2 <= RESIDUAL_MEDIUM_MAX_M2:
        return RESIDUAL_SHP_BANDS[1][0]
    return RESIDUAL_SHP_BANDS[2][0]


def build_residual_paved_parts(
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    bbox_wgs84: tuple[float, float, float, float] | None = None,
    max_piece_m2: float = RESIDUAL_SMALL_MAX_M2,
    write_shapefiles: bool = True,
) -> list[OomObjectPart]:
    """Vrátí OOM part se zbytkem 501, nebo [] když nic / ne-sprint.

    Do ``work_dir/_residual_bands/`` zapíše SHP podle velikosti + řídké
    (přeskočené) zbytky – pro ruční import i když nejsou v auto .omap.
    """
    if not str(preset_id).startswith("sprint"):
        return []
    elements = _load_or_fetch_all_osm(work_dir, bbox_wgs84)
    subjects, mask_wkbs = _load_or_build_subject_mask(work_dir, elements)
    if not subjects:
        subjects = _residential_from_features(work_dir)
    if not subjects:
        return []
    if not mask_wkbs:
        logger.warning("residual_paved: prázdná OSM maska – residential bez výřezu")
    symbol_index = symbol_index_for_code(preset_id, scale, "501")
    if symbol_index is None:
        return []

    kept_by_band: dict[str, list[bytes]] = {stem: [] for stem, _, _ in RESIDUAL_SHP_BANDS}
    sparse_wkbs: list[bytes] = []
    objects: list[str] = []
    total_m2 = 0.0
    kept_subjects = 0
    skipped_sparse = 0
    skipped_oversized = 0

    for subject in subjects:
        pieces = _difference_subjects([subject], mask_wkbs)
        exploded = _explode_polygon_wkbs(pieces)
        subj_area = _wkb_area_m2(subject)
        rem_area = sum(_wkb_area_m2(p) for p in exploded)
        if (
            subj_area > 0
            and rem_area / subj_area > _MAX_RESIDUAL_FRACTION
        ):
            skipped_sparse += 1
            sparse_wkbs.extend(exploded)
            logger.info(
                "residual_paved: přeskočen řídký residential "
                "(zbytek %.0f/%.0f m² = %.0f %% > %.0f %%)",
                rem_area,
                subj_area,
                100.0 * rem_area / subj_area,
                100.0 * _MAX_RESIDUAL_FRACTION,
            )
            continue
        kept_subjects += 1
        for piece in exploded:
            area = _wkb_area_m2(piece)
            if area <= 0:
                continue
            kept_by_band[residual_band_stem(area)].append(piece)
            if area > max_piece_m2:
                skipped_oversized += 1
                continue
            total_m2 += area
            geom_parts, _ = _wkb_parts(piece)
            objects.extend(
                _geom_parts_to_objects(
                    geom_parts,
                    symbol_index,
                    ref_x=ref_x,
                    ref_y=ref_y,
                    scale=scale,
                    grivation_deg=grivation_deg,
                    as_area=True,
                )
            )

    if write_shapefiles:
        write_residual_band_shapefiles(
            work_dir,
            kept_by_band=kept_by_band,
            sparse_wkbs=sparse_wkbs,
        )

    if not objects:
        logger.info(
            "residual_paved: nic k vykreslení "
            "(řídkých=%s, příliš velkých=%s, subjectů=%s)",
            skipped_sparse,
            skipped_oversized,
            len(subjects),
        )
        return []
    logger.info(
        "residual_paved: %s polygonů, ~%.0f m² "
        "(OSM %s, mask %s, residential ok=%s, řídké=%s, >max=%s)",
        len(objects),
        total_m2,
        len(elements),
        len(mask_wkbs),
        kept_subjects,
        skipped_sparse,
        skipped_oversized,
    )
    return [
        OomObjectPart(
            name=_PART_NAME,
            objects_xml="\n".join(objects),
            count=len(objects),
        )
    ]


def write_residual_band_shapefiles(
    work_dir: Path,
    *,
    kept_by_band: dict[str, list[bytes]],
    sparse_wkbs: list[bytes],
) -> list[Path]:
    """Zapíše pásma mensi/stredni/velke + řídké zbytky do ``_residual_bands/``."""
    from app.pipeline.crs_5514 import write_prj

    dest_dir = work_dir / _RESIDUAL_BANDS_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    buckets: list[tuple[str, list[bytes]]] = [
        (stem, kept_by_band.get(stem) or []) for stem, _, _ in RESIDUAL_SHP_BANDS
    ]
    if sparse_wkbs:
        buckets.append((RESIDUAL_SPARSE_STEM, sparse_wkbs))

    for stem, wkbs in buckets:
        if not wkbs:
            continue
        out_shp = dest_dir / f"{stem}.shp"
        if _write_polygon_shp(out_shp, stem, wkbs):
            write_prj(out_shp)
            written.append(out_shp)
    if written:
        logger.info(
            "residual_paved: SHP pásma %s",
            ", ".join(p.stem for p in written),
        )
    return written


def _write_polygon_shp(out_shp: Path, stem: str, wkbs: list[bytes]) -> bool:
    """Zapíše polygony do SHP (osgeo, jinak pyshp)."""
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        out_shp.with_suffix(suffix).unlink(missing_ok=True)

    try:
        from osgeo import ogr

        ogr.UseExceptions()
        driver = ogr.GetDriverByName("ESRI Shapefile")
        out_ds = driver.CreateDataSource(str(out_shp))
        out_lyr = out_ds.CreateLayer(stem, srs=None, geom_type=ogr.wkbPolygon)
        out_lyr.CreateField(ogr.FieldDefn("area_m2", ogr.OFTReal))
        out_lyr.CreateField(ogr.FieldDefn("band", ogr.OFTString))
        n = 0
        for wkb in wkbs:
            geom = ogr.CreateGeometryFromWkb(wkb)
            if geom is None or geom.IsEmpty():
                continue
            polys = []
            if geom.GetGeometryName() == "MULTIPOLYGON":
                for i in range(geom.GetGeometryCount()):
                    sub = geom.GetGeometryRef(i)
                    if sub is not None and not sub.IsEmpty():
                        polys.append(sub.Clone())
            else:
                polys.append(geom)
            for poly in polys:
                feat = ogr.Feature(out_lyr.GetLayerDefn())
                feat.SetField("area_m2", float(poly.GetArea()))
                feat.SetField("band", stem)
                feat.SetGeometry(poly)
                out_lyr.CreateFeature(feat)
                n += 1
        out_ds = None
        return n > 0 and out_shp.is_file()
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("residual_paved: OGR SHP %s selhalo: %s", stem, exc)

    try:
        import shapefile
        from shapely import from_wkb
    except ImportError:
        logger.warning("residual_paved: nelze zapsat SHP %s (chybí osgeo i pyshp)", stem)
        return False

    # pyshp: rings jako seznam [x,y]; first ring = outer.
    with shapefile.Writer(str(out_shp.with_suffix("")), shapeType=shapefile.POLYGON) as w:
        w.field("area_m2", "N", decimal=3)
        w.field("band", "C", size=40)
        n = 0
        for wkb in wkbs:
            try:
                geom = from_wkb(wkb)
            except Exception:
                continue
            if geom is None or geom.is_empty:
                continue
            polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
            for poly in polys:
                if poly.is_empty or poly.area <= 0:
                    continue
                rings = []
                exterior = [[float(x), float(y)] for x, y in poly.exterior.coords]
                rings.append(exterior)
                for hole in poly.interiors:
                    rings.append([[float(x), float(y)] for x, y in hole.coords])
                w.poly(rings)
                w.record(round(float(poly.area), 3), stem)
                n += 1
    return n > 0 and out_shp.is_file()


def _explode_polygon_wkbs(wkbs: list[bytes]) -> list[bytes]:
    """MultiPolygon → jednotlivé polygony (pro pásma podle plochy)."""
    out: list[bytes] = []
    try:
        from shapely import from_wkb, to_wkb

        for wkb in wkbs:
            try:
                geom = from_wkb(wkb)
            except Exception:
                out.append(wkb)
                continue
            if geom is None or geom.is_empty:
                continue
            if geom.geom_type == "Polygon":
                out.append(bytes(to_wkb(geom, hex=False)))
            elif geom.geom_type == "MultiPolygon":
                for sub in geom.geoms:
                    if sub is None or sub.is_empty:
                        continue
                    out.append(bytes(to_wkb(sub, hex=False)))
            else:
                out.append(wkb)
        return out
    except ImportError:
        pass
    try:
        from osgeo import ogr
    except ImportError:
        return list(wkbs)
    for wkb in wkbs:
        geom = ogr.CreateGeometryFromWkb(wkb)
        if geom is None or geom.IsEmpty():
            continue
        name = geom.GetGeometryName()
        if name == "POLYGON":
            out.append(bytes(geom.ExportToWkb()))
        elif name == "MULTIPOLYGON":
            for i in range(geom.GetGeometryCount()):
                sub = geom.GetGeometryRef(i)
                if sub is None or sub.IsEmpty():
                    continue
                out.append(bytes(sub.ExportToWkb()))
        else:
            out.append(wkb)
    return out


def _wkb_area_m2(wkb: bytes) -> float:
    try:
        from osgeo import ogr

        return float(ogr.CreateGeometryFromWkb(wkb).GetArea())
    except Exception:
        pass
    try:
        from shapely import from_wkb

        return float(from_wkb(wkb).area)
    except Exception:
        return 0.0


def _load_or_build_subject_mask(
    work_dir: Path, elements: list[dict]
) -> tuple[list[bytes], list[bytes]]:
    """Subject/mask WKB – cache podle mtime OSM dumpů."""
    osm_cache = work_dir / "osm_paths" / _CACHE_NAME
    wkb_cache = work_dir / "osm_paths" / _WKB_CACHE_NAME
    if (
        elements
        and osm_cache.is_file()
        and wkb_cache.is_file()
        and wkb_cache.stat().st_mtime >= osm_cache.stat().st_mtime
    ):
        try:
            data = json.loads(wkb_cache.read_text(encoding="utf-8"))
            subjects = [base64.b64decode(s) for s in data.get("subjects") or []]
            mask = [base64.b64decode(s) for s in data.get("mask") or []]
            if subjects:
                logger.info(
                    "residual_paved: WKB cache (%s subject, %s mask)",
                    len(subjects),
                    len(mask),
                )
                return subjects, mask
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    subjects, mask = _subjects_and_mask_wkbs(elements)
    if subjects and elements:
        try:
            wkb_cache.parent.mkdir(parents=True, exist_ok=True)
            wkb_cache.write_text(
                json.dumps(
                    {
                        "subjects": [
                            base64.b64encode(s).decode("ascii") for s in subjects
                        ],
                        "mask": [base64.b64encode(m).decode("ascii") for m in mask],
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
    return subjects, mask


def _difference_subjects(
    subjects: list[bytes], mask_wkbs: list[bytes]
) -> list[bytes]:
    """Subject − maska. Maska se skládá po subjectu (STRtree), ne jeden obří Union."""
    if not mask_wkbs:
        out: list[bytes] = []
        for subject in subjects:
            out.extend(difference_polygon_wkb(subject, None))
        return out
    try:
        from shapely import STRtree, from_wkb, unary_union
    except ImportError:
        mask = union_polygon_wkbs(mask_wkbs)
        out = []
        for subject in subjects:
            out.extend(difference_polygon_wkb(subject, mask))
        return out

    mask_geoms = []
    for raw in mask_wkbs:
        try:
            g = from_wkb(raw)
        except Exception:
            continue
        if g is None or g.is_empty:
            continue
        mask_geoms.append(g)
    if not mask_geoms:
        out = []
        for subject in subjects:
            out.extend(difference_polygon_wkb(subject, None))
        return out

    tree = STRtree(mask_geoms)
    out = []
    for subject in subjects:
        try:
            subj = from_wkb(subject)
        except Exception:
            continue
        if subj is None or subj.is_empty:
            continue
        # Trochu rozšířit – buffer cest u hranice subjectu.
        try:
            hits_idx = tree.query(subj.buffer(2.0))
        except Exception:
            hits_idx = range(len(mask_geoms))
        nearby = [mask_geoms[i] for i in hits_idx]
        if not nearby:
            out.extend(difference_polygon_wkb(subject, None))
            continue
        try:
            local_mask = unary_union(nearby)
        except Exception:
            local_mask = None
            for g in nearby:
                try:
                    local_mask = g if local_mask is None else local_mask.union(g)
                except Exception:
                    continue
        if local_mask is None or local_mask.is_empty:
            out.extend(difference_polygon_wkb(subject, None))
            continue
        try:
            from shapely import to_wkb

            mask = union_polygon_wkbs([bytes(to_wkb(local_mask, hex=False))])
        except Exception:
            mask = None
        out.extend(difference_polygon_wkb(subject, mask))
    return out


def _load_or_fetch_all_osm(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float] | None,
) -> list[dict]:
    cache = work_dir / "osm_paths" / _CACHE_NAME
    if cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            els = data.get("elements")
            if isinstance(els, list) and els:
                return els
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    if bbox_wgs84 is None:
        return []
    elements = fetch_all_osm_elements(bbox_wgs84)
    if elements:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps({"elements": elements}, ensure_ascii=False),
            encoding="utf-8",
        )
    return elements


def fetch_all_osm_elements(
    bbox_wgs84: tuple[float, float, float, float],
) -> list[dict]:
    """Všechny way + multipolygon (+ tagované node) v (rozšířeném) výřezu.

    Dotazy jsou rozdělené – jeden obří ``out geom`` na město často timeoutne.
    """
    fetch_bbox = expand_bbox_wgs84(bbox_wgs84, VECTOR_FETCH_BUFFER_M)
    west, south, east, north = fetch_bbox
    bbox = f"{south},{west},{north},{east}"
    queries = [
        (
            "ways",
            f"[out:json][timeout:{_RESIDUAL_OVERPASS_TIMEOUT_S}];"
            f"way({bbox});out geom;",
        ),
        (
            "multipolygon",
            f"[out:json][timeout:{_RESIDUAL_OVERPASS_TIMEOUT_S}];"
            f'relation["type"="multipolygon"]({bbox});out geom;',
        ),
        (
            "nodes",
            f"[out:json][timeout:{_RESIDUAL_OVERPASS_TIMEOUT_S}];"
            f"node({bbox})(if:count_tags() > 0);out body;",
        ),
    ]
    all_elements: list[dict] = []
    for label, ql in queries:
        chunk = _overpass_query(ql, label=label)
        if chunk:
            all_elements.extend(chunk)
    logger.info("residual_paved OSM dump celkem: %s prvků", len(all_elements))
    return all_elements


def _overpass_query(ql: str, *, label: str) -> list[dict]:
    body = urllib.parse.urlencode({"data": ql}).encode("utf-8")
    last_err: Exception | None = None
    for url in OVERPASS_URLS:
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_RESIDUAL_HTTP_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            elements = [
                e
                for e in (data.get("elements") or [])
                if e.get("type") in {"way", "node", "relation"}
            ]
            logger.info(
                "residual_paved Overpass %s (%s): %s prvků",
                label,
                url.split("/")[2],
                len(elements),
            )
            return elements
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            last_err = exc
            logger.warning(
                "residual_paved Overpass %s %s: %s", label, url, exc
            )
            continue
    if last_err:
        logger.warning("residual_paved: OSM dump %s selhal (%s)", label, last_err)
    return []


def _subjects_and_mask_wkbs(
    elements: list[dict],
) -> tuple[list[bytes], list[bytes]]:
    subjects: list[bytes] = []
    mask: list[bytes] = []
    for el in elements:
        tags = {str(k).lower(): str(v).lower() for k, v in (el.get("tags") or {}).items()}
        el_type = el.get("type")
        if el_type == "node":
            wkb = _node_buffer_wkb(el)
            if wkb:
                mask.append(wkb)
            continue
        if el_type == "relation":
            if _is_residential(tags):
                for rings in osm_area_polygons_5514(el):
                    wkb = rings_to_polygon_wkb(rings)
                    if wkb:
                        subjects.append(wkb)
                continue
            if _wants_area(tags, closed=True):
                for rings in osm_area_polygons_5514(el):
                    wkb = rings_to_polygon_wkb(rings)
                    if wkb:
                        mask.append(wkb)
                continue
            for pts in osm_perimeter_lines_5514(el):
                wkb = _line_buffer_wkb(pts, _DEFAULT_LINE_HALF_M)
                if wkb:
                    mask.append(wkb)
            continue
        if el_type != "way":
            continue
        pts = _way_pts_5514(el)
        if len(pts) < 2:
            continue
        closed = len(pts) >= 3 and (
            pts[0] == pts[-1]
            or (
                abs(pts[0][0] - pts[-1][0]) < 0.5
                and abs(pts[0][1] - pts[-1][1]) < 0.5
            )
        )
        if _is_residential(tags) and closed:
            ring = list(pts)
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            wkb = rings_to_polygon_wkb([ring])
            if wkb:
                subjects.append(wkb)
            continue
        if _wants_area(tags, closed=closed) and closed:
            polygons = osm_area_polygons_5514(el)
            for rings in polygons:
                wkb = rings_to_polygon_wkb(rings)
                if wkb:
                    mask.append(wkb)
            continue
        half = _line_half_width_for_tags(tags)
        wkb = _line_buffer_wkb(pts, half)
        if wkb:
            mask.append(wkb)
    return subjects, mask


def _way_pts_5514(el: dict) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for node in el.get("geometry") or []:
        lat = node.get("lat")
        lon = node.get("lon")
        if lat is None or lon is None:
            continue
        pts.append(wgs84_to_projected(float(lat), float(lon)))
    return pts


def _is_residential(tags: dict) -> bool:
    return (tags.get("landuse") or "") == "residential"


def _wants_area(tags: dict, *, closed: bool) -> bool:
    area = (tags.get("area") or "").lower()
    if area in {"no", "false", "0"}:
        return False
    if area in {"yes", "true", "1"}:
        return True
    if "area:highway" in tags:
        return True
    if any(k in tags for k in _AREA_TAG_KEYS):
        # highway bez area=yes zůstává linií (i uzavřená smyčka ulice).
        if "highway" in tags and not any(
            k in tags
            for k in ("building", "landuse", "amenity", "leisure", "natural", "water")
        ):
            return False
        return closed or "building" in tags or "landuse" in tags
    return False


def _line_half_width_for_tags(tags: dict) -> float:
    if "highway" in tags or "area:highway" in tags:
        hw_raw = tags.get("highway") or tags.get("area:highway") or "path"
        draw = sprint_line_highway(tags, hw_raw)
        return path_mask_half_width_m(draw)
    barrier = tags.get("barrier") or ""
    if barrier in {"fence", "hedge", "wall", "retaining_wall"}:
        return 0.45
    if tags.get("power") in {"line", "minor_line", "cable"}:
        return 1.0
    if tags.get("railway"):
        return 1.2
    if tags.get("waterway"):
        return 1.0
    return _DEFAULT_LINE_HALF_M


def _node_buffer_wkb(el: dict) -> bytes | None:
    tags = el.get("tags") or {}
    if not tags:
        return None
    lat, lon = el.get("lat"), el.get("lon")
    if lat is None or lon is None:
        return None
    x, y = wgs84_to_projected(float(lat), float(lon))
    return _point_buffer_wkb(x, y, _POINT_BUFFER_M)


def _point_buffer_wkb(x: float, y: float, radius_m: float) -> bytes | None:
    try:
        from osgeo import ogr

        ogr.UseExceptions()
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(float(x), float(y))
        buf = pt.Buffer(radius_m, 4)
        if buf is None or buf.IsEmpty():
            return None
        return bytes(buf.ExportToWkb())
    except ImportError:
        pass
    try:
        from shapely import Point, to_wkb

        buf = Point(float(x), float(y)).buffer(radius_m, quad_segs=4)
        if buf.is_empty:
            return None
        return bytes(to_wkb(buf, hex=False))
    except ImportError:
        return None


def _line_buffer_wkb(pts: list[tuple[float, float]], half_m: float) -> bytes | None:
    if len(pts) < 2 or half_m <= 0:
        return None
    try:
        from osgeo import ogr

        ogr.UseExceptions()
        line = ogr.Geometry(ogr.wkbLineString)
        for x, y in pts:
            line.AddPoint_2D(float(x), float(y))
        buf = line.Buffer(half_m, 4)
        if buf is None or buf.IsEmpty():
            return None
        return bytes(buf.ExportToWkb())
    except ImportError:
        pass
    try:
        from shapely import LineString, to_wkb

        buf = LineString([(float(x), float(y)) for x, y in pts]).buffer(
            half_m, quad_segs=4
        )
        if buf.is_empty:
            return None
        return bytes(to_wkb(buf, hex=False))
    except Exception:
        return None


def _residential_from_features(work_dir: Path) -> list[bytes]:
    gj = work_dir / "osm_paths" / "features.geojson"
    if not gj.is_file():
        return []
    try:
        data = json.loads(gj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[bytes] = []
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        if str(props.get("kind") or "") != "residential":
            continue
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Polygon":
            continue
        raw = geom.get("coordinates") or []
        if not raw:
            continue
        rings = [[(float(x), float(y)) for x, y in ring] for ring in raw]
        wkb = rings_to_polygon_wkb(rings)
        if wkb:
            out.append(wkb)
    return out
