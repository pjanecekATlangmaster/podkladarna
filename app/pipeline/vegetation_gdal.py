from __future__ import annotations

import math
from pathlib import Path

from app.pipeline.crs_5514 import write_prj
from app.pipeline.geom_diff import difference_polygon_wkb, union_polygon_wkbs
from app.pipeline.georef import read_pgw
from app.pipeline.oom_import import (
    OomObjectPart,
    _geom_parts_to_objects,
    _pyogrio_layer_rows,
    _wkb_parts,
)
from app.pipeline.oom_symbol_map import symbol_index_for_code
from app.proj_env import ensure_proj_data

# KP palette (lightgreentone=200) → ISOM plochy.
# Třída v rastru: 0 pozadí, 1 open land, 2–4 zeleně.
# Žlutá (401) se do OOM bere z KP, ale před zápisem se z ní odečtou plochy
# ZABAGED/OSM kreslené jinak (louka, parková zeleň, orná 412) – jinak dvojité 401.
_RGB_TO_CLASS: dict[tuple[int, int, int], int] = {
    (255, 219, 166): 1,  # yellow → 401
    (200, 254, 200): 2,  # nejsvětlejší → 406
    (180, 246, 180): 2,
    (160, 239, 160): 2,
    (140, 231, 140): 3,  # střed → 408
    (120, 224, 120): 3,
    (100, 217, 100): 3,
    (80, 209, 80): 4,  # nejtmavší → 410
}

_CLASS_TO_CODE: dict[int, str] = {
    1: "401",
    2: "406",
    3: "408",
    4: "410",
}

_CLASS_NAMES: dict[str, str] = {
    "401": "Otevřený terén (KP)",
    "406": "Vegetace pomalý běh (KP)",
    "408": "Vegetace chůze (KP)",
    "410": "Vegetace boj (KP)",
}

_MIN_AREA_M2 = 12.0
# Po zapnutí yellow_smoothing jsou hrany méně „pixelové“ – mírně vyšší simplify.
_SIMPLIFY_M = 1.5
# Velké KP 401 se v OOM těžko editují → rozřezat mřížkou na menší objekty.
_YELLOW_SPLIT_CELL_M = 50.0
_YELLOW_SPLIT_MIN_AREA_M2 = 2000.0


def rgb_to_vege_class(r: int, g: int, b: int) -> int:
    if (r, g, b) in _RGB_TO_CLASS:
        return _RGB_TO_CLASS[(r, g, b)]
    if r >= 250 and g >= 250 and b >= 250:
        return 0
    return 0


def vege_class_to_oom_code(cls: int) -> str | None:
    return _CLASS_TO_CODE.get(cls)


def _vegetation_paths(work_dir: Path) -> tuple[Path, Path]:
    temp = work_dir / "temp"
    return temp / "vegetation.png", temp / "vegetation.pgw"


def _classify_rgb_arrays(r, g, b):
    import numpy as np

    out = np.zeros(r.shape, dtype=np.uint8)
    for (rr, gg, bb), cls in _RGB_TO_CLASS.items():
        out[(r == rr) & (g == gg) & (b == bb)] = cls
    return out


def generate_vegetation_shapefile(
    png: Path,
    pgw: Path,
    dest_shp: Path,
    *,
    log=None,
) -> Path | None:
    """Klasifikuje KP vegetation.png a polygonizuje do shapefile (atribut code)."""
    ensure_proj_data()
    try:
        from osgeo import gdal, ogr
        import numpy as np
    except ImportError:
        if log:
            log("Zeleň vektory: osgeo/GDAL není k dispozici – přeskočeno")
        return None

    gdal.UseExceptions()
    ogr.UseExceptions()

    src = gdal.Open(str(png))
    if src is None:
        if log:
            log(f"Zeleň vektory: nelze otevřít {png.name}")
        return None

    if src.RasterCount == 1:
        expanded = gdal.Translate(
            "", src, format="MEM", outputType=gdal.GDT_Byte, rgbExpand="rgb"
        )
        src = expanded

    if src.RasterCount < 3:
        if log:
            log("Zeleň vektory: očekáván RGB rastr")
        return None

    width, height = src.RasterXSize, src.RasterYSize
    r = src.GetRasterBand(1).ReadAsArray()
    g = src.GetRasterBand(2).ReadAsArray()
    b = src.GetRasterBand(3).ReadAsArray()
    classified = _classify_rgb_arrays(r, g, b)

    georef = read_pgw(pgw)
    geotransform = (
        georef.origin_x,
        georef.pixel_x,
        georef.rot_row,
        georef.origin_y,
        georef.rot_col,
        georef.pixel_y,
    )

    dest_shp.parent.mkdir(parents=True, exist_ok=True)
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        dest_shp.with_suffix(suffix).unlink(missing_ok=True)

    # Bez SRS v GDAL vrstvě – ImportFromEPSG/proj_identify v Dockeru padá na PROJ DB.
    # Souřadnice jsou S-JTSK metry; .prj doplníme až po zápisu (write_prj).
    mem_drv = gdal.GetDriverByName("MEM")
    class_ds = mem_drv.Create("", width, height, 1, gdal.GDT_Byte)
    class_ds.SetGeoTransform(geotransform)
    band = class_ds.GetRasterBand(1)
    band.WriteArray(np.asarray(classified, dtype=np.uint8))
    band.SetNoDataValue(0)

    driver = ogr.GetDriverByName("ESRI Shapefile")
    out_ds = driver.CreateDataSource(str(dest_shp))
    layer = out_ds.CreateLayer("vegetation", None, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("cls", ogr.OFTInteger))
    layer.CreateField(ogr.FieldDefn("code", ogr.OFTString))

    gdal.Polygonize(band, band, layer, 0, [], callback=None)

    kept: list[tuple[int, str, object]] = []
    to_delete: list[int] = []
    for feature in layer:
        cls = int(feature.GetField("cls") or 0)
        code = vege_class_to_oom_code(cls)
        fid = feature.GetFID()
        to_delete.append(fid)
        if not code:
            continue
        geom = feature.GetGeometryRef()
        if geom is None:
            continue
        simplified = geom.SimplifyPreserveTopology(_SIMPLIFY_M)
        if simplified is None or simplified.IsEmpty():
            continue
        pieces = (
            split_yellow_polygon(simplified)
            if code == "401"
            else [simplified]
        )
        for piece in pieces:
            if piece is None or piece.IsEmpty():
                continue
            if float(piece.GetArea()) < _MIN_AREA_M2:
                continue
            kept.append((cls, code, piece.Clone()))
    for fid in to_delete:
        layer.DeleteFeature(fid)
    for cls, code, geom in kept:
        feat = ogr.Feature(layer.GetLayerDefn())
        feat.SetField("cls", cls)
        feat.SetField("code", code)
        feat.SetGeometry(geom)
        layer.CreateFeature(feat)
        feat = None

    n_kept = layer.GetFeatureCount()
    out_ds = None
    class_ds = None
    src = None

    if not dest_shp.is_file():
        return None
    write_prj(dest_shp)
    if log:
        log(f"Zeleň vektory: {n_kept} polygonů → {dest_shp.name}")
    return dest_shp


def split_yellow_polygon(
    geom,
    *,
    cell_m: float = _YELLOW_SPLIT_CELL_M,
    min_area_m2: float = _YELLOW_SPLIT_MIN_AREA_M2,
):
    """Rozřeže velkou KP žlutou mřížkou; malé polygony nechá beze změny.

    Výsledek jsou samostatné plochy (snadnější mazání/úpravy v OOM) se
    zachovaným vnějším tvarem – řezy jdou jen vnitřkem.
    """
    try:
        from osgeo import ogr
    except ImportError:
        return [geom]
    if geom is None or geom.IsEmpty():
        return []
    try:
        area = float(geom.GetArea())
    except Exception:
        return [geom]
    if area < min_area_m2 or cell_m <= 0:
        return [geom]

    minx, maxx, miny, maxy = geom.GetEnvelope()
    # Zarovnat mřížku na násobky cell_m, ať sousední polygony sedí na stejných řezech.
    x0 = math.floor(minx / cell_m) * cell_m
    y0 = math.floor(miny / cell_m) * cell_m
    out: list = []
    y = y0
    while y < maxy - 1e-9:
        x = x0
        y1 = y + cell_m
        while x < maxx - 1e-9:
            x1 = x + cell_m
            ring = ogr.Geometry(ogr.wkbLinearRing)
            ring.AddPoint(x, y)
            ring.AddPoint(x1, y)
            ring.AddPoint(x1, y1)
            ring.AddPoint(x, y1)
            ring.AddPoint(x, y)
            cell = ogr.Geometry(ogr.wkbPolygon)
            cell.AddGeometry(ring)
            try:
                inter = geom.Intersection(cell)
            except Exception:
                inter = None
            if inter is not None and not inter.IsEmpty():
                out.extend(_explode_area_geoms(inter, min_area=_MIN_AREA_M2))
            x = x1
        y = y1
    return out or [geom]


def _explode_area_geoms(geom, *, min_area: float) -> list:
    from osgeo import ogr

    out: list = []

    def keep(g) -> None:
        if g is None or g.IsEmpty():
            return
        try:
            if float(g.GetArea()) < min_area:
                return
        except Exception:
            return
        gtype = g.GetGeometryType() % 1000
        if gtype == 3:  # Polygon
            out.append(g.Clone())
        elif gtype == 6:  # MultiPolygon
            for i in range(g.GetGeometryCount()):
                keep(g.GetGeometryRef(i))
        elif gtype == 7:  # GeometryCollection
            for i in range(g.GetGeometryCount()):
                keep(g.GetGeometryRef(i))

    keep(geom)
    return out


def generate_job_vegetation(work_dir: Path, *, log=None) -> Path | None:
    png, pgw = _vegetation_paths(work_dir)
    if not png.is_file() or not pgw.is_file():
        if log:
            log("Zeleň vektory: chybí temp/vegetation.png|.pgw")
        return None
    dest = work_dir / "vegetation" / "vegetation.shp"
    if log:
        log("=== Fáze: zeleň KP → polygony ===")
    return generate_vegetation_shapefile(png, pgw, dest, log=log)


def _iter_vege_rows(shp: Path):
    """Čte SHP bez načítání .prj (PROJ identify v Dockeru padá)."""
    ensure_proj_data()
    try:
        from osgeo import ogr
    except ImportError:
        ogr = None
    if ogr is not None:
        prj = shp.with_suffix(".prj")
        prj_aside: Path | None = None
        if prj.is_file():
            prj_aside = prj.with_suffix(".prj.aside")
            prj_aside.unlink(missing_ok=True)
            prj.rename(prj_aside)
        try:
            ds = ogr.Open(str(shp))
            if ds:
                layer = ds.GetLayer(0)
                if layer is not None:
                    for feature in layer:
                        geom = feature.GetGeometryRef()
                        if geom is None:
                            continue
                        props: dict[str, object] = {}
                        for i in range(feature.GetFieldCount()):
                            defn = feature.GetFieldDefnRef(i)
                            if defn:
                                props[defn.GetName()] = feature.GetField(i)
                        yield props, bytes(geom.ExportToWkb())
                    return
        finally:
            if prj_aside is not None and prj_aside.is_file():
                prj.unlink(missing_ok=True)
                prj_aside.rename(prj)
    try:
        import pyogrio
    except ImportError:
        return
    for layer_name, _t in pyogrio.list_layers(shp):
        yield from _pyogrio_layer_rows(shp, layer=layer_name)


def build_vegetation_parts(
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    subtract_wkbs: list[bytes] | None = None,
) -> list[OomObjectPart]:
    shp = work_dir / "vegetation" / "vegetation.shp"
    if not shp.is_file():
        return []

    mask = union_polygon_wkbs(subtract_wkbs or [])
    grouped: dict[str, list[str]] = {code: [] for code in _CLASS_TO_CODE.values()}
    for props, wkb in _iter_vege_rows(shp):
        code = str(props.get("code") or "")
        if code not in grouped:
            cls = props.get("cls")
            try:
                mapped = vege_class_to_oom_code(int(cls)) if cls is not None else None
            except (TypeError, ValueError):
                mapped = None
            code = mapped or ""
        if code not in grouped:
            continue
        symbol_index = symbol_index_for_code(preset_id, scale, code)
        if symbol_index is None:
            continue
        pieces = [wkb]
        if code == "401" and mask is not None and wkb:
            pieces = difference_polygon_wkb(bytes(wkb), mask)
        for piece in pieces:
            geom_parts, _ = _wkb_parts(piece)
            grouped[code].extend(
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

    parts: list[OomObjectPart] = []
    for code in ("401", "406", "408", "410"):
        objects = grouped[code]
        if objects:
            parts.append(
                OomObjectPart(
                    name=_CLASS_NAMES[code],
                    objects_xml="\n".join(objects),
                    count=len(objects),
                )
            )
    return parts
