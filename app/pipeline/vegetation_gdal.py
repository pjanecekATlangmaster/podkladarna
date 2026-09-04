from __future__ import annotations

from pathlib import Path

from app.pipeline.crs_5514 import CRS_PROJ4, CRS_WKT, write_prj
from app.pipeline.georef import read_pgw
from app.pipeline.oom_import import (
    OomObjectPart,
    _geom_parts_to_objects,
    _pyogrio_layer_rows,
    _wkb_parts,
)
from app.pipeline.oom_symbol_map import symbol_index_for_code

# KP palette (lightgreentone=200) → ISOM plochy.
# Třída v rastru: 0 pozadí, 1 open land, 2–4 zeleně.
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
_SIMPLIFY_M = 1.0


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
    try:
        from osgeo import gdal, ogr, osr
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

    mem_drv = gdal.GetDriverByName("MEM")
    class_ds = mem_drv.Create("", width, height, 1, gdal.GDT_Byte)
    class_ds.SetGeoTransform(geotransform)
    # Bez ImportFromEPSG – v Dockeru často chybí PROJ data (/opt/conda/share/proj).
    srs = osr.SpatialReference()
    if srs.ImportFromWkt(CRS_WKT) != 0:
        srs.ImportFromProj4(CRS_PROJ4)
    class_ds.SetProjection(srs.ExportToWkt())
    band = class_ds.GetRasterBand(1)
    band.WriteArray(np.asarray(classified, dtype=np.uint8))
    band.SetNoDataValue(0)

    driver = ogr.GetDriverByName("ESRI Shapefile")
    out_ds = driver.CreateDataSource(str(dest_shp))
    layer = out_ds.CreateLayer("vegetation", srs, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("cls", ogr.OFTInteger))
    layer.CreateField(ogr.FieldDefn("code", ogr.OFTString))

    gdal.Polygonize(band, band, layer, 0, [], callback=None)

    to_delete: list[int] = []
    for feature in layer:
        cls = int(feature.GetField("cls") or 0)
        code = vege_class_to_oom_code(cls)
        fid = feature.GetFID()
        if not code:
            to_delete.append(fid)
            continue
        geom = feature.GetGeometryRef()
        if geom is None:
            to_delete.append(fid)
            continue
        simplified = geom.SimplifyPreserveTopology(_SIMPLIFY_M)
        if simplified is None or simplified.IsEmpty():
            to_delete.append(fid)
            continue
        if float(simplified.GetArea()) < _MIN_AREA_M2:
            to_delete.append(fid)
            continue
        feature.SetGeometry(simplified)
        feature.SetField("code", code)
        layer.SetFeature(feature)
    for fid in to_delete:
        layer.DeleteFeature(fid)

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
    try:
        from osgeo import ogr
    except ImportError:
        ogr = None
    if ogr is not None:
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
) -> list[OomObjectPart]:
    shp = work_dir / "vegetation" / "vegetation.shp"
    if not shp.is_file():
        return []

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
        geom_parts, _ = _wkb_parts(wkb)
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
