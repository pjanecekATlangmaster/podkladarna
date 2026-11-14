"""Výšky jobu z rastrů terénu (DTM) a povrchu (DSM) – zahraniční build bez LAZ.

Česko bere LiDAR (DMR 5G + DMP OK) jako mračno bodů a DEM / DSM / CHM z něj počítá ``dem_prep``. Jinde
národní agentury dávají hlavně hotové rastry (SK DMR 5.0 / DMP 1.0, NL AHN, AT BEV). Tento modul je převede
na mřížku mapy do ``work/dem/`` se stejnými jmény jako ``dem_prep`` (``dem_raw``, ``dem_filled``, ``dsm_raw``,
``dsm_filled``, ``chm`` + ``dem_meta.json``), takže vrstevnice, srázy, kupky, stínování i vegetace z CHM
běží beze změny. Vegetace z hustoty bodů (``vegetation_density``) bez LAZ přeskočí na CHM.

Zdroj se volí podle ``elevation`` země v ``configs/countries.yaml``:

- ``local:<cesta>`` – předem stažené dlaždice (GeoTIFF) ve složce ``<cesta>/dtm`` a ``<cesta>/dsm``
  (relativní cesta k ``DATA_ROOT``); pro piloty z ručních exportů (SK MAPKA ≤ 400 km²).
- další zdroje (WCS a podobně) se registrují v ``PROVIDERS``.

Rozdíl proti CHM z DMP OK: rastrový DSM má i budovy (kde je DTM pod budovou interpolovaný, budova vyjde
jako „porost“, kterou ale v mapě překryje budova) a bývá z bezlistého období (listnatý les nižší).
Kde DTM z měření chybí (AHN: budovy a voda), je CHM 0.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from app import settings
from app.pipeline.crs_5514 import CRS_WKT
from app.pipeline.dem_prep import DemPrepResult, dem_work_dir
from app.pipeline.prepare_lidar import find_tool, log_step, run_cmd

# Díry DTM (budovy, voda) jsou u rastrů větší než u DMR 5G z LAZ (-md 24 v ``_fill_dem_nodata``).
FILL_MAX_DISTANCE_PX = 250
NODATA = -9999.0



@dataclass
class SourceRasters:
    """Výsledek zdroje: rastry terénu a povrchu (GeoTIFF / VRT v libovolném CRS, s NoData).

    ``vertical_offset(lon, lat)`` (ETRS89): výška, o kterou jsou hodnoty nad národním výškovým systémem
    (elipsoidické výšky → odečte se kvazigeoid), jinak None.
    """

    dtm: Path
    dsm: Path | None = None
    vertical_offset: Callable[[float, float], float] | None = None


# Zdroj: funkce (bounds v CRS mapy, cílová složka, log) → SourceRasters.
Provider = Callable[[tuple[float, float, float, float], Path, Callable | None], SourceRasters]
PROVIDERS: dict[str, Provider] = {}


class ElevationUnavailable(RuntimeError):
    """Pro výřez nejsou výšková data (mimo pokrytí zdroje nebo zdroj ``none``)."""


def _gdal_tool(name: str) -> str:
    for candidate in (name, f"{name}.exe", f"{name}.py"):
        try:
            return find_tool(candidate)
        except RuntimeError:
            continue
    raise RuntimeError(f"GDAL nástroj '{name}' není k dispozici")


def _local_dir(spec: str) -> Path:
    raw = spec.split(":", 1)[1].strip()
    path = Path(raw)
    return path if path.is_absolute() else settings.DATA_ROOT / path


def _tiles_vrt(tiles_dir: Path, dest: Path, *, log=None) -> Path | None:
    tiles = sorted(p for p in tiles_dir.glob("*.tif") if p.is_file()) if tiles_dir.is_dir() else []
    if not tiles:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    list_file = dest.with_suffix(".txt")
    list_file.write_text("\n".join(str(p) for p in tiles) + "\n", encoding="utf-8")
    run_cmd([_gdal_tool("gdalbuildvrt"), "-input_file_list", str(list_file), str(dest)], log=log)
    return dest if dest.is_file() else None


def local_provider(spec: str) -> Provider:
    base = _local_dir(spec)

    def _fetch(bounds, out_dir: Path, log=None) -> SourceRasters:
        dtm = _tiles_vrt(base / "dtm", out_dir / "src_dtm.vrt", log=log)
        if dtm is None:
            raise ElevationUnavailable(f"chybí dlaždice terénu v {base / 'dtm'}")
        return SourceRasters(dtm, _tiles_vrt(base / "dsm", out_dir / "src_dsm.vrt", log=log))

    return _fetch


# --------------------------------------------------------------------------- NL: AHN přes PDOK WCS

AHN_WCS = "https://service.pdok.nl/rws/ahn/wcs/v1_0"
AHN_CRS = "EPSG:28992"
AHN_BUFFER_M = 20.0
AHN_MAX_SIDE_M = 6000.0  # jeden GetCoverage do ~6 km (0,5 m → 12 000 px); větší výřez po dlaždicích


def _bounds_in(bounds, crs_from_wkt: str, crs_to: str) -> tuple[float, float, float, float]:
    from pyproj import Transformer

    xmin, ymin, xmax, ymax = bounds
    t = Transformer.from_crs(crs_from_wkt, crs_to, always_xy=True)
    xs, ys = [], []
    for i in range(11):  # hrany, ne jen rohy (pootočení mezi souřadnicovými systémy)
        f = i / 10
        for x, y in ((xmin + f * (xmax - xmin), ymin), (xmin + f * (xmax - xmin), ymax),
                     (xmin, ymin + f * (ymax - ymin)), (xmax, ymin + f * (ymax - ymin))):
            px, py = t.transform(x, y)
            xs.append(px)
            ys.append(py)
    return min(xs), min(ys), max(xs), max(ys)


def _download(url: str, dest: Path, *, log=None) -> Path:
    import urllib.request

    from app.pipeline.fetch_openzu import USER_AGENT

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=300) as resp, tmp.open("wb") as fh:
        ctype = resp.headers.get("Content-Type", "")
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    if "tiff" not in ctype.lower() or tmp.stat().st_size < 500:
        detail = tmp.read_bytes()[:300].decode("utf-8", "replace")
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"WCS nevrátil GeoTIFF ({ctype}): {detail}")
    tmp.replace(dest)
    if log:
        log(f"  {dest.name}: {dest.stat().st_size / 1e6:.1f} MB")
    return dest


def tile_boxes(
    bounds: tuple[float, float, float, float], *, max_side: float, buffer: float, step: float
) -> list[tuple[float, float, float, float]]:
    """Výřez (+ ``buffer``) rozdělený na dlaždice nejvýš ``max_side``, hrany zarovnané na ``step``."""
    import math

    xmin, ymin, xmax, ymax = bounds
    xmin, ymin = math.floor(xmin - buffer), math.floor(ymin - buffer)
    xmax, ymax = math.ceil(xmax + buffer), math.ceil(ymax + buffer)
    nx = max(1, math.ceil((xmax - xmin) / max_side))
    ny = max(1, math.ceil((ymax - ymin) / max_side))
    out = []
    for i in range(nx):
        for j in range(ny):
            box = (
                xmin + i * (xmax - xmin) / nx,
                ymin + j * (ymax - ymin) / ny,
                xmin + (i + 1) * (xmax - xmin) / nx,
                ymin + (j + 1) * (ymax - ymin) / ny,
            )
            out.append(tuple(round(v / step) * step for v in box))
    return out


def ahn_coverage_urls(coverage: str, bounds_rd: tuple[float, float, float, float]) -> list[str]:
    """GetCoverage URL po dlaždicích nejvýš ``AHN_MAX_SIDE_M`` (souřadnice RD New, zarovnané na 0,5 m)."""
    return [
        f"{AHN_WCS}?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&COVERAGEID={coverage}"
        f"&FORMAT=image/tiff&SUBSET=x({x0:.1f},{x1:.1f})&SUBSET=y({y0:.1f},{y1:.1f})"
        for x0, y0, x1, y1 in tile_boxes(bounds_rd, max_side=AHN_MAX_SIDE_M, buffer=AHN_BUFFER_M, step=0.5)
    ]


def nl_ahn_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """AHN (Actueel Hoogtebestand Nederland) DTM a DSM 0,5 m z PDOK WCS, CC0 / CC BY 4.0, bez klíče."""
    bounds_rd = _bounds_in(bounds, CRS_WKT, AHN_CRS)
    out = []
    for coverage in ("dtm_05m", "dsm_05m"):
        tiles = [
            _download(url, out_dir / f"ahn_{coverage}_{k}.tif", log=log)
            for k, url in enumerate(ahn_coverage_urls(coverage, bounds_rd))
        ]
        vrt = out_dir / f"ahn_{coverage}.vrt"
        run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", AHN_CRS, str(vrt), *map(str, tiles)], log=log)
        out.append(vrt)
    return SourceRasters(out[0], out[1])


PROVIDERS["nl_ahn"] = nl_ahn_provider


# --------------------------------------------------------------------------- SK: DMR 5.0 přes INSPIRE WCS ÚGKK

# Stahovací služba INSPIRE (Elevation): DMR 5.0 (1. cyklus LLS) 1 m, EPSG:3046 (ETRS89 / UTM 34N), výšky
# elipsoidické ETRS89-h; CC BY 4.0, atribuce „ÚGKK SR“. Povrch (DMP) ani mračno služba nemá (jen export
# mapového klienta ZBGIS s odkazem e-mailem). Z cloudu (USA) nedostupné, z Česka ano.
SK_WCS = "https://inspirews.skgeodesy.sk/geoserver/el/ows"
SK_COVERAGE = "el__EL.GridCoverage"
SK_CRS = "EPSG:3046"
SK_MAX_SIDE_M = 4000.0  # 2 × 2 km ≈ 12 MB za 30 s
SK_BUFFER_M = 20.0
# Kvazigeoid ETRS89-h → Bpv (Baltic 1957) z PROJ-data (ÚGKK SR, CC BY 4.0), ~0,5 MB, stáhne se jednou do cache.
SK_GEOID_URL = "https://cdn.proj.org/sk_gku_Slovakia_ETRS89h_to_Baltic1957.tif"


def sk_wcs_urls(bounds_utm: tuple[float, float, float, float]) -> list[str]:
    return [
        f"{SK_WCS}?service=WCS&version=2.0.1&request=GetCoverage&coverageId={SK_COVERAGE}"
        f"&format=image/tiff&subset=E({x0:.0f},{x1:.0f})&subset=N({y0:.0f},{y1:.0f})&geotiff:compression=Deflate"
        for x0, y0, x1, y1 in tile_boxes(bounds_utm, max_side=SK_MAX_SIDE_M, buffer=SK_BUFFER_M, step=1.0)
    ]


def _sk_geoid_grid(log=None) -> Path | None:
    dest = settings.CACHE_DIR / "geoid" / "sk_gku_Slovakia_ETRS89h_to_Baltic1957.tif"
    if dest.is_file() and dest.stat().st_size > 100_000:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _download(SK_GEOID_URL, dest, log=log)
    except Exception as exc:  # bez mřížky zůstanou výšky elipsoidické (vrstevnice posunuté ~40 m)
        if log:
            log(f"⚠ Kvazigeoid SK se nestáhl ({exc}): výšky zůstanou elipsoidické (o ~40–46 m vyšší).")
        return None


def geoid_offsets(grid: Path, points_lonlat: list[tuple[float, float]]) -> list[float]:
    """Výška kvazigeoidu (m) v bodech; bilineárně mezi středy pixelů mřížky PROJ-data."""
    from app.pipeline.gdal_cli_raster import read_float32_geotiff

    arr, gt, _nd = read_float32_geotiff(grid)
    arr = arr.astype("float64")
    out = []
    for lon, lat in points_lonlat:
        fx = (lon - gt[0]) / gt[1] - 0.5
        fy = (lat - gt[3]) / gt[5] - 0.5
        ix = min(max(int(fx), 0), arr.shape[1] - 2)
        iy = min(max(int(fy), 0), arr.shape[0] - 2)
        dx, dy = min(max(fx - ix, 0.0), 1.0), min(max(fy - iy, 0.0), 1.0)
        v = (
            arr[iy, ix] * (1 - dx) * (1 - dy)
            + arr[iy, ix + 1] * dx * (1 - dy)
            + arr[iy + 1, ix] * (1 - dx) * dy
            + arr[iy + 1, ix + 1] * dx * dy
        )
        out.append(float(v))
    return out


def sk_ugkk_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """DMR 5.0 Slovenska z INSPIRE WCS ÚGKK (výšky elipsoidické, kvazigeoid odečte ``prepare_raster_surfaces``)."""
    bounds_utm = _bounds_in(bounds, CRS_WKT, SK_CRS)
    tiles = [_download(url, out_dir / f"sk_dmr5_{k}.tif", log=log) for k, url in enumerate(sk_wcs_urls(bounds_utm))]
    vrt = out_dir / "sk_dmr5.vrt"
    run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", SK_CRS, str(vrt), *map(str, tiles)], log=log)
    grid = _sk_geoid_grid(log=log)
    offset = None
    if grid is not None:

        def offset(lon: float, lat: float) -> float:
            return geoid_offsets(grid, [(lon, lat)])[0]

    return SourceRasters(vrt, None, offset)


PROVIDERS["sk_ugkk"] = sk_ugkk_provider


def resolve_provider(source: str) -> Provider:
    if source.startswith("local:"):
        return local_provider(source)
    if source in PROVIDERS:
        return PROVIDERS[source]
    raise ElevationUnavailable(f"zdroj výšek „{source}“ není k dispozici")


def _is_krovak(src: Path) -> bool:
    from app.pipeline.gdal_cli_raster import _gdalinfo_json

    wkt = (_gdalinfo_json(src).get("coordinateSystem") or {}).get("wkt") or ""
    return "krovak" in wkt.lower()


def _warp_to_grid(
    src: Path,
    dest: Path,
    bounds: tuple[float, float, float, float],
    resolution_m: float,
    *,
    resampling: str,
    log=None,
) -> Path:
    xmin, ymin, xmax, ymax = bounds
    dest.parent.mkdir(parents=True, exist_ok=True)
    srs_file = dest.parent / "job_crs.wkt"
    srs_file.write_text(CRS_WKT + "\n", encoding="utf-8")
    # S-JTSK zdroje (SK: JTSK i JTSK03) mají stejné souřadnice jako mapa; bez -s_srs by GDAL převáděl
    # datum (oficiální S-JTSK → WGS84 → náš TOWGS84) a rastr posunul o metry až desítky metrů.
    same = ["-s_srs", str(srs_file)] if _is_krovak(src) and "Krovak" in CRS_WKT else []
    run_cmd(
        [
            _gdal_tool("gdalwarp"),
            "-overwrite",
            *same,
            "-t_srs",
            str(srs_file),
            "-te",
            *(f"{v:.3f}" for v in (xmin, ymin, xmax, ymax)),
            "-tr",
            f"{resolution_m:g}",
            f"{resolution_m:g}",
            "-r",
            resampling,
            "-dstnodata",
            f"{NODATA:g}",
            "-ot",
            "Float32",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            str(src),
            str(dest),
        ],
        log=log,
    )
    if not dest.is_file() or dest.stat().st_size < 500:
        raise RuntimeError(f"gdalwarp nevytvořil {dest.name}")
    return dest


def _fill(src: Path, dest: Path, *, log=None) -> Path:
    try:
        tool = _gdal_tool("gdal_fillnodata")
    except RuntimeError:
        dest.write_bytes(src.read_bytes())
        return dest
    run_cmd([tool, "-md", str(FILL_MAX_DISTANCE_PX), "-q", str(src), str(dest)], log=log)
    return dest


def _read(path: Path) -> np.ndarray:
    """Pole Float32 s NoData převedeným na ``NODATA`` (osgeo, jinak GDAL CLI)."""
    from app.pipeline.gdal_cli_raster import read_float32_geotiff

    arr, _gt, nd = read_float32_geotiff(path)
    arr = np.asarray(arr, dtype="float32")
    bad = ~np.isfinite(arr)
    if nd is not None:
        bad |= arr == np.float32(nd)
    return np.where(bad, np.float32(NODATA), arr)


def _write_like(template: Path, arr: np.ndarray, dest: Path) -> Path:
    from app.pipeline.gdal_cli_raster import read_float32_geotiff, write_float32_geotiff

    _a, gt, _nd = read_float32_geotiff(template)
    return write_float32_geotiff(dest, arr, gt, srs_wkt=CRS_WKT, nodata=NODATA)


def _valid_share(path: Path) -> float:
    arr = _read(path)
    return float((arr > NODATA / 2).mean()) if arr.size else 0.0


def _subtract_vertical_offset(path: Path, bounds, offset: Callable[[float, float], float]) -> tuple[float, float]:
    """Odečte posun výšek (kvazigeoid) jako bilineární plochu z rohů výřezu; vrátí rozsah posunu."""
    from pyproj import Transformer

    from app.pipeline.crs_5514 import CRS_PROJ4

    xmin, ymin, xmax, ymax = bounds
    to_ll = Transformer.from_crs(CRS_PROJ4, "EPSG:4258", always_xy=True)
    n00, n10, n01, n11 = (offset(*to_ll.transform(x, y)) for x, y in ((xmin, ymax), (xmax, ymax), (xmin, ymin), (xmax, ymin)))
    arr = _read(path)
    h, w = arr.shape
    fx = (np.arange(w) + 0.5)[None, :] / w
    fy = (np.arange(h) + 0.5)[:, None] / h  # řádek 0 = sever
    plane = n00 * (1 - fx) * (1 - fy) + n10 * fx * (1 - fy) + n01 * (1 - fx) * fy + n11 * fx * fy
    valid = arr > NODATA / 2
    tmp = path.with_name(path.stem + "_vert.tif")
    _write_like(path, np.where(valid, arr - plane, NODATA).astype("float32"), tmp)
    tmp.replace(path)
    return min(n00, n10, n01, n11), max(n00, n10, n01, n11)


def chm_from_rasters(dem_raw: Path, dem_filled: Path, dsm: Path, dest: Path) -> Path:
    """CHM = max(DSM − DTM, 0). Kde chybí DTM z měření (budovy a voda v AHN), je CHM 0 (ne porost),
    kde chybí DSM, také 0; mimo platný DTM vůbec NoData."""
    raw = _read(dem_raw)
    filled = _read(dem_filled)
    surf = _read(dsm)
    valid = filled > NODATA / 2
    chm = np.where(valid, np.maximum(surf - filled, 0.0), NODATA)
    chm = np.where(valid & ((surf <= NODATA / 2) | (raw <= NODATA / 2)), 0.0, chm).astype("float32")
    return _write_like(dem_filled, chm, dest)


def prepare_raster_surfaces(
    work_dir: Path,
    bounds: tuple[float, float, float, float],
    *,
    source: str,
    resolution_m: float = 1.0,
    force_refresh: bool = False,
    log=None,
) -> DemPrepResult:
    """DTM + DSM zdroje → ``work/dem`` na mřížce mapy (stejné soubory jako ``dem_prep``)."""
    del force_refresh  # cache zdrojů řeší provider
    provider = resolve_provider(source)
    dem_dir = dem_work_dir(Path(work_dir))
    src_dir = dem_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    log_step(log, f"Stahuji výšková data ({source}): terén a povrch jako rastr")
    src = provider(tuple(bounds), src_dir, log)
    dtm_src, dsm_src = src.dtm, src.dsm
    if dsm_src is None:
        # Výška porostu vždy z modelu povrchu (Petr 2026-11-14): bez DSM by vegetace chyběla a mapa byla bílá.
        raise ElevationUnavailable(f"zdroj výšek {source!r} nemá model povrchu (DSM / DMP)")

    dem_raw = _warp_to_grid(dtm_src, dem_dir / "dem_raw.tif", bounds, resolution_m, resampling="bilinear", log=log)
    if src.vertical_offset is not None:
        lo, hi = _subtract_vertical_offset(dem_raw, bounds, src.vertical_offset)
        if log:
            log(f"Výšky převedeny z elipsoidických na národní systém (kvazigeoid {lo:.2f}–{hi:.2f} m).")
    share = _valid_share(dem_raw)
    if share < 0.05:
        raise ElevationUnavailable(f"výřez je mimo pokrytí zdroje výšek (platných buněk {share:.0%})")
    if log:
        log(f"Terén z rastru: {share:.0%} buněk s daty, rozlišení {resolution_m:g} m")
    dem_filled = _fill(dem_raw, dem_dir / "dem_filled.tif", log=log)

    # Povrch: maximum v buňce (koruny, ne průměr s mezerami).
    dsm_raw = _warp_to_grid(dsm_src, dem_dir / "dsm_raw.tif", bounds, resolution_m, resampling="max", log=log)
    if src.vertical_offset is not None:
        _subtract_vertical_offset(dsm_raw, bounds, src.vertical_offset)
    dsm_filled = _fill(dsm_raw, dem_dir / "dsm_filled.tif", log=log)
    chm = chm_from_rasters(dem_raw, dem_filled, dsm_raw, dem_dir / "chm.tif")

    result = DemPrepResult(
        dem_raw=dem_raw,
        dem_filled=dem_filled,
        dsm_raw=dsm_raw,
        dsm_filled=dsm_filled,
        chm=chm,
        bounds=tuple(bounds),
        resolution_m=float(resolution_m),
        ground_laz="",
        surface_laz=None,
    )
    result.write_meta(dem_dir)
    (dem_dir / "elevation_source.json").write_text(
        json.dumps({"source": source, "dtm": str(dtm_src), "dsm": str(dsm_src) if dsm_src else None}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return result
