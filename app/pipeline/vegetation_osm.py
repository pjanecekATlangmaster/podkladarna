"""Záložní vegetace z OSM tam, kde zdroj výšek nemá povrch (zahraniční build, SK DMR 5.0 z WCS).

Bez CHM nevznikne ``work/vegetation/vegetation.shp`` a celá mapa je bílá (les). Louky, travní porosty, pole
a vřesoviště z OSM (``osm_paths/natural_cover.geojson``) se zapíšou jako otevřený prostor 401 (třída 1,
stejné pole ``cls`` / ``code`` jako vegetace z CHM); les zůstává bílý. Plochy lesa z OSM uvnitř louky mají
přednost (vyřízne se). Hustotu porostu (406–410) OSM spolehlivě nenese, proto se nekreslí.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.pipeline.crs_5514 import write_prj

OPEN_COVERS = frozenset({"meadow", "grassland", "farmland", "heath"})
FOREST_COVERS = frozenset({"forest", "wood"})
MIN_AREA_M2 = 100.0


def write_vegetation_from_osm(work_dir: Path, *, log=None) -> Path | None:
    from shapely.geometry import box, shape
    from shapely.geometry.polygon import orient
    from shapely.ops import unary_union

    from app.pipeline.job_grid import JobGrid
    from app.pipeline.osm_paths import NATURAL_COVER_NAME

    work_dir = Path(work_dir)
    src = work_dir / "osm_paths" / NATURAL_COVER_NAME
    if not src.is_file():
        if log:
            log("Vegetace z OSM: chybí přírodní kryt OSM – přeskočeno")
        return None
    open_polys, forest_polys = [], []
    for feat in json.loads(src.read_text(encoding="utf-8")).get("features") or []:
        cover = str((feat.get("properties") or {}).get("cover") or "").lower()
        if cover not in OPEN_COVERS and cover not in FOREST_COVERS:
            continue
        try:
            geom = shape(feat["geometry"]).buffer(0)
        except Exception:
            continue
        if geom.is_empty:
            continue
        (open_polys if cover in OPEN_COVERS else forest_polys).append(geom)
    if not open_polys:
        if log:
            log("Vegetace z OSM: ve výřezu nejsou louky ani pole")
        return None
    area = unary_union(open_polys)
    if forest_polys:
        area = area.difference(unary_union(forest_polys))
    grid = JobGrid.load(work_dir)
    if grid is not None:
        area = area.intersection(box(*grid.bounds()))
    polys = [p for p in getattr(area, "geoms", [area]) if p.geom_type == "Polygon" and p.area >= MIN_AREA_M2]
    if not polys:
        return None

    import shapefile

    dest = work_dir / "vegetation" / "vegetation.shp"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with shapefile.Writer(str(dest.with_suffix("")), shapeType=shapefile.POLYGON) as w:
        w.field("cls", "N", size=4)
        w.field("code", "C", size=8)
        for poly in polys:
            poly = orient(poly, sign=-1.0)  # shapefile: vnější obrys po směru hodin
            rings = [[[float(x), float(y)] for x, y in poly.exterior.coords]]
            rings += [[[float(x), float(y)] for x, y in hole.coords] for hole in poly.interiors]
            w.poly(rings)
            w.record(1, "401")
    write_prj(dest)
    if log:
        total = sum(p.area for p in polys) / 1e4
        log(f"Vegetace z OSM (bez výšky porostu): {len(polys)} ploch otevřeného prostoru 401, {total:.1f} ha")
    return dest
