"""Stálezelený porost ze zimního snímku Sentinel-2 (NDVI) pro zimní výšky bez listí (``green: leafoff``).

Zimní výšky (Belgie DHMV II, Slovinsko) mají holé listnaté koruny: zrnitost porostu (``veg_texture``) z nich dělá
falešnou zeleň (Soignes), proto byla v ``leafoff`` vypnutá. Jehličnaté porosty ale mají koruny i v zimě a zrnitost
v nich sedí (Kempen, borovice). Maska = zimní NDVI ≥ ``EVERGREEN_NDVI`` na mřížce CHM; zrnitost se pak kreslí jen
v ní. Livelox 2026-11-22: Hoge Vijvers zeleň v lese 5 → 27 % (mapař 46 %, F1 0,16 → 0,47), Bosbergen 4 → 28 %
(91 %, F1 0,08 → 0,45), bukový Soignes beze změny 2 % (holé buky zimní NDVI 0,5–0,6). Bez jasného snímku nic.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

EVERGREEN_NDVI = 0.65
WINTER_START = (12, 1)
WINTER_END = (2, 28)
WINTER_YEARS = 3  # poslední tři zimy, nejnovější jasný snímek
SCL_MAX_BAD = 0.02  # mraky, stíny i sníh (``sentinel2.SCL_BAD``)
NDVI_MPP = 10.0


def winter_windows(today: date) -> list[tuple[date, date]]:
    """Zimní období (1. 12. – 28. 2.) od nejnovějšího, jen už začatá."""
    out = []
    for year in range(today.year, today.year - WINTER_YEARS - 1, -1):
        start = date(year - 1, *WINTER_START)
        end = min(date(year, *WINTER_END), today)
        if end > start:
            out.append((start, end))
    return out[:WINTER_YEARS]


def _bbox_wgs84(gt, shape, srs_wkt: str) -> tuple[float, float, float, float]:
    from osgeo import osr

    src = osr.SpatialReference()
    src.ImportFromWkt(srs_wkt)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)
    h, w = shape
    pts = [tr.TransformPoint(x, y)[:2] for x in (gt[0], gt[0] + gt[1] * w) for y in (gt[3], gt[3] + gt[5] * h)]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _warp(href: str, gt, shape, srs_wkt: str, res: float, alg: str):
    from osgeo import gdal

    from app.pipeline import sentinel2 as s2

    gdal.UseExceptions()
    h, w = shape
    bounds = (gt[0], gt[3] + gt[5] * h, gt[0] + gt[1] * w, gt[3])
    with gdal.config_options(dict(s2.GDAL_HTTP_CONFIG)):
        ds = gdal.Warp("", f"/vsicurl/{href}", format="MEM", outputBounds=bounds, xRes=res, yRes=res,
                       dstSRS=srs_wkt, resampleAlg=alg)
        return ds.ReadAsArray()


def evergreen_mask(gt, shape, srs_wkt: str, *, log=None, today: date | None = None, search=None, read_band=None,
                   bbox=None):
    """(bool maska na mřížce CHM, den snímku) nebo (None, None) bez jasného zimního snímku.

    ``search`` / ``read_band`` / ``bbox``: náhrady pro testy (``sentinel2.search_scenes`` / ``(href, res, alg) → pole``
    / výřez ve WGS84); bez nich je potřeba osgeo."""
    import numpy as np

    from app.pipeline import sentinel2 as s2

    today = today or date.today()
    search = search or s2.search_scenes
    read_band = read_band or (lambda href, res, alg: _warp(href, gt, shape, srs_wkt, res, alg))
    bbox = bbox or _bbox_wgs84(gt, shape, srs_wkt)

    def _clear(scene) -> bool:
        try:
            scl = read_band(scene.assets["scl"], 20.0, "near")
        except Exception:  # noqa: BLE001 – nečitelná maska = snímek nebrat
            return False
        return bool(scl.size) and float(np.isin(scl, list(s2.SCL_BAD)).mean()) <= SCL_MAX_BAD

    picked = None
    for start, end in winter_windows(today):
        scenes = search(bbox, today=end, days=(end - start).days)
        picked = s2.pick_newest_clear(scenes, _clear)
        if picked is not None:
            break
    if picked is None:
        return None, None
    # COG Element84 jsou bez posunu DN (``Scene.dn_offset`` nepoužívat); měřítko se v NDVI krátí.
    red = np.maximum(read_band(picked.assets["red"], NDVI_MPP, "near").astype(np.float32), 0)
    nir = np.maximum(read_band(picked.assets["nir"], NDVI_MPP, "near").astype(np.float32), 0)
    total = nir + red
    ndvi = np.where(total > 0, (nir - red) / np.maximum(total, 1e-6), 0.0)
    mask = ndvi >= EVERGREEN_NDVI
    h, w = shape
    # 10 m → mřížka CHM (nejbližší soused, beze změny tvaru masky).
    rows = np.minimum((np.arange(h) * mask.shape[0]) // h, mask.shape[0] - 1)
    cols = np.minimum((np.arange(w) * mask.shape[1]) // w, mask.shape[1] - 1)
    return mask[np.ix_(rows, cols)], picked.day


def job_evergreen_mask(work_dir: Path, gt, shape, *, log=None):
    """Maska stálezeleného porostu pro job (CRS z ``dem/job_crs.wkt``) nebo None; chyba = None (kresba jako dřív)."""
    try:
        wkt = (Path(work_dir) / "dem" / "job_crs.wkt").read_text(encoding="utf-8")
        mask, day = evergreen_mask(gt, shape, wkt, log=log)
    except Exception as exc:  # noqa: BLE001 – maska navíc nesmí shodit vegetaci
        if log:
            log(f"Vegetace: zimní snímek Sentinel-2 nedostupný ({type(exc).__name__}) – zrnitost vypnutá")
        return None
    if mask is None:
        if log:
            log("Vegetace: bez jasného zimního snímku Sentinel-2 – zrnitost vypnutá")
        return None
    if log:
        log(f"Vegetace: stálezelený porost ze zimního snímku Sentinel-2 {day.isoformat()} ({100.0 * mask.mean():.0f} % výřezu)")
    return mask
