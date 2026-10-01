"""Záložní vegetace z CHM (DMP − DMR), když nejde ``vegetation_density``.

Primární cesta bez KP je ``vegetation_density`` (hustota LiDAR odrazů jako KP
``makevege``); CHM je jen výškový proxy pro prostředí bez ``laspy``.

Prahy (metry nad terénem) kalibrované proti KP ``vegetation.png`` na dvou AOI
(po opravě CHM v ``dem_prep``: DSM na mřížce DEM, buňka bez DMP bodu = 0):

* ``open_max_m`` **1.0** – pod tím žlutá 401 (KP ``yellowheight=0.9``)
* ``green_light_max_m`` **2.0** / ``green_mid_max_m`` **4.0** /
  ``green_dense_max_m`` **6.0** – 406/408/410 = keře a mlází v pásu, kde KP
  počítá zelené zóny (1–5.5 m)
* ≥ 6 m – bílý les (vzrostlá koruna; KP ji jako zeleň nebere)

Dřívější prahy (open < 4 m, bílá ≥ 12 m) kompenzovaly chybu CHM:
``fillnodata`` roztahoval koruny přes louky. Výsledek: střídavě „vše les“.

``open_land_subtract`` (ZABAGED/OSM odečet od 401) platí **jen** u KP cesty
v ``package_oom`` — bez KP se 401 bere výhradně z tohoto SHP.
Orto = QA-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.pipeline.crs_5514 import write_prj
from app.pipeline.dem_prep import DEM_DIR_NAME
from app.pipeline.job_grid import JobGrid

# --- Prahy CHM (metry nad terénem), kalibrace vs. KP – viz docstring. ---
CHM_OPEN_MAX_M = 1.0
CHM_GREEN_LIGHT_MAX_M = 2.0
CHM_GREEN_MID_MAX_M = 4.0
# Hustá 410; nad tím bílý (průchodný) les.
CHM_GREEN_DENSE_MAX_M = 6.0
# Na styku s loukou musí koruna být ještě vyšší, jinak zůstane 410.
WHITE_EDGE_STRICT_M = 20.0
# Morfologické otevření bílé masky (eroze→dilatace), zúží tenké výběžky do luk.
WHITE_MORPH_OPEN_ITERS = 1
# Přitažení nízkého scrubu do open – po opravě CHM bez přínosu, default vypnuto.
OPEN_REINFORCE_ITERS = 0
# Median filtr klasifikace (liché); KP medianboxsize=6 → okno 7 px (radius 3).
CHM_MEDIAN_SIZE = 7

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
    white_edge_strict_m: float = WHITE_EDGE_STRICT_M
    white_morph_iters: int = WHITE_MORPH_OPEN_ITERS
    open_reinforce_iters: int = OPEN_REINFORCE_ITERS
    median_size: int = CHM_MEDIAN_SIZE

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


def _median_filter_uint8(arr, size: int):
    """Median filtr pro uint8 klasifikaci (KP-like ``medianboxsize``)."""
    import numpy as np

    size = int(size)
    if size < 3 or size % 2 == 0:
        return np.asarray(arr, dtype=np.uint8)
    a = np.asarray(arr, dtype=np.uint8)
    pad = size // 2
    padded = np.pad(a, pad, mode="edge")
    h, w = a.shape
    # sliding window via stride tricks when available; else pure python loops on small AOI
    try:
        from numpy.lib.stride_tricks import sliding_window_view

        windows = sliding_window_view(padded, (size, size))
        return np.median(windows, axis=(-2, -1)).astype(np.uint8)
    except Exception:
        out = a.copy()
        for i in range(h):
            for j in range(w):
                out[i, j] = int(
                    np.median(padded[i : i + size, j : j + size])
                )
        return out


def chm_path(work_dir: Path) -> Path | None:
    path = Path(work_dir) / DEM_DIR_NAME / "chm.tif"
    if path.is_file() and path.stat().st_size > 500:
        return path
    return None


def _binary_erode(mask):
    """4-sousední eroze (True jen když self + N/S/E/W jsou True)."""
    import numpy as np

    m = np.asarray(mask, dtype=bool)
    out = m.copy()
    out[1:, :] &= m[:-1, :]
    out[:-1, :] &= m[1:, :]
    out[:, 1:] &= m[:, :-1]
    out[:, :-1] &= m[:, 1:]
    return out


def _binary_dilate(mask):
    """4-sousední dilatace."""
    import numpy as np

    m = np.asarray(mask, dtype=bool)
    out = m.copy()
    out[1:, :] |= m[:-1, :]
    out[:-1, :] |= m[1:, :]
    out[:, 1:] |= m[:, :-1]
    out[:, :-1] |= m[:, 1:]
    return out


def _binary_open(mask, iterations: int = 1):
    out = mask
    for _ in range(max(0, int(iterations))):
        out = _binary_dilate(_binary_erode(out))
    return out


def _touches_open(classified, open_cls: int = 1):
    """True kde soused (4) je open land."""
    import numpy as np

    open_m = np.asarray(classified, dtype=np.uint8) == open_cls
    near = np.zeros(open_m.shape, dtype=bool)
    near[1:, :] |= open_m[:-1, :]
    near[:-1, :] |= open_m[1:, :]
    near[:, 1:] |= open_m[:, :-1]
    near[:, :-1] |= open_m[:, 1:]
    return near


def _open_neighbor_fraction(open_mask):
    """Podíl open sousedů v 3×3 (včetně self) – KP yellow-threshold analog."""
    import numpy as np

    m = np.asarray(open_mask, dtype=np.float32)
    padded = np.pad(m, 1, mode="edge")
    acc = np.zeros_like(m, dtype=np.float32)
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            acc += padded[1 + di : 1 + di + m.shape[0], 1 + dj : 1 + dj + m.shape[1]]
    return acc / 9.0


def reinforce_open_land(
    classified,
    heights,
    *,
    thresholds: ChmVegeThresholds = DEFAULT_THRESHOLDS,
    valid=None,
):
    """Vtáhne nízký scrub (406, případně nízké 408) do 401, když okolí je louka.

    Proxy KP ``yellowthresold``: buňka s převážně „nízkým“ okolím zůstane
    otevřená i při mírně vyšší CHM (tráva / šum DMP−DMR), místo falešné zeleně.
    """
    import numpy as np

    out = np.asarray(classified, dtype=np.uint8).copy()
    h = np.asarray(heights, dtype=np.float32)
    if valid is None:
        valid = np.isfinite(h)
    else:
        valid = np.asarray(valid, dtype=bool)

    iters = max(0, int(thresholds.open_reinforce_iters))
    # Soft strop: scrub až po mid band, ale jen když je v louce.
    soft_max = thresholds.green_mid_max_m
    for _ in range(iters):
        open_m = out == 1
        frac = _open_neighbor_fraction(open_m)
        # Světlá zeleň v louce → open; nízké mid green jen při silné většině open.
        pull_light = (
            valid
            & (out == 2)
            & (h < soft_max)
            & (frac >= 0.45)
        )
        pull_mid = (
            valid
            & (out == 3)
            & (h < thresholds.green_light_max_m)
            & (frac >= 0.6)
        )
        if not (np.any(pull_light) or np.any(pull_mid)):
            break
        out[pull_light | pull_mid] = 1
    return out


def cleanup_white_forest(
    classified,
    heights,
    *,
    thresholds: ChmVegeThresholds = DEFAULT_THRESHOLDS,
    valid=None,
):
    """Zúží bílý les: morfologické otevření + přísnější hranice s loukou.

    Pixely, které přijdou o bílou, dostanou hustou zeleň 410 (ne open),
    ať se do luk nevrací falešná 401 z okraje korun.
    """
    import numpy as np

    out = np.asarray(classified, dtype=np.uint8).copy()
    h = np.asarray(heights, dtype=np.float32)
    if valid is None:
        valid = np.isfinite(h)
    else:
        valid = np.asarray(valid, dtype=bool)

    white = valid & (out == 0) & (h >= thresholds.green_dense_max_m)
    if not np.any(white):
        return out

    cleaned = _binary_open(white, iterations=thresholds.white_morph_iters)
    # Na styku s open: jen opravdu vysoká koruna zůstane bílá.
    open_touch = _touches_open(out, open_cls=1)
    strict = thresholds.white_edge_strict_m
    keep = cleaned & (~open_touch | (h >= strict))

    demoted = white & ~keep
    out[demoted] = 4  # dense green instead of white bleed
    out[keep] = 0
    return out


def classify_chm_array(chm, nodata, thresholds: ChmVegeThresholds = DEFAULT_THRESHOLDS):
    """Numpy uint8 třídy z CHM výšek (+ median + open reinforce + white cleanup)."""
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
    # Nodata zůstane 0; median by je „rozmazal“ do luk → obnovit po filtru.
    if thresholds.median_size >= 3:
        smoothed = _median_filter_uint8(out, thresholds.median_size)
        smoothed[~valid] = 0
        out = smoothed
    out = reinforce_open_land(out, arr, thresholds=thresholds, valid=valid)
    return cleanup_white_forest(out, arr, thresholds=thresholds, valid=valid)


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


def _simplify_vegetation_shp(
    dest_shp: Path,
    *,
    log=None,
) -> int:
    """Filtr + simplify (pyogrio/shapely → pyshp); fallback osgeo.ogr."""
    from app.pipeline.crs_5514 import write_prj

    kept: list[tuple[int, str, object]] = []

    try:
        from shapely import from_wkb
        from app.pipeline.oom_import import _pyogrio_layer_rows

        for props, wkb in _pyogrio_layer_rows(dest_shp):
            cls = int(props.get("cls") or 0)
            code = _CLASS_TO_CODE.get(cls)
            if not code or not wkb:
                continue
            geom = from_wkb(bytes(wkb))
            if geom is None or geom.is_empty:
                continue
            simplified = geom.simplify(_SIMPLIFY_M, preserve_topology=True)
            if simplified is None or simplified.is_empty:
                continue
            if float(simplified.area) < _MIN_AREA_M2:
                continue
            kept.append((cls, code, simplified))
    except Exception as exc:
        # osgeo in-place rewrite
        try:
            from osgeo import ogr

            ogr.UseExceptions()
            driver = ogr.GetDriverByName("ESRI Shapefile")
            ds = driver.Open(str(dest_shp), 1)
            if ds is None:
                return 0
            layer = ds.GetLayer(0)
            defn = layer.GetLayerDefn()
            if defn.GetFieldIndex("code") < 0:
                layer.CreateField(ogr.FieldDefn("code", ogr.OFTString))
            local_kept: list[tuple[int, str, object]] = []
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
                local_kept.append((cls, code, simplified.Clone()))
            for fid in to_delete:
                layer.DeleteFeature(fid)
            for cls, code, geom in local_kept:
                feat = ogr.Feature(layer.GetLayerDefn())
                feat.SetField("cls", cls)
                feat.SetField("code", code)
                feat.SetGeometry(geom)
                layer.CreateFeature(feat)
                feat = None
            n_kept = layer.GetFeatureCount()
            ds = None
            write_prj(dest_shp)
            return int(n_kept)
        except ImportError:
            if log:
                log(f"CHM vegetace: simplify přeskočen ({exc})")
            write_prj(dest_shp)
            return -1

    import shapefile

    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        dest_shp.with_suffix(suffix).unlink(missing_ok=True)

    writer = shapefile.Writer(str(dest_shp), shapeType=shapefile.POLYGON)
    writer.field("cls", "N", size=10)
    writer.field("code", "C", size=8)
    for cls, code, geom in kept:
        # Exterior (+ holes) as pyshp parts.
        if geom.geom_type == "Polygon":
            polys = [geom]
        elif geom.geom_type == "MultiPolygon":
            polys = list(geom.geoms)
        else:
            continue
        for poly in polys:
            parts = [list(poly.exterior.coords)]
            for ring in poly.interiors:
                parts.append(list(ring.coords))
            writer.poly(parts)
            writer.record(cls, code)
    writer.close()
    write_prj(dest_shp)
    cpg = dest_shp.with_suffix(".cpg")
    if not cpg.is_file():
        cpg.write_text("UTF-8", encoding="ascii")
    return len(kept)


def generate_vegetation_from_chm(
    chm_tif: Path,
    dest_shp: Path,
    *,
    thresholds: ChmVegeThresholds = DEFAULT_THRESHOLDS,
    tint_png: Path | None = None,
    log=None,
) -> Path | None:
    """Klasifikuje CHM → polygony vegetation.shp (cls, code)."""
    from app.pipeline.gdal_cli_raster import read_float32_geotiff

    try:
        arr, gt, nodata = read_float32_geotiff(chm_tif, log=log)
    except Exception as exc:
        if log:
            log(f"CHM vegetace: nelze číst {chm_tif.name} ({exc})")
        return None

    classified = classify_chm_array(arr, nodata, thresholds)

    if tint_png is not None:
        try:
            write_chm_tint_png(classified, tint_png)
        except Exception as exc:
            if log:
                log(f"CHM tint PNG: přeskočeno ({exc})")

    label = (
        f"CHM open<{thresholds.open_max_m:g} m, "
        f"white>={thresholds.green_dense_max_m:g} m"
    )
    return polygonize_vegetation_classes(classified, gt, dest_shp, log=log, label=label)


def polygonize_vegetation_classes(
    classified,
    gt,
    dest_shp: Path,
    *,
    log=None,
    label: str = "",
) -> Path | None:
    """uint8 třídy (0 bílý, 1–4 = 401/406/408/410) → ``vegetation.shp`` (cls, code).

    Preferuje osgeo (Docker); na Windows bez osgeo jde přes
    ``gdal_cli_raster`` + ``gdal_polygonize`` + pyogrio/shapely.
    """
    from app.pipeline.gdal_cli_raster import (
        polygonize_byte_raster,
        write_uint8_geotiff,
    )

    height, width = classified.shape
    dest_shp.parent.mkdir(parents=True, exist_ok=True)
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        dest_shp.with_suffix(suffix).unlink(missing_ok=True)

    # Rychlá cesta: osgeo polygonize v paměti (Docker / QGIS python).
    try:
        from osgeo import gdal, ogr
        import numpy as np

        gdal.UseExceptions()
        ogr.UseExceptions()
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
        from app.pipeline.crs_5514 import write_prj

        write_prj(dest_shp)
        if log:
            log(f"Vegetace: {n_kept} polygonů → {dest_shp.name} ({label})")
        return dest_shp if dest_shp.is_file() else None
    except ImportError:
        pass

    # Windows job Python: klasifikovaný Byte TIF -> gdal_polygonize -> simplify.
    class_tif = dest_shp.parent / "_chm_class.tif"
    n_kept: int | None = None
    try:
        write_uint8_geotiff(class_tif, classified, gt, nodata=0, log=log)
        polygonize_byte_raster(
            class_tif, dest_shp, layer_name="vegetation", field_name="cls", log=log
        )
        n_kept = _simplify_vegetation_shp(dest_shp, log=log)
    except Exception as exc:
        if log:
            log(f"Vegetace: CLI polygonize selhalo ({exc})")
        return None
    finally:
        class_tif.unlink(missing_ok=True)
        class_tif.with_suffix(".tif.aux.xml").unlink(missing_ok=True)

    if log and n_kept is not None:
        log(f"Vegetace: {n_kept} polygonu -> {dest_shp.name} (CLI polygonize; {label})")
    return dest_shp if dest_shp.is_file() else None


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
