"""Chodníky z DTM kraje (ZPS_OPL, ``ChodnikPlocha``) sjednocené s OSM / ZABAGED.

DTM kreslí chodníky jako plochy (detail sídlišť, dlažby), OSM jako čáry (aktuálnější, ale
hrubší). Postup: DTM plochy (úroveň 0) se vyčistí a sloučí; čáry chodníků z OSM / ZABAGED se
o DTM plochy (+ ``OSM_COVER_M``) ořežou; zbytky delší než ``OSM_MIN_REMAINDER_M`` (to, co DTM
nezná) se rozšíří na šířku chodníku a sloučí s DTM do jedné plochy. Tvar má přednost z DTM.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.pipeline.jvf_reader import iter_records

SIDEWALK_TYPES = ("ChodnikPlocha",)
CACHE_SUFFIX = ".sidewalk1.json"
# Sloučení sousedních ploch a zacelení škvír užších než 2 × tato hodnota (m).
CLOSE_M = 0.5
# Díry menší než tato plocha (m²) se zacelí (drobné záhonky, sloupky).
MIN_HOLE_M2 = 2.0
SIMPLIFY_M = 0.1
# OSM / ZABAGED čára blíž než tato vzdálenost (m) k ploše z DTM je duplicitní.
OSM_COVER_M = 0.5
# Zbytek OSM čáry kratší než toto (m) se zahodí (střepy po ořezu).
OSM_MIN_REMAINDER_M = 3.0
# Šířka chodníku z OSM čáry (m) – jako symbol 501.10.
OSM_WIDTH_M = 1.4
# Plochy menší než tato (m²) se po sloučení zahodí.
MIN_AREA_M2 = 1.0


def load_sidewalk_polygons(opl_zip: str | Path, *, use_cache: bool = True) -> list[list[list[tuple[float, float]]]]:
    """Plochy chodníků na povrchu (úroveň 0) z balíku ZPS_OPL: ``[[vnější obrys, díra, …], …]``.

    Chodníky na mostech (úroveň > 0) a pod povrchem se nepoužijí – ty kreslí cesty z OSM.
    Výsledek se cachuje u balíku.
    """
    opl_zip = Path(opl_zip)
    cache = opl_zip.with_name(opl_zip.name + CACHE_SUFFIX)
    if use_cache and cache.is_file() and cache.stat().st_mtime >= opl_zip.stat().st_mtime:
        try:
            raw = json.loads(cache.read_text(encoding="utf-8"))
            return [[[(x, y) for x, y in ring] for ring in poly] for poly in raw]
        except (OSError, ValueError, TypeError):
            pass
    out: list[list[list[tuple[float, float]]]] = []
    for rec in iter_records(opl_zip, set(SIDEWALK_TYPES)):
        if rec.geom_type != "Polygon" or not rec.coords or rec.level != 0:
            continue
        poly = [[(float(c[0]), float(c[1])) for c in ring] for ring in rec.coords if len(ring) >= 4]
        if poly:
            out.append(poly)
    if use_cache:
        try:
            cache.write_text(
                json.dumps([[[list(p) for p in ring] for ring in poly] for poly in out]), encoding="utf-8"
            )
        except OSError:
            pass
    return out


def _clean(geom, *, close_m: float, min_hole_m2: float, simplify_m: float):
    """Zacelí škvíry, odstraní drobné díry a zjednoduší tvar."""
    from shapely.geometry import MultiPolygon, Polygon

    if geom.is_empty:
        return geom
    if close_m > 0:
        geom = geom.buffer(close_m, join_style=2).buffer(-close_m, join_style=2)
    polys = [geom] if geom.geom_type == "Polygon" else [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
    cleaned = []
    for p in polys:
        holes = [r for r in p.interiors if Polygon(r).area >= min_hole_m2]
        cleaned.append(Polygon(p.exterior, holes))
    geom = MultiPolygon(cleaned) if len(cleaned) != 1 else cleaned[0]
    if simplify_m > 0 and not geom.is_empty:
        geom = geom.simplify(simplify_m, preserve_topology=True)
    return geom


def unify_sidewalks(
    dtm_polys: list[list[list[tuple[float, float]]]],
    osm_lines: list[list[tuple[float, float]]],
    *,
    osm_width_m: float = OSM_WIDTH_M,
    cover_m: float = OSM_COVER_M,
    min_remainder_m: float = OSM_MIN_REMAINDER_M,
):
    """Jedna plocha chodníků: DTM (tvar) + zbytky OSM čar (aktuálnost); shapely geometrie nebo None."""
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union

    parts = []
    for poly in dtm_polys:
        try:
            g = Polygon(poly[0], poly[1:])
        except (ValueError, IndexError):
            continue
        g = g if g.is_valid else g.buffer(0)
        if not g.is_empty:
            parts.append(g)
    dtm = _clean(unary_union(parts), close_m=CLOSE_M, min_hole_m2=MIN_HOLE_M2, simplify_m=SIMPLIFY_M) if parts else None
    covered = dtm.buffer(cover_m) if dtm is not None and not dtm.is_empty else None
    extra = []
    for coords in osm_lines:
        if len(coords) < 2:
            continue
        line = LineString(coords)
        rest = line.difference(covered) if covered is not None else line
        pieces = [rest] if rest.geom_type == "LineString" else [g for g in getattr(rest, "geoms", []) if g.geom_type == "LineString"]
        for piece in pieces:
            if piece.length >= min_remainder_m:
                # Zbytek leží ≥ cover_m od DTM; kulatý konec široký ``osm_width_m`` do něj
                # kousek zasáhne, takže se po sjednocení napojí.
                extra.append(piece.buffer(osm_width_m / 2.0, cap_style=1, join_style=1))
    items = ([dtm] if dtm is not None and not dtm.is_empty else []) + extra
    if not items:
        return None
    # Sloučení škvír mezi OSM zbytkem a DTM (≤ 2 × cover_m − šířka) + stejné čištění jako u DTM.
    merged = _clean(unary_union(items), close_m=0.2, min_hole_m2=MIN_HOLE_M2, simplify_m=SIMPLIFY_M)
    polys = [merged] if merged.geom_type == "Polygon" else list(getattr(merged, "geoms", []))
    polys = [p for p in polys if p.geom_type == "Polygon" and p.area >= MIN_AREA_M2]
    if not polys:
        return None
    from shapely.geometry import MultiPolygon

    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def polygon_rings(geom) -> list[list[list[tuple[float, float]]]]:
    """``[[vnější obrys, díra, …], …]`` z (Multi)Polygonu."""
    if geom is None or geom.is_empty:
        return []
    polys = [geom] if geom.geom_type == "Polygon" else [g for g in geom.geoms if g.geom_type == "Polygon"]
    return [
        [[(float(x), float(y)) for x, y in p.exterior.coords]]
        + [[(float(x), float(y)) for x, y in r.coords] for r in p.interiors]
        for p in polys
    ]
