"""Les a MTBO: jedna plocha zpevněného povrchu a její okraj z hotových čar.

Plochy kreslené z DTM (koridor silnice, velká parkoviště, v MTBO široké chodníky) se místo samostatných částí
s vlastním obrysem přidají jako prvky do sítě zpevněných ploch (``osm_paths.build_paved_network``), kde se sloučí
s plochami z OSM a ZABAGED do jedné geometrie P. Výplň P kreslí ``build_osm_feature_parts`` (501.1), okraj kreslí
až ``build_paved_edge_parts`` nad hotovými čarami cest: vynechá se tam, kde by ho překryla kresba čáry
(ústí silnice do plochy, silnice souběžná s okrajem), ne podle odhadu z OSM os.

Návrh a diagnóza: dokument „Les a MTBO: napojování silnic a zpevněných ploch“ (2026-10-26). ``UNIFIED_PAVED = False``
vrátí kresbu z 2.35.4 (samostatný koridor, parkoviště a chodníky z DTM s vlastním obrysem).
"""

from __future__ import annotations

from pathlib import Path

from app.pipeline.geom_clip import Bounds
from app.pipeline.oom_import import OomObjectPart

UNIFIED_PAVED = True
# ``kind`` prvků ploch z DTM v síti zpevněných ploch (je v ``_PAVED_AREA_KINDS`` i ``ROAD_CLIP_KINDS``).
DTM_PAVED_KIND = "paved_dtm"
# Úsek okraje v širokém pásu čáry (půlka kresby + půlka okraje + mezera čitelnosti) delší než tolikanásobek příčného
# ústí je souběh čáry s okrajem: okraj by vedle čáry dělal dvojitou linii, proto se vynechá celý.
EDGE_PARALLEL_FACTOR = 1.2


def unified(preset_id: str) -> bool:
    """Kreslí se v presetu zpevněný povrch jako jedna plocha s okrajem z hotových čar (les a MTBO)?"""
    return UNIFIED_PAVED and bool(preset_id) and not preset_id.startswith("sprint")


def _polygons(geom):
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon" and not g.is_empty]


# Mřížky přichycení (m) pro opakování překryvu ploch, který GEOS odmítl (TopologyException u téměř shodných hran).
OVERLAY_RETRY_GRIDS = (0.01, 0.05)


def _overlay(op, *args):
    """``op`` (shapely ``union_all`` / ``difference`` …) s pojistkou: při ``GEOSException`` se vstupy opraví
    ``make_valid`` a zkusí se znovu s přichycením na mřížku ``OVERLAY_RETRY_GRIDS``. Nevyjde-li ani to, vrátí None –
    dotažení výplně se vynechá, ale mapa vznikne (job 61ce38ad5427, MTBO Barrandov 2026-10-27)."""
    import logging

    import shapely
    from shapely.errors import GEOSException

    try:
        return op(*args)
    except GEOSException as exc:
        err = exc
    try:
        fixed = [[shapely.make_valid(g) for g in a] if isinstance(a, list) else shapely.make_valid(a) for a in args]
    except GEOSException as exc:
        err, fixed = exc, None
    for grid in OVERLAY_RETRY_GRIDS if fixed is not None else ():
        try:
            return op(*fixed, grid_size=grid)
        except GEOSException as exc:
            err = exc
    geoms = [g for a in args for g in (a if isinstance(a, list) else [a])]
    logging.getLogger(__name__).warning(
        "Zpevněné plochy: překryv %s selhal i s přichycením na mřížku, dotažení výplně se vynechá (rozsah %s): %s",
        getattr(op, "__name__", op), shapely.total_bounds(geoms).round(1).tolist(), err,
    )
    return None


def dtm_area_features(work_dir: Path, preset_id: str, scale: int, path_source: str = "mixed") -> list[dict]:
    """Plochy kreslené z DTM (koridor silnice, velká parkoviště, MTBO široké chodníky) jako prvky GeoJSON pro síť
    zpevněných ploch (``kind`` ``DTM_PAVED_KIND``, ``source`` ``dtm``); prázdný seznam bez DTM / mimo sjednocený režim."""
    from app.pipeline import dtm_import as di

    if not unified(preset_id) or not di.dtm_area_preset(preset_id):
        return []
    from shapely.geometry import mapping

    geoms = [
        ("corridor", di.corridor_geometry_for(work_dir, preset_id, scale, path_source)),
        ("parking", di.dtm_parking_geometry(work_dir, scale, path_source, preset_id)),
    ]
    if preset_id.startswith("mtbo"):
        geoms.append(("sidewalk", di.mtbo_sidewalk_geometry(work_dir, scale, path_source, preset_id)))
    out: list[dict] = []
    for label, geom in geoms:
        for poly in _polygons(geom):
            if poly.area < 1.0:
                continue
            out.append(
                {
                    "type": "Feature",
                    "properties": {"source": "dtm", "kind": DTM_PAVED_KIND, "oom_code": "501", "dtm_kind": label},
                    "geometry": mapping(poly),
                }
            )
    return out


def generalize_paved_feats(work_dir: Path, feats: list[dict], preset_id: str, scale: int) -> list[dict]:
    """Plochy sítě zpevněných ploch (OSM, ZABAGED i DTM) generalizované podle mm mapy jako plochy z DTM
    (``dtm_sidewalks.generalize_area``: zálivy a škvíry do 0,6 mm, výběžky do 0,5 mm, díry pod 1 mm², kusy pod 0,5 mm²).

    Obrys z OSM kopíruje kiosky a záhonky u parkoviště a okraj 501.2 pak kreslí zuby (Barrandov u Lidlu, Petr Retest 6
    2026-10-28). Budovy aspoň minimální velikosti (``osm_paths.building_min_area_m2``) zacelení nepřekročí; každá plocha
    zvlášť (plochy na mostě / v tunelu a kusy rozdělené silnicí se nespojí).
    """
    if not unified(preset_id) or scale <= 0 or not feats:
        return feats
    from shapely.errors import GEOSException
    from shapely.geometry import mapping, shape

    from app.pipeline import dtm_sidewalks
    from app.pipeline.dtm_import import _buildings_zone
    from app.pipeline.osm_paths import building_min_area_m2

    blockers = _buildings_zone(work_dir, tol_m=0.0, min_area_m2=building_min_area_m2(preset_id, scale))
    out: list[dict] = []
    for f in feats:
        geom = f.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            out.append(f)
            continue
        try:
            g = shape(geom)
            g = g if g.is_valid else g.buffer(0)
            gen = dtm_sidewalks.generalize_area(g, scale, blockers=blockers)
        except (ValueError, TypeError, GEOSException):
            out.append(f)
            continue
        for poly in _polygons(gen):
            out.append({**f, "geometry": mapping(poly)})
    return out


def restore_junction_mouths(work_dir: Path, feats: list[dict], preset_id: str, scale: int) -> list[dict]:
    """Ústí silnic do ploch složitých křižovatek (``junction_areas.restore_mouths``) znovu po generalizaci sítě:
    generalizace zaoblí rohy ústí a sloučení přidá výběžky okolních ploch, okraj 501.2 pak nenavazuje na okrajové linky
    silnice, která končí přesně na ústí (zlomy a háčky okraje, Petr 2026-10-29, K Barrandovu u tramvaje)."""
    from app.pipeline import dtm_import as di
    from app.pipeline import junction_areas

    if not feats or not unified(preset_id) or not di.dtm_area_preset(preset_id):
        return feats
    if not (junction_areas.ROAD_AREAS and junction_areas.JUNCTION_AREAS):
        return feats
    entries = junction_areas.junction_mouths(work_dir, preset_id, scale)
    if not entries:
        return feats
    from shapely.errors import GEOSException
    from shapely.geometry import mapping, shape

    out: list[dict] = []
    for f in feats:
        geom = f.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon") or (f.get("properties") or {}).get("structure"):
            out.append(f)
            continue
        try:
            g = shape(geom)
            new = junction_areas.restore_mouths(g if g.is_valid else g.buffer(0), entries, scale)
        except (ValueError, TypeError, GEOSException):
            out.append(f)
            continue
        if new is None or new is g:
            out.append(f)
            continue
        out.extend({**f, "geometry": mapping(poly)} for poly in _polygons(new))
    return out


def square_road_mouths(work_dir: Path, feats: list[dict], preset_id: str, scale: int, path_feats: list[dict] | None = None) -> list[dict]:
    """Ústí každé kreslené silnice do plochy sítě kolmo na čáru (``junction_areas.square_mouths``), les a MTBO, i bez DTM:
    šikmá hrana plochy nechávala vedle kolmého konce silnice bílý klín a na druhé straně okraj 501.2 křižoval
    okrajovou linku silnice (oponentura 2026-10-31, mezera 1). Čáry silnic se prodlouží o ``ROAD_EDGE_REACH_MM``
    jako při dotažení konců (``end_at_edge``). ``SQUARE_MOUTHS = False`` vypne."""
    if not SQUARE_MOUTHS or not feats or not unified(preset_id):
        return feats
    from shapely import STRtree
    from shapely.errors import GEOSException
    from shapely.geometry import mapping, shape

    from app.pipeline import junction_areas
    from app.pipeline.osm_paths import ROAD_EDGE_REACH_MM, mm_to_ground_m

    reach = mm_to_ground_m(ROAD_EDGE_REACH_MM, scale)
    lines = junction_areas._entering_lines(work_dir, preset_id, scale, features=path_feats, extend_m=reach + 2.0)
    if not lines:
        return feats
    tree = STRtree([ln for ln, _half in lines])
    depth = mm_to_ground_m(junction_areas.MOUTH_TAPER_MM, scale)
    out: list[dict] = []
    for f in feats:
        geom = f.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon") or (f.get("properties") or {}).get("structure"):
            out.append(f)
            continue
        try:
            g = shape(geom)
            g = g if g.is_valid else g.buffer(0)
            near = [lines[int(k)] for k in tree.query(g)]
            entries = junction_areas.road_mouth_entries(g, near) if near else []
            new = junction_areas.square_mouths(g, entries, depth) if entries else g
        except (ValueError, TypeError, GEOSException):
            out.append(f)
            continue
        if new is None or new is g:
            out.append(f)
            continue
        out.extend({**f, "geometry": mapping(poly)} for poly in _polygons(new))
    return out


# Ústí silnic kolmo na čáru u všech ploch sítě (``square_road_mouths``); False = jen křižovatky z DTM jako dřív.
SQUARE_MOUTHS = True


def _merged_pieces(cut):
    """Kusy okraje v pásu čáry spojené přes počáteční vrchol obrysu (průnik je tam rozdělený na dva kusy a krátký
    souběh by se počítal po částech)."""
    if cut.geom_type != "MultiLineString":
        return cut
    from shapely.ops import linemerge

    return linemerge(cut)


def _parallel_piece(piece, line, wide_r: float, scale: int) -> bool:
    """Je kus okraje ``piece`` (uvnitř pásu ``wide_r`` kolem čáry ``line``) souběh s čárou, ne křížení? Souběh: delší než
    ``EDGE_PARALLEL_FACTOR`` × šířka pásu (dřívější jediné kritérium, ~21 m v lese 1:10 000 – krátký souběh dával
    dvojitou linii), nebo aspoň ``EDGE_PARALLEL_MIN_MM`` mapy a tětiva kusu s čárou svírá nejvýš ``EDGE_PARALLEL_MAX_DEG``."""
    import math

    from shapely.geometry import Point

    from app.pipeline.osm_paths import mm_to_ground_m

    if piece.length > EDGE_PARALLEL_FACTOR * 2.0 * wide_r:
        return True
    if piece.length < mm_to_ground_m(EDGE_PARALLEL_MIN_MM, scale):
        return False
    c = list(piece.coords)
    (ax, ay), (bx, by) = c[0], c[-1]
    cx, cy = bx - ax, by - ay
    n = math.hypot(cx, cy)
    if n < 1e-6:
        return False
    mid = piece.interpolate(0.5, normalized=True)
    t = line.project(mid)
    a, b = line.interpolate(max(0.0, t - 1.0)), line.interpolate(min(line.length, t + 1.0))
    ex, ey = b.x - a.x, b.y - a.y
    e = math.hypot(ex, ey)
    if e < 1e-9:
        return False
    return abs(cx * ex + cy * ey) / (n * e) >= math.cos(math.radians(EDGE_PARALLEL_MAX_DEG))


# Krátký souběh okraje s čárou (``_parallel_piece``): od této délky na mapě (mm) a do tohoto úhlu tětivy k čáře (°).
EDGE_PARALLEL_MIN_MM = 0.6
EDGE_PARALLEL_MAX_DEG = 30.0


def edge_omit_zone(network, road_bands, preset_id: str, scale: int):
    """Kde se okraj plochy ``network`` nekreslí (shapely) nebo None: pás každé kreslené čáry cesty (``road_bands`` =
    ``path_clearance.drawn_path_bands``: ``(osa, půlka šířky m)``) rozšířený o 0,3 m – ústí čáry do plochy;
    a úseky okraje, které vedou souběžně s čárou blíž než mezera čitelnosti (``LINE_MIN_GAP_MM``)."""
    if network is None or network.is_empty or not road_bands:
        return None
    from shapely import STRtree
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    from app.pipeline.dtm_import import JUNCTION_END_TOL_M, JUNCTION_EXTRA_M
    from app.pipeline.osm_paths import LINE_MIN_GAP_MM, _extend_ends, mm_to_ground_m, paved_edge_width_m

    rings = []
    for poly in _polygons(network):
        rings.append(LineString(poly.exterior.coords))
        rings.extend(LineString(r.coords) for r in poly.interiors)
    if not rings:
        return None
    tree = STRtree(rings)
    gap = paved_edge_width_m(preset_id, scale) / 2.0 + mm_to_ground_m(LINE_MIN_GAP_MM, scale)
    zones = []
    for band in road_bands:
        coords, half = band[0], band[1]
        fill = band[2] if len(band) > 2 else half + JUNCTION_EXTRA_M  # půlka výplně mezi okrajovými linkami silnice
        if len(coords) < 2:
            continue
        wide_r = half + gap
        line = LineString(coords)
        near = [rings[int(k)] for k in tree.query(line.buffer(wide_r))]
        near = [r for r in near if r.distance(line) <= wide_r]
        if not near:
            continue
        ext = LineString(_extend_ends(coords, JUNCTION_END_TOL_M))
        # ústí: okraj plochy končí na okrajové lince silnice (šířka výplně), u čáry bez okrajů na jejím kraji
        zones.append(ext.buffer(fill, cap_style=2))
        wide = ext.buffer(wide_r, cap_style=2)
        for ring in near:
            cut = _merged_pieces(ring.intersection(wide))
            for piece in [cut] if cut.geom_type == "LineString" else getattr(cut, "geoms", []):
                if piece.geom_type == "LineString" and _parallel_piece(piece, line, wide_r, scale):
                    zones.append(piece.buffer(0.1))  # souběh: okraj vedle čáry by byl dvojitá linie
    return unary_union(zones) if zones else None


def road_fill_strips(network, road_bands, preset_id: str, scale: int):
    """Pásy výplně mezi obrysem plochy ``network`` a vnější hranou okrajové linky silnice, která vede souběžně s okrajem
    (stejné kritérium souběhu jako ``edge_omit_zone``): okraj plochy se u souběžné silnice vynechá a bez dotažení
    výplně zůstával otevřený obrys s mezerou (Petr 2026-10-26: „navazovat okraje plochy a silnice kreslené rovnou
    s okrajem“; Lamačova, D25). Vrací shapely nebo None."""
    if network is None or network.is_empty or not road_bands:
        return None
    from shapely import STRtree
    from shapely.geometry import LineString, MultiLineString, Point, Polygon
    from shapely.ops import nearest_points

    from app.pipeline.dtm_import import JUNCTION_END_TOL_M, JUNCTION_EXTRA_M
    from app.pipeline.osm_paths import LINE_MIN_GAP_MM, _extend_ends, mm_to_ground_m, paved_edge_width_m

    rings = []
    for poly in _polygons(network):
        rings.append(LineString(poly.exterior.coords))
        rings.extend(LineString(r.coords) for r in poly.interiors)
    if not rings:
        return None
    tree = STRtree(rings)
    gap = paved_edge_width_m(preset_id, scale) / 2.0 + mm_to_ground_m(LINE_MIN_GAP_MM, scale)
    strips = []
    for band in road_bands:
        coords, half = band[0], band[1]
        fill = band[2] if len(band) > 2 else half + JUNCTION_EXTRA_M
        if len(coords) < 2 or fill <= 0:
            continue  # čára bez výplně (pěšina) okraj nenahradí
        wide_r = half + gap
        line = LineString(coords)
        near = [rings[int(k)] for k in tree.query(line.buffer(wide_r))]
        near = [r for r in near if r.distance(line) <= wide_r]
        if not near:
            continue
        ext = LineString(_extend_ends(coords, JUNCTION_END_TOL_M))
        wide = ext.buffer(wide_r, cap_style=2)
        # vnější hrana okrajové linky silnice; zalomená čára dá i MultiPolygon (job 3082b1ae8292, MTBO)
        border = MultiLineString([q.exterior.coords for q in _polygons(ext.buffer(half, cap_style=2))])
        if border.is_empty:
            continue
        for ring in near:
            cut = _merged_pieces(ring.intersection(wide))
            for piece in [cut] if cut.geom_type == "LineString" else getattr(cut, "geoms", []):
                if piece.geom_type != "LineString" or not _parallel_piece(piece, line, wide_r, scale):
                    continue
                pts = [(float(x), float(y)) for x, y in piece.coords]
                qs = [nearest_points(Point(p), border)[1] for p in pts]
                poly = Polygon(pts + [(q.x, q.y) for q in reversed(qs)]).buffer(0)
                if not poly.is_empty and poly.area > 0.5:
                    strips.append(poly)
    if not strips:
        return None
    import shapely

    out = _overlay(shapely.union_all, strips)
    out = _overlay(shapely.difference, out, network) if out is not None else None
    return None if out is None or out.is_empty else out


def build_paved_edge_parts(
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    clip_bounds: Bounds | None = None,
    road_bands=None,
) -> list[OomObjectPart]:
    """Okraj jedné plochy zpevněného povrchu (les 501.2, MTBO 501.100) bez ústí kreslených čar; výplň kreslí
    ``build_osm_feature_parts``. Prázdný seznam mimo sjednocený režim nebo bez ploch."""
    if not unified(preset_id):
        return []
    from app.pipeline.dtm_import import _geometry_part
    from app.pipeline.osm_paths import paved_edge_code, paved_network_geometry

    network = paved_network_geometry(work_dir, preset_id, scale)
    extra: list[OomObjectPart] = []
    # Plochy na mostě / v tunelu (``structure``) nejsou v síti (neslučují se), výplň se kreslí, okraj dosud ne.
    from shapely.geometry import shape as _shape
    from shapely.ops import unary_union as _uu_struct

    from app.pipeline.osm_paths import paved_network_feats

    _structs = [
        _shape(f["geometry"])
        for f in paved_network_feats(work_dir, preset_id, scale)
        if (f.get("properties") or {}).get("structure") and (f.get("geometry") or {}).get("type") in ("Polygon", "MultiPolygon")
    ]
    if _structs:
        structures = _uu_struct([g if g.is_valid else g.buffer(0) for g in _structs])
        if not structures.is_empty:
            extra += _geometry_part(
                structures,
                name="Zpevněné plochy na mostě / v tunelu",
                code="501.1",
                fill_code="501.1",
                edge_code=paved_edge_code(preset_id),
                preset_id=preset_id,
                scale=scale,
                ref_x=ref_x,
                ref_y=ref_y,
                grivation_deg=grivation_deg,
                clip_bounds=clip_bounds,
                outline_separate=True,
                edge_omit=edge_omit_zone(structures, road_bands, preset_id, scale),
                edge_only=True,
            )
    if network is None or network.is_empty:
        return extra
    strips = road_fill_strips(network, road_bands, preset_id, scale)
    if strips is not None and not strips.is_empty:
        import shapely
        from shapely.geometry import box

        from app.pipeline.dtm_sidewalks import polygon_rings
        from app.pipeline.oom_coords import projected_to_map_coord
        from app.pipeline.oom_import import _area_object_with_holes
        from app.pipeline.oom_symbol_map import symbol_index_for_code

        merged = _overlay(shapely.union_all, [network, strips])
        if merged is not None:
            network = merged
        fill_idx = symbol_index_for_code(preset_id, scale, "501.1")
        drawn = strips.intersection(box(*clip_bounds)) if clip_bounds is not None else strips
        objs = []
        for rings in polygon_rings(drawn) if fill_idx is not None else []:
            obj = _area_object_with_holes(
                fill_idx,
                [[projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg) for x, y in r] for r in rings],
            )
            if obj:
                objs.append(obj)
        if objs:
            extra.append(OomObjectPart(name="Zpevněné plochy (síť) – dotažení k silnici (501.1)", objects_xml="\n".join(objs), count=len(objs)))
    return extra + _geometry_part(
        network,
        name="Zpevněné plochy (síť)",
        code="501.1",  # lesní sada ISOM 2017-2 nemá 501.0; symbol výplně se tu stejně nekreslí
        fill_code="501.1",
        edge_code=paved_edge_code(preset_id),
        preset_id=preset_id,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        grivation_deg=grivation_deg,
        clip_bounds=clip_bounds,
        outline_separate=True,
        edge_omit=edge_omit_zone(network, road_bands, preset_id, scale),
        edge_only=True,
    )
