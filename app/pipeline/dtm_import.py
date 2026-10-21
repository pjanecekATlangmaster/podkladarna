"""DTM kraje v pipeline: ploty z ZPS_KR (sprint) – příprava na pozadí a objekty do .omap.

1. ``start_dtm_prefetch`` (vlákno hned na začátku jobu): najde balíky pro výřez
   (``dtm_sources``), stáhne je do cache, načte ploty (``dtm_fences``), napojí je
   a uloží do ``work/dtm/fences.json``.
2. ``build_dtm_fence_parts`` (sestavení mapy): objekty ploty 518 / živý plot 416.
3. ``subtract_dtm_from_osm_fences``: OSM ploty se ořežou tam, kde už je zaměřený
   plot z DTM (ZABAGED / OSM jsou méně přesné; priorita DTM).
4. Plochy z OPL (chodníky, silnice, budovy, schodiště, parkoviště, malé stavby, zeleň) se uloží do
   ``work/dtm/*.json`` a v ``build_dtm_*_parts`` se sloučí s OSM (viz DEV.md, „DTM kraje ve sprintu“).

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
ROADS_NAME = "roads.json"
BUILDINGS_NAME = "buildings.json"
SMALL_PAVED_NAME = "small_paved.json"
STAIRS_NAME = "stairs.json"
PARKING_NAME = "parking.json"
GREEN_NAME = "green.json"
DECKS_NAME = "decks.json"
# OSM budova, jejíž aspoň tento podíl plochy kryjí budovy z DTM, je duplicita (DTM má přednost).
BUILDING_TWIN_SHARE = 0.5
# Budovy z DTM, jejichž hrany jsou blíž než 2 × tato hodnota (m), se sloučí do jednoho objektu.
BUILDING_FUSE_M = 0.1
# Šířka kresby OSM silnice (m) – podle symbolu: road_1 = 501.30 (2 m), ostatní 501.31 (4 m).
ROAD_CODE = "501.20"
# OSM plot do této vzdálenosti (m) od plotu z DTM se bere jako duplicitní.
OSM_DEDUP_BUFFER_M = 3.0
# Zbytek OSM plotu po ořezu kratší než toto (m) se zahodí (střepy).
OSM_MIN_REMAINDER_M = 2.0
# Les a MTBO (1:7500–1:15000): ploty kratší než tato délka (m) se nekreslí (zahradní oplocení by zahltilo mapu).
NONSPRINT_FENCE_MIN_LEN_M = 20.0
# MTBO: parkoviště z DTM se kreslí jen od této plochy (m²), menší jsou v 1:10000–1:15000 nečitelná.
MTBO_PARKING_MIN_M2 = 300.0
# MTBO: silnice s přilehlými chodníky jednou plochou (koridor); False = jen čáry 502.x jako dřív.
MTBO_ROAD_CORRIDOR = True
# MTBO zatím DTM nepoužívá (budovy, ploty, koridor, chodníky, parkoviště, schodiště): False = MTBO se kreslí
# z ZABAGED / OSM jako před zavedením DTM. Zapnout až po odladění (viz ``mtbo_dtm_off``).
MTBO_USE_DTM = False


def mtbo_dtm_off(preset_id: str) -> bool:
    """Je to MTBO a DTM se pro něj nepoužívá (``MTBO_USE_DTM`` = False)?"""
    return preset_id.startswith("mtbo") and not MTBO_USE_DTM
# MTBO: chodníky a zpevněné cesty z DTM mimo silnice jako plocha 501.0 (False = jen čáry z OSM jako dřív);
# jen části široké aspoň tolik mm mapy a s plochou aspoň ``MTBO_SIDEWALK_MIN_LEN_M`` × šířka.
MTBO_SIDEWALK_AREAS = True
MTBO_SIDEWALK_MIN_WIDTH_MM = 0.25
MTBO_SIDEWALK_MIN_LEN_M = 15.0


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
    dubious_lines: list[dict] = []
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
        construction: list[list[tuple[float, float]]] = []
        try:
            for pkg in kr_packages:
                construction.extend(dtm_fences.load_construction_borders(files[pkg.filename]))
        except Exception as exc:  # hranice staveb jsou doplněk, filtr funguje i bez nich
            if log:
                log(f"DTM: hranice staveb z KR nelze načíst ({type(exc).__name__}: {exc})")
        all_fences, removed = dtm_fences.filter_dubious_fences(all_fences, areas, construction=construction)
        dubious = len(removed)
        # Vynechané úseky se uloží; při kreslení se vrátí ty, které leží na hraně kreslené olivy (ZABAGED / OSM).
        dubious_lines = [{"code": f.code, "coords": [list(p) for p in f.coords]} for f in removed if f.level >= 0]
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
        _write_decks(work_dir, [files[p.filename] for p in packages if p.set_name == dtm_sources.SET_OPL], bounds, log)
    except Exception as exc:  # mosty z DTM jsou doplněk, kreslí se i z OSM
        if log:
            log(f"DTM: mosty přeskočeny ({type(exc).__name__}: {exc})")
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
                "dubious": dubious_lines,
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


def roads_path(work_dir: Path) -> Path:
    return work_dir / "dtm" / ROADS_NAME


def decks_path(work_dir: Path) -> Path:
    return work_dir / "dtm" / DECKS_NAME


def _write_decks(work_dir: Path, opl_files: list, bounds: Bounds, log=None) -> None:
    """Plochy mostů (úroveň ≥ 1) zasahující do výřezu → ``dtm/decks.json``."""
    from app.pipeline import dtm_bridges

    xmin, ymin, xmax, ymax = bounds
    decks = []
    for name in opl_files:
        for d in dtm_bridges.load_deck_polygons(name):
            xs = [p[0] for p in d["p"][0]]
            ys = [p[1] for p in d["p"][0]]
            if max(xs) < xmin or min(xs) > xmax or max(ys) < ymin or min(ys) > ymax:
                continue
            decks.append(d)
    dest = decks_path(work_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not decks:
        dest.unlink(missing_ok=True)
        return
    dest.write_text(
        json.dumps({"decks": [{"l": d["l"], "t": d["t"], "p": [[list(p) for p in ring] for ring in d["p"]]} for d in decks]}),
        encoding="utf-8",
    )
    if log:
        log(f"DTM mosty: {len(decks)} ploch (úrovně {sorted({d['l'] for d in decks})})")


def dtm_owns_decks(work_dir: Path) -> bool:
    """Má výřez mosty / více úrovní z DTM? Pak je kreslí ``build_dtm_bridge_parts`` (sprint)."""
    return decks_path(work_dir).is_file()


def deck_levels(work_dir: Path):
    """``{úroveň: {"light", "heavy", "all"}}`` (shapely) z ``decks.json``; prázdný dict bez mostů."""
    from app.pipeline import dtm_bridges

    try:
        data = json.loads(decks_path(work_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    decks = [{"l": d["l"], "t": d["t"], "p": [[(x, y) for x, y in ring] for ring in d["p"]]} for d in data.get("decks") or []]
    return dtm_bridges.level_decks(decks)


def deck_footprint_geometry(work_dir: Path):
    """Sjednocený půdorys mostů z DTM (shapely) nebo None."""
    from app.pipeline import dtm_bridges

    return dtm_bridges.footprint(deck_levels(work_dir)) if dtm_owns_decks(work_dir) else None


def _minus_decks(geom, work_dir: Path):
    """Plocha ``geom`` bez půdorysu mostů (spodní úroveň pod mostem)."""
    if geom is None or geom.is_empty:
        return geom
    foot = deck_footprint_geometry(work_dir)
    if foot is None or foot.is_empty:
        return geom
    cut = geom.difference(foot)
    return None if cut.is_empty else cut


def buildings_path(work_dir: Path) -> Path:
    return work_dir / "dtm" / BUILDINGS_NAME


def small_paved_path(work_dir: Path) -> Path:
    return work_dir / "dtm" / SMALL_PAVED_NAME


def stairs_path(work_dir: Path) -> Path:
    return work_dir / "dtm" / STAIRS_NAME


def parking_path(work_dir: Path) -> Path:
    return work_dir / "dtm" / PARKING_NAME


def green_path(work_dir: Path) -> Path:
    return work_dir / "dtm" / GREEN_NAME


def dtm_owns_stairs(work_dir: Path) -> bool:
    """Má výřez schodiště z DTM? Pak se OSM čáry schodů ořežou na ně (DTM má přednost)."""
    return stairs_path(work_dir).is_file()


def stairs_zone_geometry(work_dir: Path):
    """Schodiště z DTM rozšířená o ``STAIRS_ZONE_M`` (shapely) nebo None."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    from app.pipeline import dtm_sidewalks

    geoms = []
    for poly in _load_polygons_file(stairs_path(work_dir)):
        try:
            g = Polygon(poly[0], poly[1:])
            geoms.append(g if g.is_valid else g.buffer(0))
        except (ValueError, IndexError):
            continue
    geoms = [g for g in geoms if not g.is_empty]
    return unary_union(geoms).buffer(dtm_sidewalks.STAIRS_ZONE_M) if geoms else None


def is_surface_steps(highway: str) -> bool:
    """Schody na povrchu (ne lávka, tunel)."""
    from app.pipeline import osm_paths as op

    return (
        op.path_draw_highway(highway) == "steps"
        and not op.is_bridge_highway(highway)
        and not op.is_tunnel_highway(highway)
    )


def build_dtm_stairs_parts(
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
    """Schodiště z DTM jako čáry schodů (jedna čára na schodiště; sprint 532.7, les 532, MTBO 843).

    Čára vede středem schodiště ve směru nejbližší cesty (OSM schody, pak ostatní cesty), takže
    navazuje na cestu. OSM čáry schodů se uvnitř schodišť z DTM nekreslí (viz ``build_osm_path_parts``).
    """
    from shapely.geometry import LineString, Polygon

    from app.pipeline import dtm_sidewalks
    from app.pipeline.osm_paths import _path_object

    if mtbo_dtm_off(preset_id) or not dtm_owns_stairs(work_dir):
        return []
    from app.pipeline.osm_paths import osm_oom_code

    sprint = preset_id.startswith("sprint")
    # Symbol podle presetu (sprint 532.7, les 532, MTBO 843); les a MTBO bez drobných schodišť.
    idx = symbol_index_for_code(preset_id, scale, osm_oom_code("steps", preset_id))
    if idx is None:
        return []
    min_len = dtm_sidewalks.STAIRS_MIN_LEN_M if sprint else dtm_sidewalks.STAIRS_MIN_LEN_NONSPRINT_M
    deck_clear = None
    foot = deck_footprint_geometry(work_dir) if sprint else None
    if foot is not None and not foot.is_empty:
        deck_clear = foot.buffer(dtm_sidewalks.STAIRS_DECK_CLEAR_M)
    complex_polys = []
    steps_refs, path_refs = [], []
    for feat in _path_features(work_dir, path_source):
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") != "LineString" or len(coords) < 2:
            continue
        hw = str((feat.get("properties") or {}).get("highway") or "path")
        if is_surface_steps(hw):
            steps_refs.append(LineString(coords))
        elif not is_surface_road(hw):
            path_refs.append(LineString(coords))
    ref_lines = [steps_refs, path_refs]
    objects = []
    for poly in _load_polygons_file(stairs_path(work_dir)):
        try:
            g = Polygon(poly[0], poly[1:])
            g = g if g.is_valid else g.buffer(0)
        except (ValueError, IndexError):
            continue
        if g.is_empty:
            continue
        if dtm_sidewalks.is_complex_stairs(poly, foot):
            if g.area >= dtm_sidewalks.STAIRS_COMPLEX_MIN_AREA_M2:
                complex_polys.append(g)  # více ramen / u mostu: čáru nelze spolehlivě odvodit, plocha pro mapaře
            continue  # střepy do 4 m² se nekreslí
        axis = dtm_sidewalks.stairs_axis(poly, ref_lines)
        if axis is None or LineString(axis).length < min_len:
            continue
        # schodiště pod mostem (spodní úroveň) se pod mostem nekreslí; s odstupem od hrany mostu
        axes = dtm_sidewalks.clip_outside(axis, deck_clear, min_len) if deck_clear is not None else [axis]
        for ax in axes:
            for piece in clip_polyline(ax, clip_bounds) if clip_bounds else [ax]:
                mapped = [
                    projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg)
                    for x, y in piece
                ]
                obj = _path_object(idx, mapped)
                if obj:
                    objects.append(obj)
    parts: list[OomObjectPart] = []
    if objects:
        parts.append(OomObjectPart(name="DTM – schodiště (532.7)", objects_xml="\n".join(objects), count=len(objects)))
    if complex_polys and sprint:
        from shapely.ops import unary_union

        parts.extend(
            _geometry_part(
                unary_union(complex_polys),
                name="DTM – složitá schodiště (plocha 501.0, schody doplnit ručně)",
                code="501.0",
                preset_id=preset_id,
                scale=scale,
                ref_x=ref_x,
                ref_y=ref_y,
                grivation_deg=grivation_deg,
                clip_bounds=clip_bounds,
            )
        )
    return parts


def _write_sidewalks(work_dir: Path, opl_files: list, bounds: Bounds, log=None) -> None:
    """Plochy chodníků a silnic z DTM (úroveň 0) zasahující do výřezu → ``dtm/sidewalks.json``, ``dtm/roads.json``."""
    from app.pipeline import dtm_sidewalks

    for dest, loader, label in (
        (sidewalks_path(work_dir), dtm_sidewalks.load_sidewalk_polygons, "chodníky"),
        (roads_path(work_dir), dtm_sidewalks.load_road_polygons, "silnice"),
        (buildings_path(work_dir), dtm_sidewalks.load_building_polygons, "budovy"),
        (small_paved_path(work_dir), dtm_sidewalks.load_small_paved_polygons, "malé zpevněné stavby"),
        (stairs_path(work_dir), dtm_sidewalks.load_stairs_polygons, "schodiště"),
        (parking_path(work_dir), dtm_sidewalks.load_parking_polygons, "parkoviště"),
        (green_path(work_dir), dtm_sidewalks.load_green_polygons, "udržovaná zeleň"),
    ):
        xmin, ymin, xmax, ymax = bounds
        polys = []
        for name in opl_files:
            for poly in loader(name):
                xs = [p[0] for p in poly[0]]
                ys = [p[1] for p in poly[0]]
                if max(xs) < xmin or min(xs) > xmax or max(ys) < ymin or min(ys) > ymax:
                    continue
                polys.append(poly)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not polys:
            dest.unlink(missing_ok=True)
            continue
        dest.write_text(
            json.dumps({"polygons": [[[list(p) for p in ring] for ring in poly] for poly in polys]}),
            encoding="utf-8",
        )
        if log:
            log(f"DTM {label}: {len(polys)} ploch")


def _load_polygons_file(path: Path) -> list[list[list[tuple[float, float]]]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [[[(float(x), float(y)) for x, y in ring] for ring in poly] for poly in data.get("polygons") or []]


def _load_sidewalk_polygons(work_dir: Path) -> list[list[list[tuple[float, float]]]]:
    return _load_polygons_file(sidewalks_path(work_dir))


def _load_road_polygons(work_dir: Path) -> list[list[list[tuple[float, float]]]]:
    return _load_polygons_file(roads_path(work_dir))


def dtm_owns_buildings(work_dir: Path) -> bool:
    """Má výřez půdorysy budov z DTM? Pak budovy kreslí DTM a OSM jen tam, kde DTM budova chybí."""
    return buildings_path(work_dir).is_file()


def merge_dtm_buildings(work_dir: Path, features: list[dict]) -> list[dict]:
    """OSM prvky bez OSM budov, které mají v DTM protějšek, plus budovy z DTM jako prvky ``building``.

    OSM budova se zahodí, když aspoň ``BUILDING_TWIN_SHARE`` její plochy kryjí budovy z DTM.
    Budovy z DTM (přesné zaměření) tak mají přednost, OSM zůstává pro novější budovy.
    """
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union

    dtm_polys = _load_polygons_file(buildings_path(work_dir))
    if not dtm_polys:
        return features
    from shapely.geometry import Polygon

    dtm_geoms = []
    for poly in dtm_polys:
        try:
            g = Polygon(poly[0], poly[1:])
            g = g if g.is_valid else g.buffer(0)
        except (ValueError, IndexError):
            continue
        if not g.is_empty:
            dtm_geoms.append(g)
    # Těsně navazující části jedné budovy (společná stěna, škvíra pár cm) se kreslí jako jeden objekt.
    fused = unary_union(dtm_geoms).buffer(BUILDING_FUSE_M, join_style=2).buffer(-BUILDING_FUSE_M, join_style=2)
    dtm_geoms = [g for g in ([fused] if fused.geom_type == "Polygon" else list(getattr(fused, "geoms", []))) if g.geom_type == "Polygon" and not g.is_empty]
    dtm_union = unary_union(dtm_geoms)
    kept = []
    for feat in features:
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        if props.get("kind") == "building" and geom.get("type") in ("Polygon", "MultiPolygon"):
            try:
                g = shape(geom)
                g = g if g.is_valid else g.buffer(0)
                if g.area > 0 and g.intersection(dtm_union).area / g.area >= BUILDING_TWIN_SHARE:
                    continue
            except (ValueError, TypeError):
                pass
        kept.append(feat)
    for g in dtm_geoms:
        for part in [g] if g.geom_type == "Polygon" else list(getattr(g, "geoms", [])):
            if part.geom_type == "Polygon":
                kept.append(
                    {
                        "type": "Feature",
                        "properties": {"source": "dtm", "kind": "building", "oom_code": "521"},
                        "geometry": mapping(part),
                    }
                )
    return kept


def dtm_mask_wkbs(work_dir: Path) -> list[bytes]:
    """WKB ploch z DTM (budovy, chodníky, silnice, malé stavby), které se odečítají při výpočtu
    zbytkové zpevněné plochy (``residual_paved``), aby se nekreslila pod ně ani přes ně."""
    from shapely.geometry import Polygon

    out: list[bytes] = []
    for path in (buildings_path(work_dir), sidewalks_path(work_dir), roads_path(work_dir), small_paved_path(work_dir)):
        for poly in _load_polygons_file(path):
            try:
                g = Polygon(poly[0], poly[1:])
                g = g if g.is_valid else g.buffer(0)
            except (ValueError, IndexError):
                continue
            if not g.is_empty:
                out.append(g.wkb)
    return out


def build_dtm_small_paved_parts(
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    clip_bounds: Bounds | None = None,
) -> list[OomObjectPart]:
    """Malé stavby pro zpevnění povrchu z DTM (schody, rampy, zápraží u vchodů) jako plochy 501.0.

    Záměrně samostatná část (jedna plocha = jeden objekt), aby se dala později snadno opravit.
    """
    from shapely.geometry import Polygon, box

    from app.pipeline import dtm_sidewalks
    from app.pipeline.oom_import import _area_object_with_holes

    if not preset_id.startswith("sprint") or not small_paved_path(work_dir).is_file():
        return []
    idx = symbol_index_for_code(preset_id, scale, "501.0")
    if idx is None:
        return []
    clip = box(*clip_bounds) if clip_bounds is not None else None
    objects = []
    for poly in _load_polygons_file(small_paved_path(work_dir)):
        try:
            g = Polygon(poly[0], poly[1:])
            g = g if g.is_valid else g.buffer(0)
        except (ValueError, IndexError):
            continue
        if clip is not None:
            g = g.intersection(clip)
        for rings in dtm_sidewalks.polygon_rings(g):
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
    return [OomObjectPart(name="DTM – malé zpevněné stavby (501.0)", objects_xml="\n".join(objects), count=len(objects))]


def dtm_owns_roads(work_dir: Path) -> bool:
    """Má výřez plochy silnic z DTM? Pak silnice kreslí ``build_dtm_road_parts``."""
    return roads_path(work_dir).is_file()


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


def sidewalk_dtm_geometry(work_dir: Path, path_source: str = "mixed"):
    """Vyčištěná plocha chodníků z DTM (po přeřazení chybně zařazených ploch) nebo None."""
    from app.pipeline import dtm_sidewalks

    return dtm_sidewalks.dtm_union(classified_polygons(work_dir, path_source)[0])


def split_absorbed_areas(features: list[dict], dtm) -> tuple[list, list[dict]]:
    """OSM zpevněné plochy (hřiště, pěší zóna, parkoviště …) zasahující do plochy chodníků z DTM.

    Kreslí se stejným symbolem 501.0 jako chodníky, takže by vznikla zdvojená plocha (větší
    plocha a její část). Takové plochy se proto sloučí s chodníky: ``(shapely plochy, ostatní prvky)``.
    """
    from shapely.geometry import shape

    from app.pipeline import dtm_sidewalks
    from app.pipeline import osm_paths as op

    absorbed, rest = [], []
    for feat in features:
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        if props.get("kind") in op._PAVED_AREA_KINDS and geom.get("type") in ("Polygon", "MultiPolygon"):
            try:
                g = shape(geom)
                g = g if g.is_valid else g.buffer(0)
                if g.intersection(dtm).area >= dtm_sidewalks.ABSORB_MIN_OVERLAP_M2:
                    absorbed.append(g)
                    continue
            except (ValueError, TypeError):
                pass
        rest.append(feat)
    return absorbed, rest


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


def road_geometry(work_dir: Path, path_source: str = "mixed", cut_decks: bool = False):
    """Sjednocená plocha silnic (DTM + zbytky OSM; shapely) nebo None.

    ``cut_decks``: bez půdorysu mostů (sprint; pod mostem se kreslí most).
    """
    from app.pipeline import dtm_sidewalks

    polys = classified_polygons(work_dir, path_source)[1]  # vč. chodníků přeřazených mezi silnice
    if not polys:
        return None
    lines, widths = road_osm_lines(work_dir, path_source)
    geom = dtm_sidewalks.unify_sidewalks(polys, lines, widths=widths)
    return _minus_decks(geom, work_dir) if cut_decks else geom


def sidewalk_geometry(work_dir: Path, path_source: str = "mixed", roads=None, cut_decks: bool = False):
    """Sjednocená plocha chodníků (DTM + OSM / ZABAGED; shapely) po ořezu o silnice, nebo None.

    ``roads``: plocha silnic z ``road_geometry``; chodník se o ni ořízne a škvíry do
    ``ROAD_EDGE_GAP_M`` se zacelí (jedna hranice místo dvou čar těsně vedle sebe).
    """
    from app.pipeline import dtm_sidewalks

    polys, road_polys = classified_polygons(work_dir, path_source)
    if not polys:
        return None
    extra_areas: list = []
    dtm = dtm_sidewalks.dtm_union(polys)
    feats_path = work_dir / "osm_paths" / "features.geojson"
    if dtm is not None and feats_path.is_file():
        try:
            feats = json.loads(feats_path.read_text(encoding="utf-8")).get("features") or []
            feats = apply_dtm_priority_to_paved(work_dir, feats)
            extra_areas, _rest = split_absorbed_areas(feats, dtm)
        except (OSError, ValueError):
            pass
    osm_lines = sidewalk_osm_lines(work_dir, path_source)
    road = dtm_sidewalks.dtm_union(road_polys)
    if road is not None and not road.is_empty:
        # Přechody přes silnici se nekreslí (silnice je souvislá plocha): jen části mimo ni.
        osm_lines = [seg for ln in osm_lines for seg in dtm_sidewalks.clip_outside(ln, road)]
    geom = dtm_sidewalks.unify_sidewalks(polys, osm_lines, extra_areas=extra_areas)
    if geom is None:
        return None
    geom = dtm_sidewalks.separate_from_roads(geom, roads)
    return _minus_decks(geom, work_dir) if cut_decks else geom


def parking_geometry(work_dir: Path, roads=None, sidewalks=None, cut_decks: bool = False):
    """Plocha parkovišť z DTM po ořezu o silnice a chodníky (shapely) nebo None."""
    from app.pipeline import dtm_sidewalks

    park = dtm_sidewalks.dtm_union(_load_polygons_file(parking_path(work_dir)))
    if park is None or park.is_empty:
        return None
    for other in (roads, sidewalks):
        if other is not None and not other.is_empty:
            park = park.difference(other)
    if cut_decks:
        park = _minus_decks(park, work_dir)
    return park if park is not None and not park.is_empty else None


def apply_dtm_priority_to_paved(work_dir: Path, features: list[dict]) -> list[dict]:
    """OSM zpevněné plochy s přednostní DTM: parkoviště kryté DTM parkovištěm pryč, udržovaná zeleň
    z DTM se z OSM zpevněných ploch (parkoviště, pěší zóna …) vystřihne; dětská hřiště zůstávají z OSM (přesnější tvar)."""
    features = drop_osm_parking_covered_by_dtm(work_dir, features)
    return cut_osm_paved_by_dtm_green(work_dir, features)


# OSM zpevněné plochy, které DTM zeleň nevystřihuje (hřiště bývá v OSM přesnější než udržovaná zeleň v DTM).
DTM_GREEN_KEEP_KINDS = frozenset({"playground"})


def cut_osm_paved_by_dtm_green(work_dir: Path, features: list[dict]) -> list[dict]:
    """Udržovaná zeleň z DTM se odečte od OSM zpevněných ploch (kromě ``DTM_GREEN_KEEP_KINDS``); plochy bez zbytku (< 1 m²) se zahodí."""
    from app.pipeline import dtm_sidewalks

    green = dtm_sidewalks.dtm_union(_load_polygons_file(green_path(work_dir)))
    return cut_osm_paved_by_geometry(features, green, keep_kinds=DTM_GREEN_KEEP_KINDS)


def cut_osm_paved_by_geometry(features: list[dict], green, keep_kinds: frozenset = frozenset()) -> list[dict]:
    """Plochu ``green`` (shapely) odečte od OSM zpevněných ploch (kromě druhů ``keep_kinds``); zbytek < 1 m² se zahodí."""
    from shapely.geometry import mapping, shape

    from app.pipeline import dtm_sidewalks
    from app.pipeline import osm_paths as op

    if green is None or green.is_empty:
        return features
    out = []
    for feat in features:
        geom = feat.get("geometry") or {}
        kind = (feat.get("properties") or {}).get("kind")
        if kind in op._PAVED_AREA_KINDS and kind not in keep_kinds and geom.get("type") in (
            "Polygon",
            "MultiPolygon",
        ):
            try:
                g = shape(geom)
                g = g if g.is_valid else g.buffer(0)
                if g.intersects(green):
                    rest = g.difference(green)
                    if rest.is_empty or rest.area < dtm_sidewalks.MIN_AREA_M2:
                        continue
                    polys = [rest] if rest.geom_type == "Polygon" else [x for x in getattr(rest, "geoms", []) if x.geom_type == "Polygon" and x.area >= dtm_sidewalks.MIN_AREA_M2]
                    for part in polys:
                        out.append({**feat, "geometry": mapping(part)})
                    continue
            except (ValueError, TypeError):
                pass
        out.append(feat)
    return out


def drop_osm_parking_covered_by_dtm(work_dir: Path, features: list[dict]) -> list[dict]:
    """OSM parkoviště, které se překrývá s parkovištěm z DTM (aspoň 1 m²), se zahodí (DTM má přednost)."""
    from shapely.geometry import shape

    from app.pipeline import dtm_sidewalks

    park = dtm_sidewalks.dtm_union(_load_polygons_file(parking_path(work_dir)))
    if park is None or park.is_empty:
        return features
    kept = []
    for feat in features:
        geom = feat.get("geometry") or {}
        if (feat.get("properties") or {}).get("kind") == "parking" and geom.get("type") in ("Polygon", "MultiPolygon"):
            try:
                g = shape(geom)
                g = g if g.is_valid else g.buffer(0)
                if g.intersection(park).area >= dtm_sidewalks.ABSORB_MIN_OVERLAP_M2:
                    continue
            except (ValueError, TypeError):
                pass
        kept.append(feat)
    return kept


# Plochy 501.0 / 501.20 mají v symbolu obrys. Velký polygon (síť chodníků přes celé sídliště) by byl
# jeden objekt s tisíci vrcholů, který OCAD / Mapper brzdí. Takový polygon se vyplní bezobrysovým
# symbolem (501.1 / 501.21), rozřeže se (řezy nejsou vidět) a obrys se nakreslí zvlášť čárou 501.100.
AREA_SPLIT_VERTICES = 1000
OUTLINE_CHUNK_VERTICES = 800
FILL_ONLY_CODE = {"501.0": "501.1", ROAD_CODE: "501.21"}
EDGE_CODE = "501.100"
# Šířka pásu kolem hrany mostu (m), v němž se obrys spodní plochy nekreslí.
EDGE_EXCLUDE_BUFFER_M = 0.15


def _chunk_line(coords: list[tuple[float, float]], size: int) -> list[list[tuple[float, float]]]:
    """Dlouhou čáru rozdělí na kusy do ``size`` vrcholů (sousední kusy sdílí krajní vrchol)."""
    if len(coords) <= size:
        return [coords]
    out, i = [], 0
    while i < len(coords) - 1:
        out.append(coords[i : i + size])
        i += size - 1
    return out


def _geometry_part(
    geom,
    *,
    name: str,
    code: str,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    clip_bounds: Bounds | None,
    outline_separate: bool = False,
    edge_exclude=None,
) -> list[OomObjectPart]:
    """Plocha (shapely) jako části .omap se symbolem ``code``; prázdný seznam bez geometrie / symbolu.

    ``outline_separate``: vždy bezobrysová výplň + zvláštní obrys 501.100 (i u malých polygonů).
    ``edge_exclude``: plocha (půdorys mostu, shapely); obrys podél její hrany se nekreslí a polygony, které se jí
    dotýkají, se kreslí s obrysem zvlášť (spodní plocha pod mostem končí u trojúhelníků, ne obrysovou čárou).

    Polygony nad ``AREA_SPLIT_VERTICES`` vrcholů: bezobrysová výplň rozřezaná na díly + zvláštní část
    s obrysem (čára 501.100) po skutečné hranici (ne podél ořezu na výřez ani podél řezů).
    """
    from shapely.geometry import LineString, box
    from shapely.ops import unary_union

    from app.pipeline import dtm_sidewalks
    from app.pipeline.area_split import split_area
    from app.pipeline.oom_import import _area_object_with_holes

    if geom is None or geom.is_empty:
        return []
    clip = box(*clip_bounds) if clip_bounds is not None else None
    if clip is not None:
        geom = geom.intersection(clip)
    idx = symbol_index_for_code(preset_id, scale, code)
    if idx is None:
        return []
    fill_code = FILL_ONLY_CODE.get(code)
    fill_idx = symbol_index_for_code(preset_id, scale, fill_code) if fill_code else None
    edge_idx = symbol_index_for_code(preset_id, scale, EDGE_CODE)

    def to_map(ring):
        return [
            projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg)
            for x, y in ring
        ]

    objects: list[str] = []
    edges: list[str] = []
    exclude_zone = None
    if edge_exclude is not None and not edge_exclude.is_empty:
        exclude_zone = edge_exclude.boundary.buffer(EDGE_EXCLUDE_BUFFER_M)
    for rings in dtm_sidewalks.polygon_rings(geom):
        touches_deck = False
        if exclude_zone is not None:
            from shapely.geometry import Polygon as _Poly

            touches_deck = _Poly(rings[0], rings[1:]).boundary.intersects(exclude_zone)
        if (
            (outline_separate or touches_deck or sum(len(r) for r in rings) > AREA_SPLIT_VERTICES)
            and fill_idx is not None
            and edge_idx is not None
        ):
            for ring, holes in split_area(rings[0], rings[1:], AREA_SPLIT_VERTICES):
                obj = _area_object_with_holes(fill_idx, [to_map(ring), *[to_map(h) for h in holes]])
                if obj:
                    objects.append(obj)
            outline = unary_union([LineString(r) for r in rings])
            if clip is not None:
                outline = outline.difference(clip.exterior.buffer(0.02))
            if exclude_zone is not None:
                outline = outline.difference(exclude_zone)  # podél hrany mostu obrys není (trojúhelníky)
            lines = [outline] if outline.geom_type == "LineString" else [g for g in getattr(outline, "geoms", []) if g.geom_type == "LineString"]
            for ln in lines:
                for piece in _chunk_line([(float(x), float(y)) for x, y in ln.coords], OUTLINE_CHUNK_VERTICES):
                    obj = _path_object_mapped(edge_idx, to_map(piece))
                    if obj:
                        edges.append(obj)
            continue
        obj = _area_object_with_holes(idx, [to_map(r) for r in rings])
        if obj:
            objects.append(obj)
    parts = []
    if objects:
        parts.append(OomObjectPart(name=name, objects_xml="\n".join(objects), count=len(objects)))
    if edges:
        parts.append(OomObjectPart(name=f"{name} – obrys (501.100)", objects_xml="\n".join(edges), count=len(edges)))
    return parts


def _outline_part(
    geom,
    *,
    name: str,
    code: str,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    clip_bounds: Bounds | None,
) -> list[OomObjectPart]:
    """Obrys plochy (shapely) čárou ``code`` (jen obvod a díry, ne podél ořezu na výřez); po kusech do 800 vrcholů."""
    from shapely.geometry import LineString, box
    from shapely.ops import unary_union

    from app.pipeline import dtm_sidewalks

    if geom is None or geom.is_empty:
        return []
    idx = symbol_index_for_code(preset_id, scale, code)
    if idx is None:
        return []
    clip = box(*clip_bounds) if clip_bounds is not None else None
    objects: list[str] = []
    for rings in dtm_sidewalks.polygon_rings(geom):
        outline = unary_union([LineString(r) for r in rings])
        if clip is not None:
            outline = outline.intersection(clip).difference(clip.exterior.buffer(0.02))
        lines = [outline] if outline.geom_type == "LineString" else [g for g in getattr(outline, "geoms", []) if g.geom_type == "LineString"]
        for ln in lines:
            for piece in _chunk_line([(float(x), float(y)) for x, y in ln.coords], OUTLINE_CHUNK_VERTICES):
                mapped = [
                    projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg)
                    for x, y in piece
                ]
                obj = _path_object_mapped(idx, mapped)
                if obj:
                    objects.append(obj)
    return [OomObjectPart(name=name, objects_xml="\n".join(objects), count=len(objects))] if objects else []


def _path_object_mapped(symbol_index: int, mapped) -> str:
    from app.pipeline.osm_paths import _path_object

    return _path_object(symbol_index, mapped)


def _load_bike_lines(work_dir: Path) -> list[list[tuple[float, float]]]:
    """Čáry OSM chodníků / pěšin se sdíleným provozem (``osm_paths/bike_paths.geojson``)."""
    path = work_dir / "osm_paths" / "bike_paths.geojson"
    try:
        feats = json.loads(path.read_text(encoding="utf-8")).get("features") or []
    except (OSError, ValueError):
        return []
    out = []
    for f in feats:
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") == "LineString" and len(coords) >= 2:
            out.append([(float(x), float(y)) for x, y in coords])
    return out


def mtbo_corridor_geometry(work_dir: Path, scale: int, path_source: str = "mixed"):
    """Koridor silnice pro MTBO (silnice + přilehlé chodníky, jen dost široké části; shapely) nebo None."""
    from app.pipeline import dtm_sidewalks

    if not MTBO_USE_DTM or not MTBO_ROAD_CORRIDOR or not (dtm_owns_roads(work_dir) or dtm_owns_sidewalks(work_dir)):
        return None
    roads = road_geometry(work_dir, path_source)
    if roads is None:
        return None
    sidewalks = sidewalk_geometry(work_dir, path_source, roads=None)
    return dtm_sidewalks.road_corridor(
        roads, sidewalks, _load_bike_lines(work_dir), scale=scale, parking=corridor_parking_geometry(work_dir)
    )


def mtbo_sidewalk_geometry(work_dir: Path, scale: int, path_source: str = "mixed"):
    """Chodníky a zpevněné cesty z DTM mimo silnice pro MTBO (shapely) nebo None.

    Jako ve sprintu tvar bere DTM (+ zbytky OSM), ale jen části široké aspoň ``MTBO_SIDEWALK_MIN_WIDTH_MM`` mm mapy
    a mimo koridor silnice. Chodník do ``CORRIDOR_NEAR_M`` m od silnice se nekreslí (kromě sdíleného provozu –
    cyklostezky); užší cesty zůstávají čárou 834 / 503.
    """
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    from app.pipeline import dtm_sidewalks

    if not MTBO_USE_DTM or not MTBO_SIDEWALK_AREAS or not dtm_owns_sidewalks(work_dir):
        return None
    sidewalks = sidewalk_geometry(work_dir, path_source, roads=None)
    if sidewalks is None or sidewalks.is_empty:
        return None
    corridor = mtbo_corridor_geometry(work_dir, scale, path_source)
    if corridor is not None and not corridor.is_empty:
        sidewalks = sidewalks.difference(corridor)
    roads = road_geometry(work_dir, path_source)
    if roads is not None and not roads.is_empty:
        near = roads.buffer(dtm_sidewalks.CORRIDOR_NEAR_M)
        bike = [LineString(c) for c in _load_bike_lines(work_dir) if len(c) >= 2]
        if bike:
            near = near.difference(unary_union(bike).buffer(dtm_sidewalks.CORRIDOR_BIKE_PROTECT_M))
        sidewalks = sidewalks.difference(near)
    width = MTBO_SIDEWALK_MIN_WIDTH_MM * scale / 1000.0
    return dtm_sidewalks.wide_parts(sidewalks, width, min_area_m2=width * MTBO_SIDEWALK_MIN_LEN_M)


def corridor_parking_geometry(work_dir: Path):
    """Parkoviště pro koridor: parkoviště z DTM + OSM parkoviště, které z DTM nemá protějšek (shapely) nebo None."""
    from shapely.geometry import shape
    from shapely.ops import unary_union

    from app.pipeline import dtm_sidewalks

    geoms = []
    dtm_park = dtm_sidewalks.dtm_union(_load_polygons_file(parking_path(work_dir)))
    if dtm_park is not None and not dtm_park.is_empty:
        geoms.append(dtm_park)
    feats_path = work_dir / "osm_paths" / "features.geojson"
    if feats_path.is_file():
        try:
            feats = json.loads(feats_path.read_text(encoding="utf-8")).get("features") or []
        except (OSError, ValueError):
            feats = []
        feats = drop_osm_parking_covered_by_dtm(work_dir, feats)
        for feat in feats:
            geom = feat.get("geometry") or {}
            if (feat.get("properties") or {}).get("kind") == "parking" and geom.get("type") in ("Polygon", "MultiPolygon"):
                try:
                    g = shape(geom)
                    geoms.append(g if g.is_valid else g.buffer(0))
                except (ValueError, TypeError):
                    continue
    return unary_union(geoms) if geoms else None


def build_mtbo_corridor_parts(
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
    """MTBO: silnice s přilehlými chodníky jednou plochou (výplň 501.1) + černý obrubník (501.100).

    Jen tam, kde je koridor dost široký (viz ``dtm_sidewalks.road_corridor``); užší silnice kreslí
    čára 502.x z OSM cest. Čáry 502.x se uvnitř koridoru nekreslí (``build_osm_path_parts``).
    """
    if not preset_id.startswith("mtbo") or mtbo_dtm_off(preset_id):
        return []
    corridor = mtbo_corridor_geometry(work_dir, scale, path_source)
    return _geometry_part(
        corridor,
        name="MTBO – silnice s chodníky (koridor)",
        code="501.0",
        preset_id=preset_id,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        grivation_deg=grivation_deg,
        clip_bounds=clip_bounds,
        outline_separate=True,
    )


def dtm_sidewalks_union(polys):
    from app.pipeline import dtm_sidewalks

    return dtm_sidewalks.dtm_union(polys) if polys else None


def _osm_water_geometry(work_dir: Path):
    """Vodní plochy z OSM (``water_body``) jako shapely geometrie nebo None (voda pod mostem)."""
    from app.pipeline import osm_paths as op

    try:
        return op.osm_water_area_geometry(work_dir)
    except Exception:
        return None


def bridge_entrance_edge_lines(work_dir: Path, path_source: str = "mixed") -> list[list[tuple[float, float]]]:
    """Hrany 512.100 pod mosty z DTM (S-JTSK): přes chodník pod mostem, jinak přes silnici (viz ``dtm_bridges``)."""
    from app.pipeline import dtm_bridges, dtm_sidewalks

    levels = deck_levels(work_dir)
    foot = dtm_bridges.footprint(levels)
    if foot is None:
        return []
    sw_polys, road_polys = classified_polygons(work_dir, path_source)
    return dtm_bridges.entrance_edges(
        foot, dtm_sidewalks.dtm_union(sw_polys), dtm_sidewalks.dtm_union(road_polys)
    )


def build_dtm_bridge_parts(
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
    """Mosty z DTM (jen sprint): mostovka po úrovních barvou povrchu + hrany 512.100 pod mostem.

    Chodníkové mostovky 501.0, silniční 501.20 (úrovně od nejnižší; vyšší úroveň vystřihuje nižší).
    Hrany 512.100 jsou orientované tak, že vnitřek mostu leží vlevo od směru kreslení.
    """
    from app.pipeline.osm_paths import _path_object

    if not preset_id.startswith("sprint") or not dtm_owns_decks(work_dir):
        return []
    from app.pipeline import dtm_bridges

    kw = dict(
        preset_id=preset_id,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        grivation_deg=grivation_deg,
        clip_bounds=clip_bounds,
    )
    levels = deck_levels(work_dir)
    parts: list[OomObjectPart] = []
    # Mosty nad spodní zpevněnou plochou / vodou kreslí celé 512.311 / 512.302 (a obrys 501.100), ne mostovka 501.0 / 501.20.
    foot0 = dtm_bridges.footprint(levels)
    sw_polys0, road_polys0 = classified_polygons(work_dir, path_source)
    paved_parts0 = [
        g
        for g in (
            dtm_sidewalks_union(sw_polys0),
            dtm_sidewalks_union(road_polys0),
            dtm_sidewalks_union(_load_polygons_file(parking_path(work_dir))),
        )
        if g is not None and not g.is_empty
    ]
    from shapely.ops import unary_union as _uu0

    lower0 = dtm_bridges.lower_level_areas(foot0, _uu0(paved_parts0) if paved_parts0 else None, _osm_water_geometry(work_dir))
    covered = _uu0([g for g in (lower0.get("paved"), lower0.get("water")) if g is not None]) if (lower0.get("paved") is not None or lower0.get("water") is not None) else None
    for level in sorted(levels):
        for key, code, label in (("light", "501.0", "chodník 501.0"), ("heavy", ROAD_CODE, "silnice 501.20")):
            geom = levels[level][key]
            if geom is not None and covered is not None:
                geom = geom.difference(covered)  # tuto část mostu kreslí 512.3xx (níže)
            if geom is None or geom.is_empty:
                continue
            polys = [geom] if geom.geom_type == "Polygon" else [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
            polys = [g for g in polys if g.area >= dtm_bridges.MIN_DECK_AREA_M2]
            if not polys:
                continue
            from shapely.ops import unary_union

            parts.extend(
                _geometry_part(
                    unary_union(polys),
                    name=f"Mosty (DTM) úr. +{level} – {label}",
                    code=code,
                    **kw,
                )
            )
    # Křížení úrovní: spodní zpevněná plocha / voda pod mostem symbolem 512.311 / 512.302 (bílý základ + barva spodní plochy).
    if covered is not None:
        outline_lines = [g for lv in levels.values() if lv["all"] is not None for g in [lv["all"].intersection(covered)] if not g.is_empty]
        if outline_lines:
            parts.extend(
                _outline_part(
                    _uu0(outline_lines),
                    name="Mosty (DTM) – obrys mostu (501.100)",
                    code=EDGE_CODE,
                    **kw,
                )
            )
    from shapely.ops import unary_union as _uu

    foot = dtm_bridges.footprint(levels)
    sw_polys, road_polys = classified_polygons(work_dir, path_source)
    paved_parts = [g for g in (dtm_sidewalks_union(sw_polys), dtm_sidewalks_union(road_polys), dtm_sidewalks_union(_load_polygons_file(parking_path(work_dir)))) if g is not None and not g.is_empty]
    lower = dtm_bridges.lower_level_areas(foot, _uu(paved_parts) if paved_parts else None, _osm_water_geometry(work_dir))
    for key, code, label in (("paved", dtm_bridges.LOWER_PAVED_CODE, "zpevněná plocha 512.311"), ("water", dtm_bridges.LOWER_WATER_CODE, "voda 512.302")):
        if lower.get(key) is not None:
            parts.extend(_geometry_part(lower[key], name=f"Mosty (DTM) – nižší úroveň, {label}", code=code, **kw))
    edge_idx = symbol_index_for_code(preset_id, scale, "512.100")
    if edge_idx is not None:
        objects: list[str] = []
        for line in bridge_entrance_edge_lines(work_dir, path_source):
            for piece in clip_polyline(line, clip_bounds) if clip_bounds else [line]:
                mapped = [
                    projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg)
                    for x, y in piece
                ]
                obj = _path_object(edge_idx, mapped)
                if obj:
                    objects.append(obj)
        if objects:
            parts.append(OomObjectPart(name="Hrany pod mosty (DTM, 512.100)", objects_xml="\n".join(objects), count=len(objects)))
    return parts


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
    """Chodníky (sprint, MTBO) jako jedna plocha 501.0: DTM tvar + doplňky z OSM / ZABAGED, ořezaná o silnice.

    MTBO: jen široké části mimo silnice a koridor (``mtbo_sidewalk_geometry``), obrys zvlášť čárou 501.100.
    """
    if mtbo_dtm_off(preset_id):
        return []
    if preset_id.startswith("mtbo"):
        return _geometry_part(
            mtbo_sidewalk_geometry(work_dir, scale, path_source),
            name="MTBO – chodníky a zpevněné cesty z DTM",
            code="501.0",
            preset_id=preset_id,
            scale=scale,
            ref_x=ref_x,
            ref_y=ref_y,
            grivation_deg=grivation_deg,
            clip_bounds=clip_bounds,
            outline_separate=True,
        )
    if not preset_id.startswith("sprint") or not dtm_owns_sidewalks(work_dir):
        return []
    geom = sidewalk_geometry(
        work_dir,
        path_source,
        roads=road_geometry(work_dir, path_source),
        cut_decks=True,
    )
    return _geometry_part(
        geom,
        edge_exclude=deck_footprint_geometry(work_dir),
        name="Chodníky (DTM + OSM)",
        code="501.0",
        preset_id=preset_id,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        grivation_deg=grivation_deg,
        clip_bounds=clip_bounds,
    )


def build_dtm_parking_parts(
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
    """Parkoviště a odstavné plochy z DTM jako samostatná část.

    Sprint: 501.0, ořezaná o silnice a chodníky. MTBO: jen velká parkoviště (od ``MTBO_PARKING_MIN_M2``)
    zpevněnou plochou 529; silnice jsou tam čáry, takže se neořezává. Les: nic.
    """
    if mtbo_dtm_off(preset_id):
        return []
    if preset_id.startswith("mtbo") and parking_path(work_dir).is_file():
        geom = parking_geometry(work_dir)
        if geom is None:
            return []
        corridor = mtbo_corridor_geometry(work_dir, scale, path_source)
        if corridor is not None and not corridor.is_empty:
            geom = geom.difference(corridor)  # část v koridoru kreslí koridor
            if geom.is_empty:
                return []
        from shapely.ops import unary_union

        polys = [geom] if geom.geom_type == "Polygon" else [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
        big = [g for g in polys if g.area >= MTBO_PARKING_MIN_M2]
        return _geometry_part(
            unary_union(big) if big else None,
            name="DTM – velká parkoviště (529)",
            code="529",
            preset_id=preset_id,
            scale=scale,
            ref_x=ref_x,
            ref_y=ref_y,
            grivation_deg=grivation_deg,
            clip_bounds=clip_bounds,
        )
    if not preset_id.startswith("sprint") or not parking_path(work_dir).is_file():
        return []
    roads = road_geometry(work_dir, path_source)
    sidewalks = sidewalk_geometry(work_dir, path_source, roads=roads) if dtm_owns_sidewalks(work_dir) else None
    return _geometry_part(
        parking_geometry(work_dir, roads, sidewalks, cut_decks=True),
        edge_exclude=deck_footprint_geometry(work_dir),
        name="DTM – parkoviště (501.0)",
        code="501.0",
        preset_id=preset_id,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        grivation_deg=grivation_deg,
        clip_bounds=clip_bounds,
    )


def classified_polygons(
    work_dir: Path, path_source: str
) -> tuple[list[list[list[tuple[float, float]]]], list[list[list[tuple[float, float]]]]]:
    """``(chodníky, silnice)`` z DTM po přeřazení chybně zařazených ploch.

    ``ChodnikPlocha`` s OSM silnicemi uvnitř (viz ``dtm_sidewalks.reclassify_sidewalks_as_roads``)
    se bere jako silnice.
    """
    import logging

    from app.pipeline import dtm_sidewalks

    sidewalks = _load_sidewalk_polygons(work_dir)
    roads = _load_road_polygons(work_dir)
    if sidewalks:
        road_lines, _widths = road_osm_lines(work_dir, path_source)
        sidewalks, moved = dtm_sidewalks.reclassify_sidewalks_as_roads(sidewalks, road_lines)
        if moved:
            roads = roads + moved
            logging.getLogger(__name__).info(
                "DTM: %s ploch chodníků s OSM silnicí uvnitř přeřazeno mezi silnice", len(moved)
            )
    return sidewalks, roads


def is_surface_road(highway: str) -> bool:
    """Silnice na povrchu (``road_1..4``; ne most, tunel)."""
    from app.pipeline import osm_paths as op

    return (
        op.path_draw_highway(highway).startswith("road_")
        and not op.is_bridge_highway(highway)
        and not op.is_tunnel_highway(highway)
    )


def road_osm_lines(
    work_dir: Path, path_source: str
) -> tuple[list[list[tuple[float, float]]], list[float]]:
    """Osy silnic z OSM (podle zdroje cest) a šířky jejich kresby (m)."""
    from app.pipeline import osm_paths as op

    lines, widths = [], []
    for feat in _path_features(work_dir, path_source):
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") != "LineString" or len(coords) < 2:
            continue
        hw = str((feat.get("properties") or {}).get("highway") or "path")
        if is_surface_road(hw):
            lines.append([(float(x), float(y)) for x, y in coords])
            widths.append(op.drawn_width_m(hw))
    return lines, widths


def build_dtm_road_parts(
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
    """Silnice (jen sprint) jako jedna plocha 501.20: DTM tvar + doplňky z OSM."""
    if not preset_id.startswith("sprint") or not (dtm_owns_roads(work_dir) or dtm_owns_sidewalks(work_dir)):
        return []
    return _geometry_part(
        road_geometry(work_dir, path_source, cut_decks=True),
        edge_exclude=deck_footprint_geometry(work_dir),
        name="Silnice (DTM + OSM)",
        code=ROAD_CODE,
        preset_id=preset_id,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        grivation_deg=grivation_deg,
        clip_bounds=clip_bounds,
    )


def dtm_cover_geometry(work_dir: Path, buffer_m: float | None = None):
    """Plochy chodníků a silnic z DTM rozšířené o ``buffer_m`` (výchozí ``OSM_COVER_M``; shapely) nebo None.

    Čáry cest se o ně ořežou, aby se uvnitř ploch nekreslily podruhé; s ``buffer_m=0`` končí přesně na hraně.
    """
    from shapely.ops import unary_union

    from app.pipeline import dtm_sidewalks

    parts = []
    for polys in (_load_sidewalk_polygons(work_dir), _load_road_polygons(work_dir)):
        dtm = dtm_sidewalks.dtm_union(polys)
        if dtm is not None and not dtm.is_empty:
            parts.append(dtm)
    if not parts:
        return None
    grow = dtm_sidewalks.OSM_COVER_M if buffer_m is None else buffer_m
    union = unary_union(parts)
    return union.buffer(grow) if grow else union


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


def _load_dubious_fences(work_dir: Path) -> list[tuple[str, list[tuple[float, float]]]]:
    """Podezřelé ploty vynechané při přípravě (``fences.json`` → ``dubious``)."""
    try:
        data = json.loads(fences_path(work_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for f in data.get("dubious") or []:
        coords = [(float(x), float(y)) for x, y in f.get("coords") or []]
        if len(coords) >= 2:
            out.append((str(f.get("code") or dtm_fences.CODE_FENCE), coords))
    return out


# Podezřelý plot (neznámý původ, hranice jiného objektu) se vrátí, když z aspoň ``FENCE_OLIVE_EDGE_SHARE`` délky leží do
# ``FENCE_OLIVE_EDGE_TOL_M`` m od okraje kreslené olivy (zahrada, zastavěná plocha …) – je to hranice soukromého pozemku.
FENCE_OLIVE_EDGE_TOL_M = 0.5
FENCE_OLIVE_EDGE_SHARE = 0.8


def fences_on_olive_edge(
    lines: list[tuple[str, list[tuple[float, float]]]], olive, *, tol_m: float = FENCE_OLIVE_EDGE_TOL_M, share: float = FENCE_OLIVE_EDGE_SHARE
) -> list[tuple[str, list[tuple[float, float]]]]:
    """Ploty ležící na okraji olivy ``olive`` (shapely; prázdná = žádné)."""
    from shapely.geometry import LineString

    if olive is None or olive.is_empty:
        return []
    zone = olive.boundary.buffer(tol_m)
    out = []
    for code, ln in lines:
        g = LineString(ln)
        if g.length > 0 and g.intersection(zone).length >= share * g.length:
            out.append((code, ln))
    return out


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


# Plot vedoucí po obrysu budovy (do tolika m) se nekreslí – budova má přednost; zbytky kratší než ``FENCE_BUILDING_MIN_PIECE_M`` pryč.
FENCE_BUILDING_TOL_M = 0.5
FENCE_BUILDING_MIN_PIECE_M = 1.5


def _buildings_zone(work_dir: Path, tol_m: float = FENCE_BUILDING_TOL_M):
    """Budovy tak, jak se kreslí (DTM budovy + OSM bez protějšku), rozšířené o ``tol_m`` m (shapely) nebo None."""
    from shapely.geometry import shape
    from shapely.ops import unary_union

    feats_path = work_dir / "osm_paths" / "features.geojson"
    try:
        feats = json.loads(feats_path.read_text(encoding="utf-8")).get("features") or []
    except (OSError, ValueError):
        feats = []
    if dtm_owns_buildings(work_dir):
        feats = merge_dtm_buildings(work_dir, feats)
    polys = []
    for feat in feats:
        geom = feat.get("geometry") or {}
        if (feat.get("properties") or {}).get("kind") != "building" or geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        try:
            g = shape(geom)
            polys.append(g if g.is_valid else g.buffer(0))
        except (ValueError, TypeError):
            continue
    return unary_union(polys).buffer(tol_m) if polys else None


def _olive_geometry(work_dir: Path, fill_rings: list[list[tuple[float, float]]]):
    """Kreslené olivy (shapely, i prázdná): zahrady z OSM (``kind == "garden"``), oplocení vyplněná olivou z DTM
    a olivové vrstvy ZABAGED (``ZABAGED_OLIVE_LAYERS``)."""
    from shapely.geometry import Polygon, shape
    from shapely.ops import unary_union

    polys = []
    try:
        feats = json.loads((work_dir / "osm_paths" / "features.geojson").read_text(encoding="utf-8")).get("features") or []
    except (OSError, ValueError):
        feats = []
    for feat in feats:
        geom = feat.get("geometry") or {}
        if (feat.get("properties") or {}).get("kind") != "garden" or geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        try:
            g = shape(geom)
            polys.append(g if g.is_valid else g.buffer(0))
        except (ValueError, TypeError):
            continue
    for ring in fill_rings:
        if len(ring) >= 4:
            try:
                g = Polygon(ring)
                polys.append(g if g.is_valid else g.buffer(0))
            except ValueError:
                continue
    polys.extend(_zabaged_olive_polygons(work_dir))
    return unary_union(polys) if polys else Polygon()


# Vrstvy ZABAGED, které se v sadě symbolů kreslí jako olivová plocha (zastavěná plocha, zahrady, hřbitov; viz ``configs/zabaged.txt``).
ZABAGED_OLIVE_LAYERS = ("ArealUceloveZastavby", "OvocnySadZahrada", "Hrbitov")


def _zabaged_olive_polygons(work_dir: Path) -> list:
    """Polygony olivových vrstev ZABAGED z ``zabaged_clean.zip`` (shapely); bez archivu nebo pyshp prázdný seznam."""
    import io
    import zipfile

    from shapely.geometry import shape

    archive = work_dir / "zabaged_clean.zip"
    if not archive.is_file():
        return []
    try:
        import shapefile

        out = []
        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
            for stem in ZABAGED_OLIVE_LAYERS:
                if not {f"{stem}.shp", f"{stem}.shx", f"{stem}.dbf"} <= names:
                    continue
                reader = shapefile.Reader(
                    shp=io.BytesIO(zf.read(f"{stem}.shp")),
                    shx=io.BytesIO(zf.read(f"{stem}.shx")),
                    dbf=io.BytesIO(zf.read(f"{stem}.dbf")),
                )
                for sh in reader.iterShapes():
                    if sh.shapeType == 0:
                        continue
                    try:
                        g = shape(sh.__geo_interface__)
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if g.geom_type in ("Polygon", "MultiPolygon"):
                        out.append(g if g.is_valid else g.buffer(0))
        return out
    except Exception:  # olivy ze ZABAGED jsou doplněk podmínky, bez nich se spojuje podle ostatních zdrojů
        return []


def cut_fences_by_buildings(
    lines: list[tuple[str, list[tuple[float, float]]]], zone, min_piece_m: float = FENCE_BUILDING_MIN_PIECE_M
) -> list[tuple[str, list[tuple[float, float]]]]:
    """Části plotů ležící na obrysu budovy / uvnitř ní (``zone``) se odříznou; ploty mimo ni zůstanou beze změny."""
    from shapely.geometry import LineString

    if zone is None or zone.is_empty:
        return lines
    out = []
    for code, ln in lines:
        if len(ln) < 2:
            continue
        line = LineString(ln)
        if not line.intersects(zone):
            out.append((code, ln))
            continue
        rest = line.difference(zone)
        pieces = [rest] if rest.geom_type == "LineString" else [g for g in getattr(rest, "geoms", []) if g.geom_type == "LineString"]
        out.extend((code, [(float(x), float(y)) for x, y in g.coords]) for g in pieces if g.length >= min_piece_m)
    return out


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
    """Objekty plotů z DTM i OSM: jedna síť, společné zpracování.

    OSM ploty (po ořezu o DTM) se spojí a vnitřní se zahodí. Sprint navíc otočí čárky k olivě,
    vyplní oplocení olivou a odsune ploty od kreslených cest – stejně jako ploty z DTM (ty jsou
    spojené, filtrované a orientované už při přípravě). Les a MTBO kreslí jen spojené ploty
    (516 / 522, živý plot 416) delší než ``NONSPRINT_FENCE_MIN_LEN_M`` bez orientace; oplocení se
    olivou vyplňuje ve sprintu a v lese (MTBO ne).
    """
    from app.pipeline.oom_import import _area_object_with_holes
    from app.pipeline.osm_paths import _path_object, feature_oom_code

    if mtbo_dtm_off(preset_id) or not dtm_owns_fences(work_dir):
        return []
    sprint = preset_id.startswith("sprint")
    fill_olive = not preset_id.startswith("mtbo")  # výplň oplocení olivou (520): sprint a les, ne MTBO
    dtm_lines, fill_rings = _load_fence_data(work_dir)
    osm_lines = _osm_fence_lines(work_dir)
    areas = _load_opl_areas(work_dir)
    if not sprint:
        if not fill_olive:
            fill_rings = []
        # Symboly podle presetu: plot 516 (les) / 522 (MTBO), živý plot 416.
        code_map = {
            dtm_fences.CODE_FENCE: feature_oom_code("fence", preset_id),
            dtm_fences.CODE_HEDGE: feature_oom_code("hedge", preset_id),
        }
        dtm_lines = [(code_map.get(c, c), ln) for c, ln in dtm_lines]
        osm_lines = [(code_map.get(c, c), ln) for c, ln in osm_lines]
    lines = list(dtm_lines)
    if osm_lines:
        try:
            tagged = [(c, ln, False) for c, ln in dtm_lines] + [(c, ln, True) for c, ln in osm_lines]
            dropped = dtm_fences.fences_inside_enclosures([(c, ln) for c, ln, _o in tagged])
            tagged = [item for i, item in enumerate(tagged) if i not in dropped]
            keep_dtm = [(c, ln) for c, ln, is_osm in tagged if not is_osm]
            keep_osm = [(c, ln) for c, ln, is_osm in tagged if is_osm]
            if sprint and keep_osm and (areas["olive"] or areas["public"]):
                keep_osm = dtm_fences.orient_fences(keep_osm, areas)
            lines = keep_dtm + keep_osm
        except Exception:  # sjednocení je doplněk, ploty se kreslí i bez něj
            lines = dtm_lines + osm_lines
    if not sprint:
        from shapely.geometry import LineString

        lines = [(c, ln) for c, ln in lines if len(ln) >= 2 and LineString(ln).length >= NONSPRINT_FENCE_MIN_LEN_M]
    if not lines and not _load_dubious_fences(work_dir):
        return []
    if fill_olive and osm_lines and (areas["olive"] or areas["building"]):
        try:
            fill_rings = dtm_fences.private_enclosure_rings(lines, areas)
        except Exception:
            pass
    try:
        olive_geom = _olive_geometry(work_dir, fill_rings if fill_olive else [])
        # Podezřelé ploty ležící na hraně kreslené olivy se vracejí (hranice soukromého pozemku).
        dubious_back = fences_on_olive_edge(_load_dubious_fences(work_dir), olive_geom)
        if not sprint:
            from shapely.geometry import LineString as _L2

            dubious_back = [
                (code_map.get(c, c), ln)
                for c, ln in dubious_back
                if _L2(ln).length >= NONSPRINT_FENCE_MIN_LEN_M
            ]
        lines = lines + dubious_back
        # Plot na obrysu budovy se nekreslí (budova má přednost, kreslí se z DTM / OSM).
        buildings_zone = _buildings_zone(work_dir)
        lines = cut_fences_by_buildings(lines, buildings_zone)
        # Data DTM bývají neúplná: mezery mezi konci plotů, které na sebe míří, se propojí (ne přes budovu ani cestu).
        from shapely.geometry import LineString as _LS
        from shapely.ops import unary_union as _uu

        blockers = [buildings_zone] if buildings_zone is not None else []
        blockers.extend(_LS(c).buffer(0.2) for c, _need in path_clearances(work_dir, path_source) if len(c) >= 2)
        connectors = dtm_fences.fence_gap_connectors(
            lines, forbidden=_uu(blockers) if blockers else None, near=olive_geom
        )
        if connectors or dubious_back:
            # Spojky se slijí s ploty do jedné linie (po kódu symbolu); ve sprintu se pak celá linie otočí tak, aby
            # čárky plotu mířily do olivy (OPL zahrady + kreslená oliva z OSM / ZABAGED / DTM).
            joined: list[tuple[str, list[tuple[float, float]]]] = []
            for code in dict.fromkeys(c for c, _ln in lines + connectors):
                group = [ln for c, ln in lines + connectors if c == code]
                joined.extend((code, ln) for ln in dtm_fences.merge_lines(group))
            lines = joined
            if sprint and not olive_geom.is_empty:
                polys = [olive_geom] if olive_geom.geom_type == "Polygon" else list(getattr(olive_geom, "geoms", []))
                olive_polys = [
                    [list(p.exterior.coords)] + [list(r.coords) for r in p.interiors]
                    for p in polys
                    if p.geom_type == "Polygon" and not p.is_empty
                ]
                lines = dtm_fences.orient_fences(lines, {**areas, "olive": list(areas["olive"]) + olive_polys})
    except Exception:  # dedup a propojení jsou doplněk, ploty se kreslí i bez nich
        pass
    try:
        paths = path_clearances(work_dir, path_source) if sprint else []
        if paths:
            # Obrysy výplní se posouvají stejnou funkcí polohy jako ploty → zůstanou na plotech.
            moved = dtm_fences.avoid_paths(lines + [("520", r) for r in fill_rings], paths)
            lines, fill_rings = moved[: len(lines)], [r for _c, r in moved[len(lines) :]]
    except Exception:  # odsun je kosmetika, ploty se kreslí i bez něj
        pass
    symbol_cache: dict[str, int | None] = {}
    objects: list[str] = []
    area_objects: list[str] = []
    olive_idx = symbol_index_for_code(preset_id, scale, "520") if fill_olive else None
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
