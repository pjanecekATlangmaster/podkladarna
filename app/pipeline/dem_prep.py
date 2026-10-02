"""Sdílený DEM / DSM / CHM prep z DMR 5G + DMP OK (PDAL + GDAL).

Jedna cesta pro shade, vrstevnice a budoucí vegetaci/srázy – výstup do
``work/dem/``. CHM = max(DSM_raw − DEM, 0), DSM na mřížce DEM; buňka bez
DMP bodu = 0 (louka), ne fillnodata. Orto do této větve nepatří.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.download_cache import (
    file_fingerprint,
    persist_surfaces,
    try_restore_surfaces,
)
from app.pipeline.reference_layers import _fill_dem_nodata, _pdal_dem_from_laz
from app.pipeline.prepare_lidar import find_tool, log_step, run_cmd

DEM_DIR_NAME = "dem"
DEM_META_NAME = "dem_meta.json"
DEFAULT_SURFACE_RESOLUTION_M = 1.0


@dataclass
class DemPrepResult:
    dem_raw: Path
    dem_filled: Path
    dsm_raw: Path | None
    dsm_filled: Path | None
    chm: Path | None
    bounds: tuple[float, float, float, float]
    resolution_m: float
    ground_laz: str
    surface_laz: str | None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["bounds"] = list(self.bounds)
        for key in ("dem_raw", "dem_filled", "dsm_raw", "dsm_filled", "chm"):
            val = data.get(key)
            data[key] = str(val) if val else None
        return data

    def write_meta(self, dem_dir: Path) -> Path:
        path = dem_dir / DEM_META_NAME
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path


def dem_work_dir(work_dir: Path) -> Path:
    path = work_dir / DEM_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pick_ground_laz(lidar_dir: Path) -> Path | None:
    for name in (
        "ground_merged.laz",
        "dmr_ground_0.laz",
        "merged_crop.laz",
        "merged_crop_retry.laz",
        "merged.laz",
    ):
        path = lidar_dir / name
        if path.is_file() and path.stat().st_size > 1000:
            return path
    for path in sorted(lidar_dir.glob("dmr_ground_*.laz")):
        if path.is_file() and path.stat().st_size > 1000:
            return path
    return None


def _pick_surface_laz(lidar_dir: Path) -> Path | None:
    for name in ("veg_merged.laz", "dmp_veg_0.laz"):
        path = lidar_dir / name
        if path.is_file() and path.stat().st_size > 1000:
            return path
    for path in sorted(lidar_dir.glob("dmp_veg_*.laz")):
        if path.is_file() and path.stat().st_size > 1000:
            return path
    return None


def _raster_grid(path: Path, *, log=None) -> tuple[float, float, int, int] | None:
    """(origin_x, origin_y dolní okraj, width, height) existujícího rastru."""
    try:
        from app.pipeline.gdal_cli_raster import _gdalinfo_json

        meta = _gdalinfo_json(path, log=log)
        width, height = (int(v) for v in meta["size"])
        gt = [float(v) for v in meta["geoTransform"]]
    except Exception:
        return None
    return gt[0], gt[3] + gt[5] * height, width, height


# DMP OK ořez bere jen třídy 5/6 – buňka bez bodu = otevřený terén, ne díra.
# Výška 0 místo fillnodata (ta roztáhla koruny přes louky → „všechno les“).
_CHM_CALC = "where(B<-9000,-9999,where(A<-9000,0,maximum(A-B,0)))"


def _gdal_chm(dem: Path, dsm: Path, dest: Path, *, log=None) -> Path:
    """CHM = max(DSM − DEM, 0); DSM NoData (žádný vegetační bod) → 0.

    ``dsm`` má být **raw** DSM (bez fillnodata) na stejné mřížce jako ``dem``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    log_step(log, "Počítám CHM (výška porostu = povrch minus terén)")
    for name in ("gdal_calc", "gdal_calc.py"):
        try:
            tool = find_tool(name)
        except RuntimeError:
            continue
        run_cmd(
            [
                tool,
                "-A",
                str(dsm),
                "-B",
                str(dem),
                "--outfile",
                str(dest),
                "--calc",
                _CHM_CALC,
                "--hideNoData",
                "--type",
                "Float32",
                "--NoDataValue",
                "-9999",
                "--overwrite",
            ],
            log=log,
        )
        if dest.is_file() and dest.stat().st_size >= 500:
            return dest
        raise RuntimeError(f"gdal_calc nevytvořil použitelný CHM ({dest.name})")

    try:
        from osgeo import gdal
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "CHM: chybí gdal_calc i osgeo – nastavte QGIS (C:\\QGIS) / OSGeo4W"
        ) from exc

    gdal.UseExceptions()
    dem_ds = gdal.Open(str(dem))
    dsm_ds = gdal.Open(str(dsm))
    if dem_ds is None or dsm_ds is None:
        raise RuntimeError("CHM: nelze otevřít DEM/DSM")
    dem_band = dem_ds.GetRasterBand(1)
    dsm_band = dsm_ds.GetRasterBand(1)
    dem_arr = dem_band.ReadAsArray().astype("float32")
    dsm_arr = dsm_band.ReadAsArray().astype("float32")
    dem_nd = dem_band.GetNoDataValue()
    dsm_nd = dsm_band.GetNoDataValue()
    chm = np.maximum(dsm_arr - dem_arr, 0.0)
    if dsm_nd is not None:
        chm = np.where(dsm_arr == dsm_nd, 0.0, chm)
    if dem_nd is not None:
        chm = np.where(dem_arr == dem_nd, -9999.0, chm)
    chm = chm.astype("float32")
    driver = gdal.GetDriverByName("GTiff")
    out = driver.Create(
        str(dest), dem_ds.RasterXSize, dem_ds.RasterYSize, 1, gdal.GDT_Float32
    )
    out.SetGeoTransform(dem_ds.GetGeoTransform())
    out.SetProjection(dem_ds.GetProjection())
    band = out.GetRasterBand(1)
    band.SetNoDataValue(-9999)
    band.WriteArray(chm)
    band.FlushCache()
    out = None
    dem_ds = None
    dsm_ds = None
    return dest


def _result_from_dem_dir(
    dem_dir: Path,
    *,
    bounds: tuple[float, float, float, float],
    resolution_m: float,
    ground_name: str,
    surface_name: str | None,
) -> DemPrepResult:
    def _opt(name: str) -> Path | None:
        path = dem_dir / name
        return path if path.is_file() and path.stat().st_size >= 500 else None

    dem_filled = dem_dir / "dem_filled.tif"
    dem_raw = dem_dir / "dem_raw.tif"
    if not dem_raw.is_file():
        dem_raw = dem_filled
    return DemPrepResult(
        dem_raw=dem_raw,
        dem_filled=dem_filled,
        dsm_raw=_opt("dsm_raw.tif"),
        dsm_filled=_opt("dsm_filled.tif"),
        chm=_opt("chm.tif"),
        bounds=bounds,
        resolution_m=float(resolution_m),
        ground_laz=ground_name,
        surface_laz=surface_name,
    )


def prepare_job_surfaces(
    work_dir: Path,
    bounds: tuple[float, float, float, float],
    *,
    resolution_m: float = DEFAULT_SURFACE_RESOLUTION_M,
    ground_laz: Path | None = None,
    surface_laz: Path | None = None,
    cache_dir: Path | None = None,
    force_refresh: bool = False,
    log=None,
) -> DemPrepResult:
    """Vytvoří filled DEM (+ DSM/CHM pokud je DMP LAZ) do ``work/dem/``.

    Při ``cache_dir`` (AOI surfaces) znovupoužije DEM/DSM/CHM, pokud sedí
    fingerprint ground/surface LAZ — ekvidistance/lavičky cache neinvalidují.
    """
    lidar_dir = work_dir / "lidar"
    ground = ground_laz or _pick_ground_laz(lidar_dir)
    if ground is None or not ground.is_file():
        raise FileNotFoundError(
            "DEM prep: chybí ground LAZ (ground_merged / dmr_ground_* / merged_*)"
        )
    surface = surface_laz if surface_laz is not None else _pick_surface_laz(lidar_dir)

    dem_dir = dem_work_dir(work_dir)
    ground_fp = file_fingerprint(ground)
    surface_fp = file_fingerprint(surface) if surface is not None else None

    if cache_dir is not None and try_restore_surfaces(
        cache_dir,
        dem_dir,
        bounds=bounds,
        resolution_m=float(resolution_m),
        ground_fp=ground_fp,
        surface_fp=surface_fp,
        force=force_refresh,
        log=log,
    ):
        result = _result_from_dem_dir(
            dem_dir,
            bounds=bounds,
            resolution_m=float(resolution_m),
            ground_name=ground.name,
            surface_name=surface.name if surface else None,
        )
        result.write_meta(dem_dir)
        return result

    dem_raw = dem_dir / "dem_raw.tif"
    dem_filled = dem_dir / "dem_filled.tif"
    if log:
        log(
            f"DEM prep: ground={ground.name}, "
            f"surface={surface.name if surface else '—'}, "
            f"res={resolution_m:g} m"
        )
    log_step(log, "Připravuji DEM z DMR5G (terén pro vrstevnice a srázy)")
    _pdal_dem_from_laz(
        ground, bounds, dem_raw, resolution_m=float(resolution_m), log=log
    )
    if not dem_raw.is_file() or dem_raw.stat().st_size < 500:
        raise RuntimeError(f"PDAL nevytvořil DEM ({dem_raw.name})")
    _fill_dem_nodata(dem_raw, dem_filled, log=log)

    dsm_raw: Path | None = None
    chm: Path | None = None
    if surface is not None and surface.is_file():
        dsm_raw = dem_dir / "dsm_raw.tif"
        dsm_kwargs = {}
        dem_grid = _raster_grid(dem_raw, log=log)
        if dem_grid is not None:
            dsm_kwargs["grid"] = dem_grid
        log_step(log, "Připravuji DSM z DMP (povrch pro výšku vegetace)")
        _pdal_dem_from_laz(
            surface,
            bounds,
            dsm_raw,
            resolution_m=float(resolution_m),
            log=log,
            **dsm_kwargs,
        )
        if dsm_raw.is_file() and dsm_raw.stat().st_size >= 500:
            # Úmyslně bez dsm_filled / fillnodata: CHM bere raw DSM (buňka bez
            # DMP bodu = 0 / louka). Fill by roztáhl koruny přes otevřený terén
            # a nic jiného dsm_filled nečte.
            try:
                chm = _gdal_chm(dem_filled, dsm_raw, dem_dir / "chm.tif", log=log)
            except Exception as exc:
                if log:
                    log(f"CHM: přeskočeno ({exc})")
                chm = None
        else:
            if log:
                log("DSM: PDAL nevytvořil použitelný raster – CHM přeskočeno")
            dsm_raw = None

    # dsm_filled jen z případné staré AOI cache; nově se negeneruje.
    dsm_filled_path = dem_dir / "dsm_filled.tif"
    dsm_filled = (
        dsm_filled_path
        if dsm_filled_path.is_file() and dsm_filled_path.stat().st_size >= 500
        else None
    )
    result = DemPrepResult(
        dem_raw=dem_raw,
        dem_filled=dem_filled,
        dsm_raw=dsm_raw,
        dsm_filled=dsm_filled,
        chm=chm,
        bounds=bounds,
        resolution_m=float(resolution_m),
        ground_laz=ground.name,
        surface_laz=surface.name if surface else None,
    )
    result.write_meta(dem_dir)
    if cache_dir is not None:
        persist_surfaces(
            cache_dir,
            dem_dir,
            bounds=bounds,
            resolution_m=float(resolution_m),
            ground_fp=ground_fp,
            surface_fp=surface_fp,
            log=log,
        )
    if log:
        parts = [f"DEM={dem_filled.name}"]
        if dsm_raw:
            parts.append(f"DSM={dsm_raw.name}")
        if chm:
            parts.append(f"CHM={chm.name}")
        log("DEM prep hotovo: " + ", ".join(parts))
    return result


def load_dem_meta(work_dir: Path) -> dict | None:
    path = work_dir / DEM_DIR_NAME / DEM_META_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
