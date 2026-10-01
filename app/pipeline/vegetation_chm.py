"""Hrubá vegetace z CHM (DMP − DMR) – vlna 1 bez KP vegetation.png.

Prahy výšky: open (401) vs hustá zeleň (406/408/410) vs bílý les (bez
výplně). Bílý les má vyšší práh, ať nepřetéká do luk. ZABAGED/OSM odečet
401 řeší ``open_land_subtract`` při skládání OOM (stejné vegetation.shp).
Orto zůstává QA-only – sem nepatří.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.pipeline.crs_5514 import write_prj
from app.pipeline.dem_prep import DEM_DIR_NAME
from app.pipeline.job_grid import JobGrid

# --- Prahy CHM (metry nad terénem). Jemnější oddělení dense green vs white. ---
# Open: nízko → žlutá 401 (louky). Vyšší práh = méně „bílého“ bleed do luk.
CHM_OPEN_MAX_M = 1.2
# Světlá zeleň 406
CHM_GREEN_LIGHT_MAX_M = 4.0
# Střed 408
CHM_GREEN_MID_MAX_M = 9.0
# Hustá 410 – nad tím bílý (průchodný) les bez výplně.
CHM_GREEN_DENSE_MAX_M = 14.0

_MIN_AREA_M2 = 12.0
_SIMPLIFY_M = 1.5

# Stejné kódy jako vegetation_gdal (OOM build_vegetation_parts).
_CLASS_TO_CODE = {
    1: "401",
    2: "406",
    3: "408",
    4: "410",
}


@dataclass(frozen=True)
class ChmVegeThresholds:
    open_max_m: float = CHM_OPEN_MAX_M
    green_light_max_m: float = CHM_GREEN_LIGHT_MAX_M
    green_mid_max_m: float = CHM_GREEN_MID_MAX_M
    green_dense_max_m: float = CHM_GREEN_DENSE_MAX_M

    def classify_height(self, h: float) -> int:
        """0 = pozadí / bílý les, 1 open, 2–4 zeleně."""
        if h != h:  # NaN
            return 0
        if h < self.open_max_m:
            return 1
        if h < self.green_light_max_m:
            return 2
        if h < self.green_mid_max_m:
            return 3
        if h < self.green_dense_max_m:
            return 4
        return 0  # tall canopy → white forest (no fill)


DEFAULT_THRESHOLDS = ChmVegeThresholds()


def chm_path(work_dir: Path) -> Path | None:
    path = Path(work_dir) / DEM_DIR_NAME / "chm.tif"
    if path.is_file() and path.stat().st_size > 500:
        return path
    return None


def classify_chm_array(chm, nodata, thresholds: ChmVegeThresholds = DEFAULT_THRESHOLDS):
    """Numpy uint8 třídy z CHM výšek."""
    import numpy as np

    arr = np.asarray(chm, dtype=np.float32)
    out = np.zeros(arr.shape, dtype=np.uint8)
    valid = np.ones(arr.shape, dtype=bool)
    if nodata is not None:
        valid &= arr != float(nodata)
    valid &= np.isfinite(arr)

    open_m = thresholds.open_max_m
    g1 = thresholds.green_light_max_m
    g2 = thresholds.green_mid_max_m
    g3 = thresholds.green_dense_max_m

    out[valid & (arr < open_m)] = 1
    out[valid & (arr >= open_m) & (arr < g1)] = 2
    out[valid & (arr >= g1) & (arr < g2)] = 3
    out[valid & (arr >= g2) & (arr < g3)] = 4
    # >= g3 zůstane 0 = bílý les
    return out


def write_chm_tint_png(
    classified,
    dest: Path,
    *,
    opacity: int = 140,
) -> Path | None:
    """RGBA tint pro volitelný preview overlay (zelené třídy)."""
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return None
    h, w = classified.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    # 401 žlutá slabě
    m = classified == 1
    rgba[m] = (255, 220, 120, max(40, opacity // 2))
    m = classified == 2
    rgba[m] = (160, 230, 160, opacity)
    m = classified == 3
    rgba[m] = (100, 200, 100, opacity)
    m = classified == 4
    rgba[m] = (40, 140, 40, opacity)
    dest.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(dest, format="PNG")
    return dest if dest.is_file() else None


def generate_vegetation_from_chm(
    chm_tif: Path,
    dest_shp: Path,
    *,
    thresholds: ChmVegeThresholds = DEFAULT_THRESHOLDS,
    tint_png: Path | None = None,
    log=None,
) -> Path | None:
    """Klasifikuje CHM → polygony vegetation.shp (cls, code)."""
    try:
        from osgeo import gdal, ogr
        import numpy as np
    except ImportError:
        if log:
            log("CHM vegetace: osgeo/GDAL není k dispozici – přeskočeno")
        return None

    gdal.UseExceptions()
    ogr.UseExceptions()

    src = gdal.Open(str(chm_tif))
    if src is None:
        if log:
            log(f"CHM vegetace: nelze otevřít {chm_tif.name}")
        return None

    band = src.GetRasterBand(1)
    arr = band.ReadAsArray()
    if arr is None:
        return None
    nodata = band.GetNoDataValue()
    classified = classify_chm_array(arr, nodata, thresholds)
    gt = src.GetGeoTransform()
    width, height = src.RasterXSize, src.RasterYSize

    if tint_png is not None:
        try:
            write_chm_tint_png(classified, tint_png)
        except Exception as exc:
            if log:
                log(f"CHM tint PNG: přeskočeno ({exc})")

    dest_shp.parent.mkdir(parents=True, exist_ok=True)
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        dest_shp.with_suffix(suffix).unlink(missing_ok=True)

    mem_drv = gdal.GetDriverByName("MEM")
    class_ds = mem_drv.Create("", width, height, 1, gdal.GDT_Byte)
    class_ds.SetGeoTransform(gt)
    class_band = class_ds.GetRasterBand(1)
    class_band.WriteArray(np.asarray(classified, dtype=np.uint8))
    class_band.SetNoDataValue(0)

    driver = ogr.GetDriverByName("ESRI Shapefile")
    out_ds = driver.CreateDataSource(str(dest_shp))
    layer = out_ds.CreateLayer("vegetation", None, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("cls", ogr.OFTInteger))
    layer.CreateField(ogr.FieldDefn("code", ogr.OFTString))

    gdal.Polygonize(class_band, class_band, layer, 0, [], callback=None)

    kept: list[tuple[int, str, object]] = []
    to_delete: list[int] = []
    for feature in layer:
        cls = int(feature.GetField("cls") or 0)
        code = _CLASS_TO_CODE.get(cls)
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
        if float(simplified.GetArea()) < _MIN_AREA_M2:
            continue
        kept.append((cls, code, simplified.Clone()))
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
        log(
            f"CHM vegetace: {n_kept} polygonů → {dest_shp.name} "
            f"(open<{thresholds.open_max_m:g} m, "
            f"white≥{thresholds.green_dense_max_m:g} m)"
        )
    return dest_shp


def generate_job_vegetation_chm(
    work_dir: Path,
    *,
    thresholds: ChmVegeThresholds = DEFAULT_THRESHOLDS,
    log=None,
) -> Path | None:
    """Job fáze: CHM → work/vegetation/vegetation.shp (+ volitelný tint)."""
    work_dir = Path(work_dir)
    src = chm_path(work_dir)
    if src is None:
        if log:
            log("CHM vegetace: chybí work/dem/chm.tif – přeskočeno")
        return None
    # job_grid není nutný pro polygonize (geotransform z CHM), ale loguj mismatch.
    grid = JobGrid.load(work_dir)
    if grid is None and log:
        log("CHM vegetace: varování – chybí job_grid (CHM geotransform stačí)")

    dest = work_dir / "vegetation" / "vegetation.shp"
    tint = work_dir / "vegetation" / "chm_tint.png"
    if log:
        log("=== Fáze: vegetace z CHM (bez KP) ===")
    return generate_vegetation_from_chm(
        src, dest, thresholds=thresholds, tint_png=tint, log=log
    )
