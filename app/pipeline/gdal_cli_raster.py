"""Raster I/O přes GDAL CLI, když job Python nemá ``osgeo``.

Na Windows/QGIS stroji (``C:\\QGIS\\bin``) systémový Python typicky neumí
``import osgeo``, ale ``gdalinfo`` / ``gdal_translate`` / ``gdal_polygonize``
ano. CHM vegetace a srázy z DEM tak nepadnou na tichý ImportError.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import numpy as np

from app.pipeline.prepare_lidar import find_tool, run_cmd


def _gdalinfo_json(path: Path, *, log=None) -> dict:
    tool = find_tool("gdalinfo")
    result = run_cmd([tool, "-json", str(path)], log=log)
    return json.loads(result.stdout or "{}")


def read_float32_geotiff(
    path: Path, *, log=None
) -> tuple[np.ndarray, tuple[float, float, float, float, float, float], float | None]:
    """Vrátí (array HxW float32, geotransform 6-tuple, nodata)."""
    path = Path(path)
    try:
        from osgeo import gdal

        gdal.UseExceptions()
        ds = gdal.Open(str(path))
        if ds is None:
            raise RuntimeError(f"Nelze otevřít {path.name}")
        band = ds.GetRasterBand(1)
        arr = band.ReadAsArray()
        if arr is None:
            raise RuntimeError(f"Prázdný raster {path.name}")
        nodata = band.GetNoDataValue()
        gt = ds.GetGeoTransform()
        ds = None
        return np.asarray(arr, dtype=np.float32), tuple(float(x) for x in gt), nodata
    except ImportError:
        pass

    meta = _gdalinfo_json(path, log=log)
    size = meta.get("size") or []
    if len(size) != 2:
        raise RuntimeError(f"gdalinfo: neplatná velikost {path.name}")
    width, height = int(size[0]), int(size[1])
    gt_raw = meta.get("geoTransform") or [0, 1, 0, 0, 0, -1]
    gt = tuple(float(x) for x in gt_raw[:6])
    bands = meta.get("bands") or [{}]
    nodata = bands[0].get("noDataValue")
    if nodata is not None:
        nodata = float(nodata)

    tmp = Path(tempfile.mkdtemp(prefix="podkladarna_envi_"))
    try:
        envi = tmp / "band.bil"
        translate = find_tool("gdal_translate")
        run_cmd(
            [translate, "-of", "ENVI", "-ot", "Float32", str(path), str(envi)],
            log=log,
        )
        if not envi.is_file():
            # Některé buildy pojmenují výstup bez přípony.
            candidates = [p for p in tmp.iterdir() if p.is_file() and p.suffix.lower() != ".hdr"]
            if not candidates:
                raise RuntimeError(f"gdal_translate ENVI selhal pro {path.name}")
            envi = candidates[0]
        arr = np.fromfile(envi, dtype="<f4")
        if arr.size != width * height:
            raise RuntimeError(
                f"ENVI velikost {arr.size} ≠ {width}×{height} ({path.name})"
            )
        return arr.reshape((height, width)), gt, nodata
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def write_uint8_geotiff(
    path: Path,
    arr: np.ndarray,
    gt: tuple[float, float, float, float, float, float],
    *,
    nodata: int = 0,
    log=None,
) -> Path:
    """Zapíše Byte GeoTIFF (ENVI → gdal_translate), bez osgeo."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.asarray(arr, dtype=np.uint8)
    if data.ndim != 2:
        raise ValueError("write_uint8_geotiff očekává 2D pole")
    height, width = data.shape

    try:
        from osgeo import gdal

        gdal.UseExceptions()
        driver = gdal.GetDriverByName("GTiff")
        ds = driver.Create(str(path), width, height, 1, gdal.GDT_Byte)
        ds.SetGeoTransform(gt)
        band = ds.GetRasterBand(1)
        band.SetNoDataValue(int(nodata))
        band.WriteArray(data)
        band.FlushCache()
        ds = None
        return path
    except ImportError:
        pass

    tmp = Path(tempfile.mkdtemp(prefix="podkladarna_cls_"))
    try:
        bil = tmp / "cls.bil"
        data.tofile(bil)
        px = abs(float(gt[1])) or 1.0
        py = abs(float(gt[5])) or 1.0
        hdr = tmp / "cls.hdr"
        hdr.write_text(
            (
                "ENVI\n"
                f"samples = {width}\n"
                f"lines = {height}\n"
                "bands = 1\n"
                "header offset = 0\n"
                "file type = ENVI Standard\n"
                "data type = 1\n"
                "interleave = bsq\n"
                "byte order = 0\n"
                f"map info = {{Arbitrary, 1.0000, 1.0000, {gt[0]}, {gt[3]}, "
                f"{px}, {py}, 0, N}}\n"
            ),
            encoding="ascii",
        )
        translate = find_tool("gdal_translate")
        run_cmd(
            [
                translate,
                "-of",
                "GTiff",
                "-a_nodata",
                str(int(nodata)),
                str(bil),
                str(path),
            ],
            log=log,
        )
        if not path.is_file() or path.stat().st_size < 64:
            raise RuntimeError(f"Zápis GeoTIFF selhal: {path.name}")
        return path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def polygonize_byte_raster(
    raster: Path,
    dest_shp: Path,
    *,
    layer_name: str = "vegetation",
    field_name: str = "cls",
    log=None,
) -> Path:
    """``gdal_polygonize`` → ESRI Shapefile (cls pole = DN)."""
    dest_shp = Path(dest_shp)
    dest_shp.parent.mkdir(parents=True, exist_ok=True)
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        dest_shp.with_suffix(suffix).unlink(missing_ok=True)

    try:
        tool = find_tool("gdal_polygonize")
    except RuntimeError:
        tool = find_tool("gdal_polygonize.py")

    run_cmd(
        [
            tool,
            str(raster),
            "-f",
            "ESRI Shapefile",
            str(dest_shp),
            layer_name,
            field_name,
        ],
        log=log,
    )
    if not dest_shp.is_file():
        raise RuntimeError(f"gdal_polygonize nevytvořil {dest_shp.name}")
    return dest_shp
