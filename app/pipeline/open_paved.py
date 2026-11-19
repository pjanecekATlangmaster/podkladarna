"""Sprint mimo Česko: otevřená plocha 401 bez zeleně v ortofotu země a s nízkým letním NDVI jako zpevněná plocha 501.

Petr 2026-11-16 (revize zahraničí, bod 1; měření a náhledy ``evropa-data/zahranicni-build/maska-zelene/``): porost
mimo Česko je z rastru CHM, nízký CHM je 401. Česko má na ulicích parcely RÚIAN (``street_parcels``), v cizině ne, takže
chodníky, náměstí, nábřeží a dvory vycházejí jako žlutá louka (Berlín, Antverpy, Curych 24–30 % viditelné 401).

- **Ortofoto země** (WMS / TMS online šablony z ``online_templates``, 0,5 m) najde kandidáty: okno ``WINDOW_M`` se
  zelení (``orthophoto.green_mask``) nejvýš ``GREEN_MAX_SHARE`` a se zpevněným vzhledem (šedá, ne tmavá) aspoň
  ``PAVED_MIN_SHARE``; kus aspoň ``MIN_AREA_M2``.
- **Letní NDVI Sentinel-2** (B04 / B08, 10 m) kus potvrdí: medián nejvýš ``NDVI_MAX`` (malý kus pod
  ``SMALL_AREA_M2`` přísněji ``NDVI_MAX_SMALL``, 10m pixel ho míchá s okolím). Zimní ortofoto (Flandry) vidí hnědou
  trávu jako dlažbu, NDVI ji zamítne; samo NDVI nestačí (malé trávníky v centru Vídně mají 0,13).

Kus se kreslí jako 501.1 (výplň bez obrysu) pod ostatní kresbou. Barva 501.1 je nad žlutou, 401 se proto nevyřezává
a zůstává pod ní celá jako pod ulicemi z katastru v Česku (``street_parcels``): ořez by po vyhlazení porostu nechal
bílé proužky a rozdrobil 401 na tisíce kousků. Barva 501.1 je ale i nad olivou a zelenou, proto se od hotové části
odečte, co už kreslí jiné části (``trim_to_drawn``: voda, les a zelené porosty, sady, oliva, jiná 501). Les, MTBO a Česko beze změny. Bez ortofota země nebo bez jasného
letního snímku se nic nemění; vstupy se stahují na pozadí od začátku jobu (``prefetch``).
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path

# Záloha: False = sprint mimo Česko kreslí 401 jako dřív.
OPEN_PAVED = True

CACHE_SUBDIR = "open_paved"
ORTHO_NAME = "ortofoto.tif"
ORTHO_META_NAME = "ortofoto.json"
NDVI_NAME = "ndvi.tif"
NDVI_META_NAME = "ndvi.json"
RESULT_NAME = "zpevnene.geojson"
RESULT_META_NAME = "zpevnene.json"

# Ortofoto: výřez mapy + přesah, 0,5 m (jako maska zeleně ulic v Česku).
ORTHO_MPP = 0.5
ORTHO_PAD_M = 20.0
ORTHO_MAX_PIXELS = 60_000_000
# Celé stažení ortofota nejvýš tolik sekund (pomalá WMS job nebrzdí, mapa se kreslí bez toho).
ORTHO_DEADLINE_S = 300.0
# Selhání stažení (chyba jedné dlaždice) se zkusí znovu, dokud běží ORTHO_DEADLINE_S.
ORTHO_ATTEMPTS = 2
ORTHO_RETRY_WAIT_S = 5.0
# Platné ortofoto: černá (mimo snímek) a bílá (prázdná dlaždice) se nepočítají; pod tento podíl výřezu nic.
ORTHO_MIN_VALID_SHARE = 0.5
# Ortofoto klíč online šablony podle země (``online_templates``); Rakousko jen dlaždice basemap.at (TMS).
# Ortofoto jen přes API klíč (není online šablona): země → modul s ``fetch_ortho`` a ``redact``.
DIRECT_ORTHO = {"DK": "app.pipeline.reference_dk"}
ORTHO_KEYS = {
    "AT": "at_ortho",
    "BE": "be_ortho",
    "CH": "ch_swissimage",
    "DE": "de_truedop",
    "EE": "ee_ortho",
    "FI": "fi_ortho",
    "FR": "fr_ortho",
    "IT": "it_ortho",
    "LU": "lu_ortho",
    "DE-NW": "de_nw_dop",
    "DE-ST": "de_st_dop",
    "DE-MV": "de_mv_dop",
    "DE-NI": "de_ni_dop",
    "DE-BB": "de_bb_dop",
    "DE-RP": "de_rp_dop",
    "DE-BW": "de_bw_dop",
    "DE-SN": "de_sn_dop",
    "DE-HE": "de_he_dop",
    "DE-SL": "de_sl_dop",
    "DE-HB": "de_hb_dop",
    "DE-BY": "de_by_dop",
    "PL": "pl_ortho",
    "NL": "nl_luchtfoto",
}

# Kandidát z ortofota (prahy změřené na 9 mapách ze 4 zemí).
WINDOW_M = 5.0
GREEN_MAX_SHARE = 0.15
PAVED_MIN_SHARE = 0.5
PAVED_SAT_MAX = 0.2
DARK_VALUE = 50
MIN_AREA_M2 = 25.0
# Potvrzení letním NDVI: medián kusu.
NDVI_MAX = 0.3
SMALL_AREA_M2 = 200.0
NDVI_MAX_SMALL = 0.2
# Letní snímek: 1. 6. – 10. 9. letos (do dneška), jinak loni; maska SCL ve výřezu nejvýš 1 % špatných pixelů.
SUMMER_START = (6, 1)
SUMMER_END = (9, 10)
SCL_MAX_BAD = 0.01
NDVI_MPP = 10.0
# Výstupní plocha: obrys z pixelů se zjednoduší (m), rozšíří o CLIP_PAD_M a ořízne přesně obrysem 401.
SIMPLIFY_M = 0.25
CLIP_PAD_M = 0.5
# Pás OSM tratě (m od osy): štěrkové lože vypadá v ortofotu jako dlažba.
RAIL_HALF_M = 3.0
# Na předstažení z pozadí se sestavení mapy počká nejvýš tolik sekund, pak stahuje samo.
PREFETCH_WAIT_S = 300.0
# COG v Element84 earth-search mají v metadatech posun −0,1, data ho ale nemají (1. percentil červené 180 DN).
REFLECTANCE_SCALE = 1e-4

_LOCK = threading.Lock()


def applies(preset_id: str) -> bool:
    """Sprint mimo Česko se zapnutou zálohou."""
    from app.pipeline.country_profile import process_uses_cz_sources

    return OPEN_PAVED and str(preset_id).startswith("sprint") and not process_uses_cz_sources()


def _cache_dir(work_dir: Path) -> Path:
    return Path(work_dir) / CACHE_SUBDIR


def map_bounds(bbox_wgs84) -> tuple[float, float, float, float]:
    """Obálka vstupů v souřadnicích jobu (výřez mapy + ``ORTHO_PAD_M``)."""
    from app.pipeline.fetch_openzu import crop_bounds_5514

    return crop_bounds_5514(*tuple(bbox_wgs84), buffer_m=ORTHO_PAD_M)


def _country_code() -> str:
    from app.pipeline.country_profile import process_country

    country = process_country()
    return str(getattr(country, "code", "") or "").upper()


def ortho_source_xml(country: str, bounds) -> str | None:
    """GDAL popis ortofota země (WMS ve výřezu, nebo celosvětové dlaždice TMS), nebo None."""
    from app.pipeline import online_templates as ot

    key = ORTHO_KEYS.get(country)
    if not key:
        return None
    for entry in ot.ONLINE_TMS_LAYERS.get(country, ()):
        if entry[0] == key:
            _key, _filename, _label, url, zoom, _mpp, _photo = entry
            return ot.osm_tms_xml(zoom, url)
    for entry in ot.wms_layers_for(country):
        if entry[0] == key:
            _key, _filename, _label, url, layer, fmt, _transparent, _mpp = entry
            return ot.wms_xml(url, layer, tuple(bounds), image_format=fmt, transparent=False, mpp=ORTHO_MPP)
    return None


def _gdal_http():
    from app.pipeline.sentinel2 import GDAL_HTTP_CONFIG

    return dict(GDAL_HTTP_CONFIG)


def _grid(bounds, mpp: float) -> tuple[int, int, tuple]:
    xmin, ymin, xmax, ymax = bounds
    width = max(1, math.ceil((xmax - xmin) / mpp))
    height = max(1, math.ceil((ymax - ymin) / mpp))
    return width, height, (xmin, ymax - height * mpp, xmin + width * mpp, ymax)


def _meta_matches(path: Path, want: dict) -> bool:
    try:
        return json.loads(path.read_text(encoding="utf-8")) == want
    except (OSError, ValueError):
        return False


def fetch_ortho(work_dir: Path, bounds, *, log=None, deadline_s: float = ORTHO_DEADLINE_S) -> Path | None:
    """Ortofoto země 0,5 m ve výřezu → ``open_paved/ortofoto.tif`` (cache jobu), nebo None."""
    from osgeo import gdal

    from app.pipeline.crs_5514 import CRS_WKT

    country = _country_code()
    out = _cache_dir(work_dir)
    dest, meta_path = out / ORTHO_NAME, out / ORTHO_META_NAME
    width, height, grid = _grid(bounds, ORTHO_MPP)
    want = {"country": country, "bounds": [round(v, 2) for v in grid], "mpp": ORTHO_MPP}
    if dest.is_file() and _meta_matches(meta_path, want):
        return dest
    if width * height > ORTHO_MAX_PIXELS:
        if log:
            log(f"ortofoto přeskočeno: výřez je příliš velký ({width}×{height} px)")
        return None
    if country in DIRECT_ORTHO:
        return _fetch_direct_ortho(country, dest, meta_path, want, grid, width, height, log=log)
    xml = ortho_source_xml(country, grid)
    if xml is None:
        if log:
            log("ortofoto země není k dispozici – beze změny")
        return None
    out.mkdir(parents=True, exist_ok=True)
    src = out / "ortofoto_zdroj.xml"
    src.write_text(xml, encoding="utf-8")
    tmp = out / f"_tmp_{os.getpid()}_{threading.get_ident()}_ortofoto.tif"
    started = time.monotonic()

    def _progress(_complete, _msg, _data):
        return 0 if time.monotonic() - started > deadline_s else 1

    gdal.UseExceptions()
    for attempt in range(ORTHO_ATTEMPTS):
        try:
            with gdal.config_options(_gdal_http()):
                ds = gdal.Warp(
                    str(tmp), str(src), format="GTiff", outputBounds=grid, width=width, height=height,
                    dstSRS=CRS_WKT, resampleAlg="bilinear", outputType=gdal.GDT_Byte,
                    creationOptions=["COMPRESS=DEFLATE", "TILED=YES"], callback=_progress,
                )
                ds = None
            break
        except RuntimeError as exc:
            tmp.unlink(missing_ok=True)
            late = time.monotonic() - started > deadline_s
            if not late and attempt + 1 < ORTHO_ATTEMPTS:  # WMS občas vrátí chybu u jedné dlaždice (Flandry)
                time.sleep(ORTHO_RETRY_WAIT_S)
                continue
            if log:
                log("ortofoto přeskočeno: " + (f"stahování přes {deadline_s:.0f} s" if late else f"{exc}".strip()[:160]))
            return None
    tmp.replace(dest)
    meta_path.write_text(json.dumps(want), encoding="utf-8")
    if log:
        log(f"ortofoto země {width}×{height} px za {time.monotonic() - started:.0f} s")
    return dest


def _fetch_direct_ortho(country: str, dest: Path, meta_path: Path, want: dict, grid, width: int, height: int,
                        *, log=None) -> Path | None:
    """Ortofoto služby s API klíčem (``DIRECT_ORTHO``): stáhne ho modul země sám, bez XML zdroje GDAL ve work/."""
    import importlib

    from app.pipeline.crs_5514 import CRS_WKT

    module = importlib.import_module(DIRECT_ORTHO[country])
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"_tmp_{os.getpid()}_{threading.get_ident()}_ortofoto.tif")
    started = time.monotonic()
    try:
        module.fetch_ortho(tmp, grid, width, height, CRS_WKT)
    except Exception as exc:  # noqa: BLE001 – bez ortofota sprint beze změny
        tmp.unlink(missing_ok=True)
        if log:
            log("ortofoto přeskočeno: " + module.redact(f"{exc}".strip())[:160])
        return None
    tmp.replace(dest)
    meta_path.write_text(json.dumps(want), encoding="utf-8")
    if log:
        log(f"ortofoto země {width}×{height} px za {time.monotonic() - started:.0f} s")
    return dest


def _summer_windows(today: date) -> list[tuple[date, date]]:
    """Letní období k hledání snímku: letos (do dneška), loni."""
    out = []
    for year in (today.year, today.year - 1):
        start = date(year, *SUMMER_START)
        end = min(date(year, *SUMMER_END), today)
        if end > start:
            out.append((start, end))
    return out


def _warp_band(href: str, grid, mpp: float, resampling: str):
    from osgeo import gdal

    from app.pipeline.crs_5514 import CRS_WKT

    width, height, bounds = _grid(grid, mpp)
    with gdal.config_options(_gdal_http()):
        ds = gdal.Warp(
            "", f"/vsicurl/{href}", format="MEM", outputBounds=bounds, width=width, height=height,
            dstSRS=CRS_WKT, resampleAlg=resampling,
        )
        arr = ds.ReadAsArray()
        gt = ds.GetGeoTransform()
    return arr, gt


def fetch_summer_ndvi(work_dir: Path, bbox_wgs84, bounds, *, log=None, today: date | None = None, search=None,
                      read_band=None) -> Path | None:
    """Letní NDVI Sentinel-2 (10 m) ve výřezu → ``open_paved/ndvi.tif`` + ``ndvi.json`` (snímek, den), nebo None.

    ``search`` / ``read_band``: náhrady pro testy (``sentinel2.search_scenes`` / čtení pásma COG)."""
    import numpy as np
    from osgeo import gdal

    from app.pipeline import sentinel2 as s2
    from app.pipeline.crs_5514 import CRS_WKT

    out = _cache_dir(work_dir)
    dest, meta_path = out / NDVI_NAME, out / NDVI_META_NAME
    today = today or date.today()
    _w, _h, grid = _grid(bounds, NDVI_MPP)
    key = {"bounds": [round(v, 2) for v in grid], "today": today.isoformat()}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if meta.get("key") == key and (not meta.get("scene") or dest.is_file()):
            return dest if meta.get("scene") else None  # bez jasného snímku se znovu nehledá
    search = search or s2.search_scenes
    read_band = read_band or _warp_band
    gdal.UseExceptions()

    def _clear(scene) -> bool:
        try:
            scl, _gt = read_band(scene.assets["scl"], grid, 20.0, "near")
        except Exception:  # nečitelná maska = snímek nebrat
            return False
        bad = float(np.isin(scl, list(s2.SCL_BAD)).mean()) if scl.size else 1.0
        return bad <= SCL_MAX_BAD

    picked = None
    for start, end in _summer_windows(today):
        try:
            scenes = search(tuple(bbox_wgs84), today=end, days=(end - start).days)
        except Exception as exc:
            if log:
                log(f"Sentinel-2 nedostupný ({type(exc).__name__}) – beze změny")
            return None
        picked = s2.pick_newest_clear(scenes, _clear)
        if picked is not None:
            break
    out.mkdir(parents=True, exist_ok=True)
    if picked is None:
        meta_path.write_text(json.dumps({"key": key, "scene": None}), encoding="utf-8")
        if log:
            log("Sentinel-2: bez jasného letního snímku – beze změny")
        return None
    try:
        red, gt = read_band(picked.assets["red"], grid, NDVI_MPP, "near")
        nir, _gt = read_band(picked.assets["nir"], grid, NDVI_MPP, "near")
    except Exception as exc:
        if log:
            log(f"Sentinel-2: pásma nečitelná ({type(exc).__name__}) – beze změny")
        return None
    red = np.maximum(red.astype(np.float32), 0) * REFLECTANCE_SCALE
    nir = np.maximum(nir.astype(np.float32), 0) * REFLECTANCE_SCALE
    total = nir + red
    ndvi = np.where(total > 0.01, (nir - red) / np.maximum(total, 1e-6), 0.0).astype(np.float32)
    drv = gdal.GetDriverByName("GTiff")
    tmp = out / f"_tmp_{os.getpid()}_{threading.get_ident()}_ndvi.tif"
    ds = drv.Create(str(tmp), ndvi.shape[1], ndvi.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform(gt)
    ds.SetProjection(CRS_WKT)
    ds.GetRasterBand(1).WriteArray(ndvi)
    ds = None
    tmp.replace(dest)
    meta_path.write_text(
        json.dumps({"key": key, "scene": picked.item_id, "day": picked.day.isoformat()}), encoding="utf-8"
    )
    if log:
        log(f"Sentinel-2 letní snímek {picked.day.isoformat()} (NDVI 10 m)")
    return dest


def prefetch(work_dir: Path, bbox_wgs84, *, log=None) -> None:
    """Na pozadí od začátku jobu (sprint mimo Česko): letní NDVI a ortofoto země do cache jobu."""
    if not OPEN_PAVED:
        return
    with _LOCK:
        bounds = map_bounds(bbox_wgs84)
        if fetch_summer_ndvi(work_dir, bbox_wgs84, bounds, log=log) is not None:
            fetch_ortho(work_dir, bounds, log=log)


def _inputs(work_dir: Path, bbox_wgs84, log=None):
    """(ortofoto, NDVI, den snímku) z cache, po čekání na předstažení; chybějící stáhne samo."""
    got = _LOCK.acquire(timeout=PREFETCH_WAIT_S)
    try:
        bounds = map_bounds(bbox_wgs84)
        ndvi = fetch_summer_ndvi(work_dir, bbox_wgs84, bounds, log=log)
        if ndvi is None:
            return None
        ortho = fetch_ortho(work_dir, bounds, log=log)
        if ortho is None:
            return None
        day = json.loads((_cache_dir(work_dir) / NDVI_META_NAME).read_text(encoding="utf-8")).get("day")
        return ortho, ndvi, day
    finally:
        if got:
            _LOCK.release()


def _stamp(path: Path) -> list:
    try:
        st = path.stat()
    except OSError:
        return [path.name, None]
    return [path.name, st.st_mtime_ns, st.st_size]


def _open_rows_wkb(shp: Path) -> list[bytes]:
    """Řádky 401 z ``vegetation.shp`` (WKB)."""
    from app.pipeline.vegetation_gdal import _iter_vege_rows, vege_class_to_oom_code

    out = []
    for props, wkb in _iter_vege_rows(shp):
        code = str(props.get("code") or "")
        if not code:
            try:
                code = vege_class_to_oom_code(int(props.get("cls"))) or ""
            except (TypeError, ValueError):
                code = ""
        if code == "401" and wkb:
            out.append(bytes(wkb))
    return out


def _rasterize(wkbs: list[bytes], gt, width: int, height: int):
    import numpy as np
    from osgeo import gdal, ogr

    mem = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal.GDT_Byte)
    mem.SetGeoTransform(gt)
    vds = ogr.GetDriverByName("Memory").CreateDataSource("")
    lyr = vds.CreateLayer("open")
    for wkb in wkbs:
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetGeometry(ogr.CreateGeometryFromWkb(wkb))
        lyr.CreateFeature(feat)
    gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1])
    arr = mem.ReadAsArray().astype(bool)
    vds = None
    return np.asarray(arr)


def paved_mask(rgb, open401, ndvi, mpp: float):
    """bool maska kusů 401, které ortofoto vidí jako zpevněné a letní NDVI potvrdí (stejná mřížka)."""
    import numpy as np
    from scipy import ndimage as ndi

    from app.pipeline.orthophoto import green_mask, saturation

    value = rgb.max(axis=-1)
    valid = (value > 0) & ~(rgb.min(axis=-1) >= 250)
    green = green_mask(rgb)
    paved = (~green) & (saturation(rgb) < PAVED_SAT_MAX) & (value >= DARK_VALUE)
    k = max(1, int(round(WINDOW_M / mpp)))
    gshare = ndi.uniform_filter(green.astype(np.float32), k)
    pshare = ndi.uniform_filter(paved.astype(np.float32), k)
    cand = open401 & valid & (gshare <= GREEN_MAX_SHARE) & (pshare >= PAVED_MIN_SHARE)
    cand = ndi.binary_opening(cand, iterations=1)
    lab, n = ndi.label(cand)
    if n == 0:
        return np.zeros_like(cand)
    ids = np.arange(1, n + 1)
    sizes = np.asarray(ndi.sum(cand, lab, ids)) * mpp * mpp
    med = np.asarray(ndi.median(ndvi, lab, ids))
    limit = np.where(sizes < SMALL_AREA_M2, NDVI_MAX_SMALL, NDVI_MAX)
    keep = ids[(sizes >= MIN_AREA_M2) & (med <= limit)]
    return np.isin(lab, keep)


def prepare(work_dir: Path, bbox_wgs84, *, preset_id: str, log=None) -> list:
    """Zpevněné kusy 401 (shapely polygony v souřadnicích jobu) pro sprint mimo Česko; jinak []. Výsledek se uloží
    do ``open_paved/zpevnene.geojson``."""
    if not applies(preset_id):
        return []
    from app.pipeline.vegetation_chm import CHM_SOURCE_MARK

    work_dir = Path(work_dir)
    shp = work_dir / "vegetation" / "vegetation.shp"
    if not shp.is_file() or not (shp.parent / CHM_SOURCE_MARK).is_file():
        return []
    try:
        return _prepare(work_dir, shp, bbox_wgs84, log=log)
    except Exception as exc:  # mapa se kreslí i bez toho
        if log:
            log(f"Zpevněné plochy z ortofota: přeskočeno ({type(exc).__name__}: {exc})")
        try:  # starší výsledek nesmí zůstat (``load_polygons``)
            _write_result(_cache_dir(work_dir), [], None)
        except OSError:
            pass
        return []


def _prepare(work_dir: Path, shp: Path, bbox_wgs84, *, log=None) -> list:
    import numpy as np
    from osgeo import gdal
    from shapely.geometry import shape

    from app.pipeline import dtm_import
    from app.pipeline.osm_paths import NATURAL_COVER_NAME, SAND_AREAS_NAME

    out = _cache_dir(work_dir)
    inputs = _inputs(work_dir, bbox_wgs84, log=log)
    if inputs is None:
        _write_result(out, [], None)
        if log:
            log("Zpevněné plochy z ortofota: bez ortofota země nebo letního snímku – beze změny")
        return []
    ortho, ndvi_path, day = inputs
    stamp = [_stamp(shp), _stamp(shp.with_suffix(".dbf")), _stamp(ortho), _stamp(ndvi_path),
             _stamp(work_dir / "osm_paths" / "features.geojson"), _stamp(work_dir / "osm_paths" / SAND_AREAS_NAME),
             _stamp(work_dir / "osm_paths" / NATURAL_COVER_NAME), _stamp(dtm_import.buildings_path(work_dir))]
    res, meta_path = out / RESULT_NAME, out / RESULT_META_NAME
    if res.is_file() and _meta_matches(meta_path, {"stamp": stamp}):
        data = json.loads(res.read_text(encoding="utf-8"))
        return [shape(f["geometry"]) for f in data.get("features") or []]
    gdal.UseExceptions()
    ds = gdal.Open(str(ortho))
    gt = ds.GetGeoTransform()
    width, height = ds.RasterXSize, ds.RasterYSize
    rgb = np.moveaxis(ds.ReadAsArray()[:3], 0, -1)  # TMS / WMS může mít i alfu
    ds = None
    valid = (rgb.max(axis=-1) > 0) & ~(rgb.min(axis=-1) >= 250)
    polys: list = []
    if float(valid.mean()) >= ORTHO_MIN_VALID_SHARE:
        open_wkbs = _open_rows_wkb(shp)
        open401 = _rasterize(open_wkbs, gt, width, height)
        exclude = exclude_zone(work_dir)
        if exclude is not None:
            open401 &= ~_rasterize([exclude.wkb], gt, width, height)
        bounds = (gt[0], gt[3] + gt[5] * height, gt[0] + gt[1] * width, gt[3])
        ndvi = gdal.Warp("", str(ndvi_path), format="MEM", outputBounds=bounds, width=width, height=height,
                         resampleAlg="bilinear").ReadAsArray()
        mask = paved_mask(rgb, open401, ndvi, gt[1])
        polys = fit_to_open(_mask_to_polygons(mask, gt), open_wkbs, exclude)
    elif log:
        log(f"Zpevněné plochy z ortofota: ortofoto pokrývá jen {valid.mean() * 100:.0f} % výřezu – beze změny")
    _write_result(out, polys, {"stamp": stamp})
    if log:
        area = sum(p.area for p in polys)
        log(f"Zpevněné plochy z ortofota: {len(polys)} kusů, {area:.0f} m² místo 401 (Sentinel-2 {day})")
    return polys


def _mask_to_polygons(mask, gt) -> list:
    from shapely.geometry import Polygon

    from app.pipeline.street_parcels import _mask_polygons

    if not mask.any():
        return []
    geom = _mask_polygons(mask, gt[0], gt[3], gt[1])
    if geom is None or geom.is_empty:
        return []
    geom = geom.simplify(SIMPLIFY_M, preserve_topology=True).buffer(0)
    out = []
    for g in getattr(geom, "geoms", [geom]):
        if isinstance(g, Polygon) and g.area >= MIN_AREA_M2:
            out.append(g)
    return out


def fit_to_open(polys: list, open_wkbs: list[bytes], exclude=None) -> list:
    """Kusy z pixelové masky o ``CLIP_PAD_M`` rozšířené a oříznuté přesně obrysem 401 (mřížka CHM a ortofota se liší,
    jinak by mezi nimi zůstaly žluté proužky); bez ``exclude`` (tratě, voda, budovy, pláž – ``exclude_zone``)."""
    if not polys or not open_wkbs:
        return []
    import shapely
    from shapely.geometry import Polygon

    rows = [shapely.from_wkb(w) for w in open_wkbs]
    row_tree = shapely.STRtree(rows)
    ex_parts = [g for g in getattr(exclude, "geoms", [exclude]) if not g.is_empty] if exclude is not None else []
    ex_tree = shapely.STRtree(ex_parts) if ex_parts else None

    def _near(tree, parts, geom):  # sjednocení jen dílů u ``geom`` (celé sjednocení je u velkých map pomalé)
        if tree is None:
            return None
        idx = tree.query(geom, predicate="intersects")
        return shapely.union_all([parts[i] for i in idx]) if len(idx) else None

    out = []
    for poly in polys:
        padded = poly.buffer(CLIP_PAD_M, join_style="mitre")
        near_open = _near(row_tree, rows, padded)
        if near_open is None:
            continue
        geom = padded.intersection(near_open)
        near_ex = _near(ex_tree, ex_parts, geom)
        if near_ex is not None:
            geom = geom.difference(near_ex)
        for g in getattr(geom, "geoms", [geom]):
            if isinstance(g, Polygon) and g.area >= MIN_AREA_M2:
                out.append(g)
    if not out:
        return []
    # Zbytek řádku 401 menší než kus CHM (MIN_AREA_M2) je okraj pixelu, ne trávník: přidá se k ploše, jinak by zůstal
    # žlutý drobek (nebo po zahození bílá díra).
    out_tree = shapely.STRtree(out)
    extra = []
    for row in rows:
        near_paved = _near(out_tree, out, row)
        if near_paved is None:
            continue
        rest = row.difference(near_paved)
        for g in getattr(rest, "geoms", [rest]):
            if not isinstance(g, Polygon) or g.area >= MIN_AREA_M2:
                continue
            near_ex = _near(ex_tree, ex_parts, g)
            if near_ex is not None and g.intersection(near_ex).area > 0.5 * g.area:
                continue
            extra.append(g)
    if not extra:
        return out
    merged = shapely.union_all(out + extra)
    return [g for g in getattr(merged, "geoms", [merged]) if isinstance(g, Polygon) and g.area >= MIN_AREA_M2]


def exclude_zone(work_dir: Path):
    """Kde zpevněná plocha z ortofota nebude, nebo None: pás OSM tratí (``rail_line``, bez tramvají – ty vedou
    v dlážděné ulici; štěrkové lože vypadá jako dlažba), OSM vodní plochy, budovy (OSM i technická mapa; CHM je
    pod nimi často 401 a šedá střecha vypadá jako dlažba), přírodní kryt OSM (``osm_paths.NATURAL_COVER_NAME``: les,
    louka, křoviny, vřesoviště, mokřad …; suchá tráva dun vypadá jako dlažba) a mimo Česko pláž a písek
    (``osm_paths.SAND_AREAS_NAME``, světlý písek vypadá jako beton – Scheveningen, Pirita)."""
    from shapely.geometry import LineString, Polygon, shape
    from shapely.ops import unary_union

    from app.pipeline import dtm_import
    from app.pipeline.osm_paths import NATURAL_COVER_NAME, SAND_AREAS_NAME

    osm_dir = Path(work_dir) / "osm_paths"
    zones = []

    def _features(name: str) -> list:
        try:
            return json.loads((osm_dir / name).read_text(encoding="utf-8")).get("features") or []
        except (OSError, ValueError):
            return []

    for feat in _features("features.geojson"):
        geom = feat.get("geometry") or {}
        kind = (feat.get("properties") or {}).get("kind")
        try:
            if kind == "rail_line" and geom.get("type") == "LineString" and len(geom.get("coordinates") or []) >= 2:
                zones.append(LineString(geom["coordinates"]).buffer(RAIL_HALF_M, cap_style="flat"))
            elif kind in ("water_body", "building") and geom.get("type") in ("Polygon", "MultiPolygon"):
                zones.append(shape(geom).buffer(0))
        except (ValueError, TypeError, AttributeError):
            continue
    try:
        building_rings = dtm_import._load_polygons_file(dtm_import.buildings_path(work_dir))
    except Exception:  # budovy technické mapy jsou doplněk
        building_rings = []
    for rings in building_rings:
        if rings and len(rings[0]) >= 4:
            zones.append(Polygon(rings[0], rings[1:]).buffer(0))
    for feat in [*_features(NATURAL_COVER_NAME), *_features(SAND_AREAS_NAME)]:
        try:
            zones.append(shape(feat.get("geometry") or {}).buffer(0))
        except (ValueError, TypeError, AttributeError):
            continue
    zones = [z for z in zones if not z.is_empty]
    return unary_union(zones) if zones else None


def _write_result(out: Path, polys: list, meta: dict | None) -> None:
    from shapely.geometry import mapping

    out.mkdir(parents=True, exist_ok=True)
    feats = [{"type": "Feature", "properties": {}, "geometry": mapping(p)} for p in polys]
    (out / RESULT_NAME).write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    meta_path = out / RESULT_META_NAME
    if meta is None:
        meta_path.unlink(missing_ok=True)
    else:
        meta_path.write_text(json.dumps(meta), encoding="utf-8")


def result_path(work_dir: Path) -> Path:
    return _cache_dir(work_dir) / RESULT_NAME


def load_polygons(work_dir: Path) -> list:
    """Uložené kusy z ``prepare`` (prázdné, když nejsou)."""
    from shapely.geometry import shape

    try:
        data = json.loads(result_path(work_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for feat in data.get("features") or []:
        try:
            g = shape(feat.get("geometry") or {})
        except (ValueError, TypeError, AttributeError):
            continue
        if not g.is_empty:
            out.append(g)
    return out


def build_open_paved_part(work_dir: Path, polygons: list, *, preset_id: str, scale: int, ref_x: float, ref_y: float,
                          grivation_deg: float):
    """Část mapy 501.1 (výplň bez obrysu) z kusů ``prepare``; None, když nic nebo chybí symbol."""
    if not polygons:
        return None
    from app.pipeline.oom_import import OomObjectPart, _geom_parts_to_objects, _wkb_parts
    from app.pipeline.oom_symbol_map import symbol_index_for_code
    from app.pipeline.osm_paths import feature_oom_code

    sym = symbol_index_for_code(preset_id, scale, "501.1")
    if sym is None:
        sym = symbol_index_for_code(preset_id, scale, feature_oom_code("pedestrian_area", preset_id, "501"))
    if sym is None:
        return None
    objects: list[str] = []
    for poly in polygons:
        geom_parts, _ = _wkb_parts(poly.wkb)
        objects.extend(
            _geom_parts_to_objects(
                geom_parts, sym, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg, as_area=True
            )
        )
    if not objects:
        return None
    return OomObjectPart(name=PART_NAME, objects_xml="\n".join(objects), count=len(objects))


PART_NAME = "Zpevněné plochy z ortofota (501)"


# Plochy, které 501.1 nepřekryje (barva 501.1 je v sadě nad olivou i zelenou): voda, les a zelené porosty, sady,
# vinice, oliva. Plochy 501 z jiných zdrojů (mezery ``residual_paved``, OSM, technická mapa) se odečtou také,
# aby se plocha nekreslila dvakrát; 501 na mostě ne (pod mostem je zem).
COVER_MAJORS = frozenset({301, 302, 405, 406, 407, 408, 409, 410, 411, 413, 414, 520})
# Po ořezu pryč pruhy užší než 2 × TRIM_OPEN_M (zbytky podél ořezaných ploch).
TRIM_OPEN_M = 0.5


def trim_to_drawn(parts, *, preset_id: str, scale: int, log=None):
    """``parts`` s částí ``PART_NAME`` bez ploch, které už kreslí jiné části (``COVER_MAJORS``, plochy 501 mimo mosty).

    Zbytek se otevře o ``TRIM_OPEN_M`` a kusy pod ``MIN_AREA_M2`` se zahodí. Bez té části vrací ``parts`` beze změny,
    při chybě také (kresba zůstane neořezaná)."""
    at = next((i for i, p in enumerate(parts) if p.name == PART_NAME), None)
    if at is None:
        return parts
    try:
        objects = _trimmed_objects(parts, at, preset_id=preset_id, scale=scale)
    except Exception as exc:  # kosmetika
        if log:
            log(f"⚠ Zpevněné plochy z ortofota: ořez o kresbu přeskočen ({type(exc).__name__}: {exc})")
        return parts
    if objects is None:
        return parts
    from dataclasses import replace

    out = list(parts)
    if objects:
        out[at] = replace(parts[at], objects_xml="\n".join(objects), count=len(objects))
    else:
        del out[at]
    if log:
        log(f"Zpevněné plochy z ortofota (501): {len(objects)} ploch po ořezu o kresbu")
    return out


def _trimmed_objects(parts, at: int, *, preset_id: str, scale: int):
    """Objekty části ``parts[at]`` po ořezu (seznam XML), nebo None, když se nic neořezalo."""
    import shapely
    from shapely.geometry import Polygon

    from app.pipeline.oom_import import _area_object_with_holes
    from app.pipeline.oom_symbols import symbol_set_path
    from app.pipeline.path_clearance import _COORDS_RE, _OBJECT_RE, _parse_coords, _polygon, _rings, _rounded_pieces, _symbol_info
    from app.pipeline.street_parcels import BRIDGE_PART_PREFIX

    def area_poly(m):
        cm = _COORDS_RE.search(m.group(4))
        poly = _polygon(_rings(_parse_coords(cm.group(1)))) if cm else None
        if poly is None or poly.is_empty:
            return None
        return poly if poly.is_valid else poly.buffer(0)

    info = _symbol_info(symbol_set_path(preset_id, scale))
    cover = []
    for i, part in enumerate(parts):
        if i == at:
            continue
        on_bridge = str(part.name).startswith(BRIDGE_PART_PREFIX)
        for m in _OBJECT_RE.finditer(part.objects_xml):
            if m.group(2) != "1":
                continue
            major, kind = info.get(int(m.group(3)), (0, ""))
            if kind != "area" or not (major in COVER_MAJORS or (major == 501 and not on_bridge)):
                continue
            poly = area_poly(m)
            if poly is not None and not poly.is_empty:
                cover.append(poly)
    if not cover:
        return None
    tree = shapely.STRtree(cover)
    um_per_m = 1e6 / scale
    half = TRIM_OPEN_M * um_per_m
    min_area = MIN_AREA_M2 * um_per_m * um_per_m
    objects, changed = [], False
    for m in _OBJECT_RE.finditer(parts[at].objects_xml):
        poly = area_poly(m) if m.group(2) == "1" else None
        hits = [cover[int(k)] for k in tree.query(poly, predicate="intersects")] if poly is not None else []
        if not hits:
            objects.append(m.group(0))
            continue
        changed = True
        rest = poly.difference(shapely.union_all(hits).buffer(0))
        rest = rest.buffer(-half, quad_segs=2, join_style="mitre").buffer(half, quad_segs=2, join_style="mitre")
        sym = int(m.group(3))
        for g in getattr(rest, "geoms", [rest]):
            if not isinstance(g, Polygon) or g.area < min_area:
                continue
            for pts in _rounded_pieces(g):
                obj = _area_object_with_holes(sym, [[(p[0], p[1]) for p in r] for r in _rings(pts)])
                if obj:
                    objects.append(obj)
    return objects if changed else None
