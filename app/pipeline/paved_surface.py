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
        limit = EDGE_PARALLEL_FACTOR * 2.0 * wide_r
        for ring in near:
            cut = ring.intersection(wide)
            for piece in [cut] if cut.geom_type == "LineString" else getattr(cut, "geoms", []):
                if piece.geom_type == "LineString" and piece.length > limit:
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
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import nearest_points, unary_union

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
        border = ext.buffer(half, cap_style=2).exterior  # vnější hrana okrajové linky silnice
        limit = EDGE_PARALLEL_FACTOR * 2.0 * wide_r
        for ring in near:
            cut = ring.intersection(wide)
            for piece in [cut] if cut.geom_type == "LineString" else getattr(cut, "geoms", []):
                if piece.geom_type != "LineString" or piece.length <= limit:
                    continue
                pts = [(float(x), float(y)) for x, y in piece.coords]
                qs = [nearest_points(Point(p), border)[1] for p in pts]
                poly = Polygon(pts + [(q.x, q.y) for q in reversed(qs)]).buffer(0)
                if not poly.is_empty and poly.area > 0.5:
                    strips.append(poly)
    if not strips:
        return None
    out = unary_union(strips).difference(network)
    return None if out.is_empty else out


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
    if network is None or network.is_empty:
        return []
    extra: list[OomObjectPart] = []
    strips = road_fill_strips(network, road_bands, preset_id, scale)
    if strips is not None and not strips.is_empty:
        from shapely.geometry import box
        from shapely.ops import unary_union

        from app.pipeline.dtm_sidewalks import polygon_rings
        from app.pipeline.oom_coords import projected_to_map_coord
        from app.pipeline.oom_import import _area_object_with_holes
        from app.pipeline.oom_symbol_map import symbol_index_for_code

        network = unary_union([network, strips])
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
