"""DTM kraje v pipeline: ploty z ZPS_KR (sprint) – příprava na pozadí a objekty do .omap.

1. ``start_dtm_prefetch`` (vlákno hned na začátku jobu): najde balíky pro výřez
   (``dtm_sources``), stáhne je do cache, načte ploty (``dtm_fences``), napojí je
   a uloží do ``work/dtm/fences.json``.
2. ``build_dtm_fence_parts`` (sestavení mapy): objekty ploty 518 / živý plot 416.
3. ``subtract_dtm_from_osm_fences``: OSM ploty se ořežou tam, kde už je zaměřený
   plot z DTM (ZABAGED / OSM jsou méně přesné; priorita DTM).

Chyba (výpadek DMVS, oblast bez DTM) nikdy neshodí job – jen se zaloguje a mapa
zůstane z OSM.
"""

from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from app.pipeline import dtm_fences, dtm_sources
from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514
from app.pipeline.geom_clip import Bounds, clip_polyline
from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import OomObjectPart
from app.pipeline.oom_symbol_map import symbol_index_for_code

FENCES_NAME = "fences.json"
SIDEWALKS_NAME = "sidewalks.json"
# OSM plot do této vzdálenosti (m) od plotu z DTM se bere jako duplicitní.
OSM_DEDUP_BUFFER_M = 3.0
# Zbytek OSM plotu po ořezu kratší než toto (m) se zahodí (střepy).
OSM_MIN_REMAINDER_M = 2.0


def fences_path(work_dir: Path) -> Path:
    return work_dir / "dtm" / FENCES_NAME


class DtmPrefetch:
    """Příprava dat DTM na pozadí; ``result()`` počká a vrátí cestu k ``fences.json`` (nebo None)."""

    def __init__(self, future: Future, executor: ThreadPoolExecutor) -> None:
        self.future = future
        self._executor = executor

    def result(self) -> Path | None:
        try:
            return self.future.result()
        finally:
            self._executor.shutdown(wait=False)


def prepare_dtm_fences(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log=None,
    force: bool = False,
) -> Path | None:
    """Ploty z DTM pro výřez → ``work_dir/dtm/fences.json``; None, když data nejsou."""
    west, south, east, north = bbox_wgs84
    packages = dtm_sources.find_packages_for_bbox(
        bbox_wgs84, sets=(dtm_sources.SET_KR, dtm_sources.SET_OPL), force=force, log=log
    )
    if not packages:
        if log:
            log("DTM: pro výřez nejsou datové sady (ploty zůstanou z OSM)")
        return None
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    # Stažení více balíků souběžně (I/O); parsování a cache JSON pak postupně.
    files = dtm_sources.download_packages(packages, log=log)
    kr_packages = [p for p in packages if p.set_name == dtm_sources.SET_KR]
    if not kr_packages:
        return None
    all_fences: list[dtm_fences.Fence] = []
    for pkg in kr_packages:
        all_fences.extend(
            dtm_fences.fences_in_bounds(dtm_fences.load_fences(files[pkg.filename]), bounds)
        )
    oriented = False
    dubious = 0
    areas: dict[str, list] = {"olive": [], "public": [], "building": [], "open": []}
    try:
        for pkg in (p for p in packages if p.set_name == dtm_sources.SET_OPL):
            part = dtm_fences.load_orientation_areas(files[pkg.filename])
            for key in areas:
                areas[key].extend(part.get(key, []))
    except Exception as exc:  # plochy jsou jen doplněk, ploty se kreslí i bez nich
        if log:
            log(f"DTM: plochy z OPL nelze načíst ({type(exc).__name__}: {exc})")
    if areas["olive"] or areas["building"]:
        # Podezřelé „ploty“ (stará migrace s neznámým původem, jen hranice jiného objektu).
        all_fences, removed = dtm_fences.filter_dubious_fences(all_fences, areas)
        dubious = len(removed)
        if removed and log:
            ids = ", ".join(f.oid for f in removed[:3] if f.oid)
            log(f"DTM ploty: vynecháno {dubious} podezřelých úseků (neznámý původ, bez soukromé hranice; např. {ids})")
    merged = dtm_fences.merged_fences(all_fences)
    inner_dropped = 0
    try:
        merged, inner_dropped = dtm_fences.drop_fences_inside_enclosures(merged)
        if inner_dropped and log:
            log(f"DTM ploty: zahozeno {inner_dropped} plotů uvnitř většího oplocení")
    except Exception as exc:  # filtr je doplněk, ploty se kreslí i bez něj
        if log:
            log(f"DTM: filtr plotů uvnitř oplocení přeskočen ({type(exc).__name__}: {exc})")
    fill_rings: list[list[tuple[float, float]]] = []
    try:
        fill_rings = dtm_fences.private_enclosure_rings(merged, areas)
        if log:
            log(f"DTM ploty: {len(fill_rings)} oplocení k vyplnění olivou")
    except Exception as exc:  # výplň olivou je doplněk
        if log:
            log(f"DTM: výplň oplocení přeskočena ({type(exc).__name__}: {exc})")
    try:
        if areas["olive"] or areas["public"]:
            merged = dtm_fences.orient_fences(
                merged, areas, bounds=bounds, setback_m=dtm_fences.FENCE_SETBACK_M
            )
            oriented = True
    except Exception as exc:  # orientace je jen kosmetika
        if log:
            log(f"DTM: orientace plotů přeskočena ({type(exc).__name__}: {exc})")
    try:
        _write_sidewalks(work_dir, [files[p.filename] for p in packages if p.set_name == dtm_sources.SET_OPL], bounds, log)
    except Exception as exc:  # chodníky z DTM jsou doplněk, kreslí se i z OSM
        if log:
            log(f"DTM: chodníky přeskočeny ({type(exc).__name__}: {exc})")
    dest = fences_path(work_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(
            {
                "packages": [p.filename for p in packages],
                "source_pieces": len(all_fences),
                "oriented": oriented,
                "dubious_removed": dubious,
                "inner_removed": inner_dropped,
                "opl_files": [str(files[p.filename]) for p in packages if p.set_name == dtm_sources.SET_OPL],
                "fences": [{"code": code, "coords": [list(p) for p in line]} for code, line in merged],
                "fills": [[list(p) for p in ring] for ring in fill_rings],
            }
        ),
        encoding="utf-8",
    )
    if log:
        log(f"DTM ploty: {len(all_fences)} úseků → {len(merged)} napojených čar ({len(packages)} balíků)")
    return dest


def sidewalks_path(work_dir: Path) -> Path:
    return work_dir / "dtm" / SIDEWALKS_NAME


def _write_sidewalks(work_dir: Path, opl_files: list, bounds: Bounds, log=None) -> None:
    """Plochy chodníků z DTM (úroveň 0) zasahující do výřezu → ``work_dir/dtm/sidewalks.json``."""
    from app.pipeline import dtm_sidewalks

    xmin, ymin, xmax, ymax = bounds
    polys = []
    for name in opl_files:
        for poly in dtm_sidewalks.load_sidewalk_polygons(name):
            xs = [p[0] for p in poly[0]]
            ys = [p[1] for p in poly[0]]
            if max(xs) < xmin or min(xs) > xmax or max(ys) < ymin or min(ys) > ymax:
                continue
            polys.append(poly)
    dest = sidewalks_path(work_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not polys:
        dest.unlink(missing_ok=True)
        return
    dest.write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in poly] for poly in polys]}), encoding="utf-8"
    )
    if log:
        log(f"DTM chodníky: {len(polys)} ploch")


def _load_sidewalk_polygons(work_dir: Path) -> list[list[list[tuple[float, float]]]]:
    try:
        data = json.loads(sidewalks_path(work_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [[[(float(x), float(y)) for x, y in ring] for ring in poly] for poly in data.get("polygons") or []]


def dtm_owns_sidewalks(work_dir: Path) -> bool:
    """Má výřez plochy chodníků z DTM? Pak chodníky kreslí ``build_dtm_sidewalk_parts``."""
    return sidewalks_path(work_dir).is_file()


def _path_features(work_dir: Path, path_source: str) -> list[dict]:
    from app.pipeline import osm_paths as op

    src = op.resolve_path_source(path_source)
    name = {
        op.PATH_SOURCE_ZABAGED: "paths_bridges.geojson",
        op.PATH_SOURCE_OSM: "paths_osm.geojson",
    }.get(src, "paths_mixed.geojson")
    gj_path = work_dir / "osm_paths" / name
    if not gj_path.is_file():
        gj_path = work_dir / "osm_paths" / "paths.geojson"
    if not gj_path.is_file():
        return []
    try:
        return json.loads(gj_path.read_text(encoding="utf-8")).get("features") or []
    except (OSError, ValueError):
        return []


def sidewalk_cover_geometry(work_dir: Path):
    """Plocha chodníků z DTM rozšířená o ``OSM_COVER_M`` (shapely) nebo None."""
    from app.pipeline import dtm_sidewalks

    dtm = dtm_sidewalks.dtm_union(_load_sidewalk_polygons(work_dir))
    return dtm.buffer(dtm_sidewalks.OSM_COVER_M) if dtm is not None and not dtm.is_empty else None


def is_surface_sidewalk(highway: str) -> bool:
    """Zpevněný chodník na povrchu (ne lávka, tunel, schody ani nezpevněná pěšina)."""
    from app.pipeline import osm_paths as op

    return (
        op.path_draw_highway(highway) == "sidewalk"
        and not op.is_bridge_highway(highway)
        and not op.is_tunnel_highway(highway)
        and highway != "steps"
    )


def sidewalk_osm_lines(work_dir: Path, path_source: str) -> list[list[tuple[float, float]]]:
    """Osy chodníků z OSM / ZABAGED (podle zdroje cest), které se sjednocují s DTM."""
    lines = []
    for feat in _path_features(work_dir, path_source):
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") != "LineString" or len(coords) < 2:
            continue
        if is_surface_sidewalk(str((feat.get("properties") or {}).get("highway") or "path")):
            lines.append([(float(x), float(y)) for x, y in coords])
    return lines


def build_dtm_sidewalk_parts(
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    clip_bounds: Bounds | None = None,
    path_source: str = "mixed",
) -> list[OomObjectPart]:
    """Chodníky (jen sprint) jako jedna plocha 501.0: DTM tvar + doplňky z OSM / ZABAGED."""
    from shapely.geometry import box

    from app.pipeline import dtm_sidewalks
    from app.pipeline.oom_import import _area_object_with_holes

    if not preset_id.startswith("sprint") or not dtm_owns_sidewalks(work_dir):
        return []
    polys = _load_sidewalk_polygons(work_dir)
    if not polys:
        return []
    geom = dtm_sidewalks.unify_sidewalks(polys, sidewalk_osm_lines(work_dir, path_source))
    if geom is None:
        return []
    if clip_bounds is not None:
        geom = geom.intersection(box(*clip_bounds))
    idx = symbol_index_for_code(preset_id, scale, "501.0")
    if idx is None:
        return []
    objects = []
    for rings in dtm_sidewalks.polygon_rings(geom):
        mapped = [
            [
                projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg)
                for x, y in ring
            ]
            for ring in rings
        ]
        obj = _area_object_with_holes(idx, mapped)
        if obj:
            objects.append(obj)
    if not objects:
        return []
    return [OomObjectPart(name="Chodníky (DTM + OSM)", objects_xml="\n".join(objects), count=len(objects))]


def start_dtm_prefetch(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log=None,
    force: bool = False,
) -> DtmPrefetch:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dtm-dl")

    def _run() -> Path | None:
        try:
            return prepare_dtm_fences(work_dir, bbox_wgs84, log=log, force=force)
        except Exception as exc:  # výpadek DMVS / poškozený balík nesmí shodit job
            if log:
                log(f"DTM: ploty přeskočeny ({type(exc).__name__}: {exc})")
            return None

    if log:
        log("Start přípravy DTM (ploty) na pozadí.")
    return DtmPrefetch(executor.submit(_run), executor)


def _load_fence_data(
    work_dir: Path,
) -> tuple[list[tuple[str, list[tuple[float, float]]]], list[list[tuple[float, float]]]]:
    """``(ploty, obrysy oplocení k vyplnění olivou)`` z ``fences.json``."""
    path = fences_path(work_dir)
    if not path.is_file():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    lines = []
    for f in data.get("fences") or []:
        coords = [(float(x), float(y)) for x, y in f.get("coords") or []]
        if len(coords) >= 2:
            lines.append((str(f.get("code") or dtm_fences.CODE_FENCE), coords))
    fills = []
    for ring in data.get("fills") or []:
        pts = [(float(x), float(y)) for x, y in ring]
        if len(pts) >= 4:
            fills.append(pts)
    return lines, fills


def load_fence_lines(work_dir: Path) -> list[tuple[str, list[tuple[float, float]]]]:
    return _load_fence_data(work_dir)[0]


def path_clearances(work_dir: Path, path_source: str) -> list[tuple[list[tuple[float, float]], float]]:
    """Osy kreslených cest a požadovaná vzdálenost osy plotu od nich (m).

    Černě kreslené cesty (pěšina, chodník, schody …) mají mezeru navíc; u asfaltových
    (silnice, zpevněná cyklostezka) stačí plot nezasahovat do osy – dotyk s okrajem je
    menší problém. Tunely se přeskakují.
    """
    from app.pipeline import osm_paths as op

    src = op.resolve_path_source(path_source)
    name = {
        op.PATH_SOURCE_ZABAGED: "paths_bridges.geojson",
        op.PATH_SOURCE_OSM: "paths_osm.geojson",
    }.get(src, "paths_mixed.geojson")
    gj_path = work_dir / "osm_paths" / name
    if not gj_path.is_file():
        gj_path = work_dir / "osm_paths" / "paths.geojson"
    if not gj_path.is_file():
        return []
    try:
        data = json.loads(gj_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for feat in data.get("features") or []:
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") != "LineString" or len(coords) < 2:
            continue
        hw = str((feat.get("properties") or {}).get("highway") or "path")
        if op.is_tunnel_highway(hw):
            continue
        base = op.path_draw_highway(hw)
        half = op.drawn_width_m(hw) / 2.0
        asphalt = base.startswith("road_") or base == "cycleway_paved"
        if asphalt:
            need = half + 0.2
        else:
            need = half + dtm_fences.FENCE_HALF_WIDTH_M + dtm_fences.PATH_GAP_M
        out.append(([(float(x), float(y)) for x, y in coords], need))
    return out


def dtm_owns_fences(work_dir: Path) -> bool:
    """Jsou pro výřez data DTM? Pak ploty (DTM i OSM) kreslí společně ``build_dtm_fence_parts``."""
    return fences_path(work_dir).is_file()


def _osm_fence_lines(work_dir: Path) -> list[tuple[str, list[tuple[float, float]]]]:
    """OSM ploty (``kind == "fence"``) po ořezu o ploty z DTM, s napojenými konci."""
    gj_path = work_dir / "osm_paths" / "features.geojson"
    if not gj_path.is_file():
        return []
    try:
        feats = json.loads(gj_path.read_text(encoding="utf-8")).get("features") or []
    except (OSError, ValueError):
        return []
    feats = [f for f in feats if (f.get("properties") or {}).get("kind") == "fence"]
    feats = subtract_dtm_from_osm_fences(feats, dtm_fence_buffer(work_dir))
    pieces = []
    for feat in feats:
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") == "LineString" and len(coords) >= 2:
            pieces.append([(float(x), float(y)) for x, y in coords])
    return [(dtm_fences.CODE_FENCE, ln) for ln in dtm_fences.merge_lines(pieces)]


def _load_opl_areas(work_dir: Path) -> dict[str, list]:
    areas: dict[str, list] = {"olive": [], "public": [], "building": [], "open": []}
    try:
        data = json.loads(fences_path(work_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return areas
    for name in data.get("opl_files") or []:
        try:
            part = dtm_fences.load_orientation_areas(name)
        except Exception:  # plochy jsou doplněk (orientace, výplň)
            continue
        for key in areas:
            areas[key].extend(part.get(key, []))
    return areas


def build_dtm_fence_parts(
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    clip_bounds: Bounds | None = None,
    path_source: str = "mixed",
) -> list[OomObjectPart]:
    """Objekty plotů (jen sprint) z DTM i OSM: jedna síť, společné zpracování.

    OSM ploty (po ořezu o DTM) se spojí, vnitřní se zahodí, otočí se čárky k olivě,
    oplocení se vyplní olivou a ploty se odsunou od kreslených cest – stejně jako ploty
    z DTM (ty jsou spojené, filtrované a orientované už při přípravě).
    """
    from app.pipeline.oom_import import _area_object_with_holes
    from app.pipeline.osm_paths import _path_object

    if not preset_id.startswith("sprint") or not dtm_owns_fences(work_dir):
        return []
    dtm_lines, fill_rings = _load_fence_data(work_dir)
    osm_lines = _osm_fence_lines(work_dir)
    areas = _load_opl_areas(work_dir)
    lines = list(dtm_lines)
    if osm_lines:
        try:
            tagged = [(c, ln, False) for c, ln in dtm_lines] + [(c, ln, True) for c, ln in osm_lines]
            dropped = dtm_fences.fences_inside_enclosures([(c, ln) for c, ln, _o in tagged])
            tagged = [item for i, item in enumerate(tagged) if i not in dropped]
            keep_dtm = [(c, ln) for c, ln, is_osm in tagged if not is_osm]
            keep_osm = [(c, ln) for c, ln, is_osm in tagged if is_osm]
            if keep_osm and (areas["olive"] or areas["public"]):
                keep_osm = dtm_fences.orient_fences(keep_osm, areas)
            lines = keep_dtm + keep_osm
        except Exception:  # sjednocení je doplněk, ploty se kreslí i bez něj
            lines = dtm_lines + osm_lines
    if not lines:
        return []
    if osm_lines and (areas["olive"] or areas["building"]):
        try:
            fill_rings = dtm_fences.private_enclosure_rings(lines, areas)
        except Exception:
            pass
    try:
        paths = path_clearances(work_dir, path_source)
        if paths:
            # Obrysy výplní se posouvají stejnou funkcí polohy jako ploty → zůstanou na plotech.
            moved = dtm_fences.avoid_paths(lines + [("520", r) for r in fill_rings], paths)
            lines, fill_rings = moved[: len(lines)], [r for _c, r in moved[len(lines) :]]
    except Exception:  # odsun je kosmetika, ploty se kreslí i bez něj
        pass
    symbol_cache: dict[str, int | None] = {}
    objects: list[str] = []
    area_objects: list[str] = []
    olive_idx = symbol_index_for_code(preset_id, scale, "520")
    if olive_idx is not None:
        for raw_ring in fill_rings:
            ring = _fill_ring(raw_ring, clip_bounds)
            if not ring:
                continue
            mapped_ring = [
                projected_to_map_coord(
                    x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg
                )
                for x, y in ring
            ]
            obj = _area_object_with_holes(olive_idx, [mapped_ring])
            if obj:
                area_objects.append(obj)
    for code, line in lines:
        if code not in symbol_cache:
            symbol_cache[code] = symbol_index_for_code(preset_id, scale, code)
        idx = symbol_cache[code]
        if idx is None:
            continue
        for piece in clip_polyline(line, clip_bounds) if clip_bounds else [line]:
            mapped = [
                projected_to_map_coord(
                    x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg
                )
                for x, y in piece
            ]
            obj = _path_object(idx, mapped)
            if obj:
                objects.append(obj)
    parts: list[OomObjectPart] = []
    if area_objects:
        parts.append(
            OomObjectPart(
                name="DTM – oplocené plochy", objects_xml="\n".join(area_objects), count=len(area_objects)
            )
        )
    if objects:
        parts.append(OomObjectPart(name="Ploty (DTM + OSM)", objects_xml="\n".join(objects), count=len(objects)))
    return parts


def _fill_ring(line, clip_bounds):
    """Vnější obrys oplocení (ořezaný na výřez) pro výplň; prázdný seznam, když nic nezbude."""
    from shapely.geometry import Polygon, box

    poly = dtm_fences._closed_ring_polygon(line)
    if poly is None:
        return []
    if clip_bounds:
        poly = poly.intersection(box(*clip_bounds))
        if poly.is_empty:
            return []
        if poly.geom_type != "Polygon":
            parts = [g for g in getattr(poly, "geoms", []) if g.geom_type == "Polygon"]
            if not parts:
                return []
            poly = max(parts, key=lambda g: g.area)
    return [(float(x), float(y)) for x, y in poly.exterior.coords]


def dtm_fence_buffer(work_dir: Path, buffer_m: float = OSM_DEDUP_BUFFER_M):
    """Sjednocený pás kolem plotů z DTM (Shapely), nebo None."""
    lines = load_fence_lines(work_dir)
    if not lines:
        return None
    try:
        from shapely.geometry import LineString
        from shapely.ops import unary_union
    except ImportError:
        return None
    return unary_union([LineString(line).buffer(buffer_m) for _code, line in lines])


def subtract_dtm_from_osm_fences(feats: list[dict], buffer) -> list[dict]:
    """OSM ploty (``kind == "fence"``) bez částí, které leží u plotu z DTM."""
    if buffer is None:
        return feats
    from shapely.geometry import LineString

    out: list[dict] = []
    for feat in feats:
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        if props.get("kind") != "fence" or geom.get("type") != "LineString":
            out.append(feat)
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            out.append(feat)
            continue
        line = LineString(coords)
        rest = line.difference(buffer)
        if rest.is_empty:
            continue
        if rest.equals(line):
            out.append(feat)
            continue
        pieces = list(rest.geoms) if hasattr(rest, "geoms") else [rest]
        for piece in pieces:
            if piece.geom_type != "LineString" or piece.length < OSM_MIN_REMAINDER_M:
                continue
            out.append({**feat, "geometry": {"type": "LineString", "coordinates": [list(c) for c in piece.coords]}})
    return out
