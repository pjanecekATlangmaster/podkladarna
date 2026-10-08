from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app.download_cache import (
    is_fresh,
    link_or_copy,
    read_meta,
    references_cache_dir,
    utcnow_iso,
    write_meta,
)
from app.pipeline.crs_5514 import CRS_PROJ4
from app.pipeline.fetch_openzu import USER_AGENT, crop_bounds_5514
from app.pipeline.georef import PgwGeoref, read_pgw
from app.pipeline.prepare_lidar import find_tool, log_step, run_cmd
from app.settings import REF_CACHE_MAX_AGE_DAYS
from app.tiles import fetch_tile

# Kartografický standard (GDAL výchozí): světlo ze severozápadu, 45° nad obzorem.
HILLSHADE_AZIMUTH = 315
HILLSHADE_ALTITUDE = 45
# Strop výstupního referenčního PNG (orto/ZTM/katastr/hillshade). KP šablona je ~1 m/px.
MAX_REF_PIXELS = 8192
# ČÚZK WMS MaxWidth/MaxHeight = 4096 – nad tím skládáme dlaždice.
WMS_MAX_GETMAP_PX = 4000
# OSM podklad – stejný strop, ať jdou ulice čitelně (ne jen velikost KP šablony).
MAX_OSM_REF_PIXELS = 8192
# Cílové rozlišení v metrech/px (nejjemnější, co se vejde do stropu pixelů).
# Ortofoto ČÚZK je nativně ~0,20 m; 0,25 m je praktický kompromis vůči WMS/OOM.
_REF_MPP_CANDIDATES = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 8.0)
_OSM_MPP_CANDIDATES = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 8.0)
# Ortofoto: vždy 0,25 m/px (les se na 1 m/px nedá mapovat). Nad strop jedné
# šablony se AOI rozdělí na mřížku dlaždic – Mapper načítá šablonu celou do paměti.
ORTHO_MPP = 0.25
# 8192² RGB32 = přesně 256 MiB – Qt (Mapper) má na obrázek alokační limit
# 256 MB, proto rezerva.
ORTHO_MAX_TILE_PX = 8000
_HTTP_RETRIES = 3
_HTTP_RETRY_WAIT_S = 2.0
ORTHO_JPEG_QUALITY = 90
# Katastr: bílé (a téměř bílé) pixely → alfa 0, když server TRANSPARENT ignoruje.
KM_WHITE_THRESHOLD = 250
# Verze obsahu referenční cache (2 = průhledný katastr + ortofoto jako JPEG dlaždice).
REF_CACHE_FORMAT = 2
# DMR 5G má ~0,5 m mezi body – jemnější raster dělá díry a kostičkovaný hillshade.
WEB_MERCATOR_HALF = 20037508.342789244

ORTOFOTO_WMS = (
    "https://ags.cuzk.gov.cz/arcgis1/services/ORTOFOTO/MapServer/WMSServer"
)
# Veřejný OSM WMS (poslední fallback – méně detailní než XYZ dlaždice).
OSM_WMS = "https://ows.terrestris.de/osm/service"
# Preferované XYZ zdroje pro barevný Mapnik styl (GDAL WMS driver).
# openstreetmap.de občas vrací 404 na okrajové dlaždice → GDAL padá.
OSM_XYZ_URLS = (
    "https://a.tile.openstreetmap.fr/osmfr/${z}/${x}/${y}.png",
    "https://tile.openstreetmap.org/${z}/${x}/${y}.png",
    "https://tile.openstreetmap.de/${z}/${x}/${y}.png",
)
HILLSHADE_WMS = (
    "https://ags.cuzk.gov.cz/arcgis2/services/dmr5g/ImageServer/WMSServer"
)
ZTM_WMS = "https://ags.cuzk.gov.cz/arcgis1/services/ZTM/MapServer/WMSServer"
# Veřejná WMS katastrální mapy (KN) – https://services.cuzk.gov.cz/wms/local-km-wms.asp
KM_WMS = "https://services.cuzk.gov.cz/wms/local-km-wms.asp"
KM_LAYER = "KN"
DMPOK_WMS = (
    "https://ags.cuzk.gov.cz/arcgis2/services/dmp_obrazova_korelace/ImageServer/WMSServer"
)
DMPOK_PREVIEW_LAYER = "dmp_obrazova_korelace:TintedHillshadeContinuous"
# (klíč v built_refs, WMS layer, výstupní soubor, popisek OOM, průhlednost, viditelná v OOM)
HILLSHADE_VARIANTS: tuple[tuple[str, str, str, str, float, bool], ...] = (
    (
        "hillshade",
        "dmr5g:GrayscaleHillshade",
        "hillshade_dmr5g.png",
        "Hillshade DMR 5G",
        0.55,
        True,
    ),
    (
        "hillshade_z10",
        "dmr5g:GrayscaleHillshadeZ10",
        "hillshade_dmr5g_z10.png",
        "Hillshade DMR 5G Z10",
        0.50,
        False,
    ),
    (
        "hillshade_z20",
        "dmr5g:GrayscaleHillshadeZ20",
        "hillshade_dmr5g_z20.png",
        "Hillshade DMR 5G Z20",
        0.45,
        False,
    ),
)


def reference_metadata() -> dict:
    return {
        "hillshade_source": "ČÚZK DMR 5G WMS",
        "hillshade_variants": [v[2] for v in HILLSHADE_VARIANTS],
        "hillshade_tool": "WMS ImageServer",
        "map_layers": ["osm.png", "mapa_ztm.png", "katastr.png"],
        "orthophoto": f"JPEG {ORTHO_MPP:.2f} m/px, dlaždice ≤ {ORTHO_MAX_TILE_PX} px (.jgw)",
        "dmpok_preview": "dmpok_nahled.png",
    }


def fetch_cuzk_wms_png(
    wms_url: str,
    layer: str,
    bounds_5514: tuple[float, float, float, float],
    template_png: Path,
    template_pgw: Path,
    dest_png: Path,
    dest_pgw: Path,
    *,
    label: str,
    transparent: bool = False,
    log: callable | None = None,
) -> bool:
    """Stáhne PNG+PGW z ČÚZK WMS ve S-JTSK (EPSG:5514).

    ``transparent``: průhledné pozadí (WMS TRANSPARENT=TRUE, výstup RGBA);
    když server alfu neposlal, bílá → alfa 0.
    """
    tw, th, mpp = _ref_target_size(template_png, template_pgw)
    if log:
        log(f"Stahuji {label} ČÚZK WMS ({tw}×{th} px, ~{mpp:.2f} m/px)…")
    ok = _download_wms_raster(
        wms_url,
        layer,
        bounds_5514,
        dest_png,
        dest_pgw,
        width=tw,
        height=th,
        image_format="image/png",
        transparent=transparent,
        log=log,
    )
    if ok and transparent:
        how = ensure_png_alpha(dest_png)
        if log:
            log(
                f"{label}: průhlednost "
                + ("ze serveru (alfa)" if how == "server" else "bílá → alfa 0 (fallback)")
            )
    if ok and log:
        log(f"{label}: {dest_png.name}")
    return ok


def ensure_png_alpha(png: Path, *, threshold: int = KM_WHITE_THRESHOLD) -> str:
    """Zajistí RGBA PNG s průhledným pozadím.

    Vrací ``"server"`` (alfa už byla, jen případně převod na RGBA) nebo
    ``"fallback"`` (server TRANSPARENT ignoroval – bílé pixely dostaly alfa 0).
    """
    import numpy as np
    from PIL import Image

    with Image.open(png) as im:
        mode = im.mode
        arr = np.array(im.convert("RGBA"))
    if int(arr[..., 3].min()) < 255:
        if mode != "RGBA":
            Image.fromarray(arr, "RGBA").save(png)
        return "server"
    white = (arr[..., :3] >= threshold).all(axis=-1)
    arr[white, 3] = 0
    Image.fromarray(arr, "RGBA").save(png)
    return "fallback"


def _gdal_tool(name: str) -> str:
    for candidate in (name, f"{name}.exe"):
        try:
            return find_tool(candidate)
        except RuntimeError:
            continue
    raise RuntimeError(f"GDAL nástroj '{name}' není k dispozici")


def _raster_size(png: Path) -> tuple[int, int]:
    from struct import unpack

    data = png.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Neplatný PNG: {png}")
    offset = 8
    while offset + 8 <= len(data):
        length = unpack(">I", data[offset : offset + 4])[0]
        chunk = data[offset + 4 : offset + 8]
        if chunk == b"IHDR":
            w, h = unpack(">II", data[offset + 8 : offset + 16])
            return int(w), int(h)
        offset += 12 + length
    raise ValueError(f"PNG bez IHDR: {png}")


def _target_size(width: int, height: int, max_px: int = MAX_REF_PIXELS) -> tuple[int, int]:
    longest = max(width, height, 1)
    if longest <= max_px:
        return width, height
    scale = max_px / longest
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def _extent_target_size(
    template_png: Path,
    template_pgw: Path,
    *,
    max_px: int,
    mpp_candidates: tuple[float, ...],
) -> tuple[int, int, float]:
    """Velikost PNG podle extentu šablony – max detail pod limitem pixelů."""
    xmin, ymin, xmax, ymax, _, _ = _template_extent(template_png, template_pgw)
    width_m = abs(xmax - xmin)
    height_m = abs(ymax - ymin)
    for mpp in mpp_candidates:
        tw = max(1, int(round(width_m / mpp)))
        th = max(1, int(round(height_m / mpp)))
        if max(tw, th) <= max_px:
            return tw, th, mpp
    tw, th = _target_size(
        max(1, int(round(width_m))),
        max(1, int(round(height_m))),
        max_px=max_px,
    )
    mpp = max(width_m / tw, height_m / th)
    return tw, th, mpp


def _ref_target_size(
    template_png: Path,
    template_pgw: Path,
    *,
    max_px: int = MAX_REF_PIXELS,
) -> tuple[int, int, float]:
    """Ortofoto / ZTM / katastr / hillshade – jemnější než KP šablona (~1 m/px)."""
    return _extent_target_size(
        template_png,
        template_pgw,
        max_px=max_px,
        mpp_candidates=_REF_MPP_CANDIDATES,
    )


def _osm_target_size(
    template_png: Path,
    template_pgw: Path,
    *,
    max_px: int = MAX_OSM_REF_PIXELS,
) -> tuple[int, int, float]:
    """Velikost OSM PNG podle extentu šablony – max detail pod limitem pixelů."""
    return _extent_target_size(
        template_png,
        template_pgw,
        max_px=max_px,
        mpp_candidates=_OSM_MPP_CANDIDATES,
    )


def _split_pixel_grid(
    width: int, height: int, max_px: int = WMS_MAX_GETMAP_PX
) -> list[tuple[int, int, int, int]]:
    """Dlaždice (x0, y0, x1, y1) v pixelech; x1/y1 jsou exkluzivní."""
    tiles: list[tuple[int, int, int, int]] = []
    y = 0
    while y < height:
        th = min(max_px, height - y)
        x = 0
        while x < width:
            tw = min(max_px, width - x)
            tiles.append((x, y, x + tw, y + th))
            x += tw
        y += th
    return tiles


def _wms_getmap_url(
    wms_url: str,
    layer: str,
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    width: int,
    height: int,
    image_format: str,
    transparent: bool = False,
) -> str:
    params = {
        "service": "WMS",
        "request": "GetMap",
        "version": "1.3.0",
        "layers": layer,
        "styles": "default",
        "crs": "EPSG:5514",
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "width": str(width),
        "height": str(height),
        "format": image_format,
        "transparent": "true" if transparent else "false",
    }
    return wms_url + "?" + urllib.parse.urlencode(params)


def _http_get_bytes(url: str, timeout: int = 120) -> bytes:
    """GET s opakováním – ortofoto 6×6 km je ~150 GetMap a jeden výpadek
    by jinak zahodil celou vrstvu."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(_HTTP_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # 4xx se opakováním nespraví – jen výpadky sítě a 5xx.
            client_error = isinstance(exc, urllib.error.HTTPError) and exc.code < 500
            if client_error or attempt == _HTTP_RETRIES - 1:
                raise
            time.sleep(_HTTP_RETRY_WAIT_S * (attempt + 1))
    raise AssertionError("unreachable")


def _is_png(data: bytes) -> bool:
    return len(data) >= 500 and data[:8] == b"\x89PNG\r\n\x1a\n"


def _is_jpeg(data: bytes) -> bool:
    return len(data) >= 500 and data[:2] == b"\xff\xd8"


def _pixel_window_bounds(
    bounds_5514: tuple[float, float, float, float],
    full_w: int,
    full_h: int,
    window: tuple[int, int, int, int],
) -> tuple[float, float, float, float]:
    xmin, ymin, xmax, ymax = bounds_5514
    x0, y0, x1, y1 = window
    pixel_x = (xmax - xmin) / full_w
    pixel_y = (ymax - ymin) / full_h
    return (
        xmin + x0 * pixel_x,
        ymax - y1 * pixel_y,
        xmin + x1 * pixel_x,
        ymax - y0 * pixel_y,
    )


def _write_image_bytes(path: Path, data: bytes, image_format: str) -> bool:
    if image_format == "image/jpeg":
        if not _is_jpeg(data):
            return False
    elif not _is_png(data):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def _mosaic_wms_tiles(
    tiles: list[tuple[Path, int, int, int, int]],
    bounds_5514: tuple[float, float, float, float],
    dest_png: Path,
    dest_pgw: Path,
    width: int,
    height: int,
    *,
    jpeg: bool = False,
    transparent: bool = False,
    log: callable | None = None,
) -> bool:
    """Složí WMS dlaždice (PNG/JPEG + world file) do cílového PNG/JPEG + world file.

    ``transparent``: dlaždice se předem převedou na RGBA (server posílá paletové
    PNG s tRNS, které gdalwarp jako alfa nerozpozná) → výstup má alfa pásmo.
    ``jpeg``: výstup JPEG (q90), bez PNG mezikroku.
    """
    xmin, ymin, xmax, ymax = bounds_5514
    if transparent and not jpeg:
        from PIL import Image

        for path, *_ in tiles:
            with Image.open(path) as im:
                rgba = im.convert("RGBA") if im.mode != "RGBA" else None
            if rgba is not None:
                rgba.save(path)
    gdalwarp = _gdal_tool("gdalwarp")
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    warp_out = dest_png
    if jpeg:
        # JPEG driver neumí Create → gdalwarp do VRT, pak gdal_translate.
        warp_out = tiles[0][0].parent / "_mosaic.vrt"
    cmd = [
        gdalwarp,
        "-s_srs",
        CRS_PROJ4,
        "-t_srs",
        CRS_PROJ4,
        "-te",
        str(xmin),
        str(ymin),
        str(xmax),
        str(ymax),
        "-ts",
        str(width),
        str(height),
        "-r",
        "near",
        "-of",
        "VRT" if jpeg else "PNG",
        "-overwrite",
        *[str(path) for path, *_ in tiles],
        str(warp_out),
    ]
    log_step(log, "Skládám dlaždice WMS do jednoho snímku (celý výřez mapy)")
    run_cmd(cmd, log=log)
    if jpeg:
        run_cmd(
            [
                _gdal_tool("gdal_translate"),
                str(warp_out),
                str(dest_png),
                "-of",
                "JPEG",
                "-co",
                f"QUALITY={ORTHO_JPEG_QUALITY}",
            ],
            log=log,
        )
        # GDAL k JPEG píše vedlejší .aux.xml (statistiky) – nepotřebujeme.
        dest_png.with_name(dest_png.name + ".aux.xml").unlink(missing_ok=True)
    if not dest_png.is_file() or dest_png.stat().st_size < 500:
        return False
    _write_pgw_for_extent(dest_pgw, xmin, ymin, xmax, ymax, width, height)
    return True


def _download_wms_raster(
    wms_url: str,
    layer: str,
    bounds_5514: tuple[float, float, float, float],
    dest_png: Path,
    dest_pgw: Path,
    *,
    width: int,
    height: int,
    image_format: str,
    transparent: bool = False,
    log: callable | None = None,
) -> bool:
    """GetMap; nad WMS_MAX_GETMAP_PX stáhne dlaždice a složí je GDAL.

    ``image/jpeg`` zůstává JPEG až do výstupu (``dest_png`` je pak ``.jpg`` a
    ``dest_pgw`` ``.jgw``); ``transparent`` posílá TRANSPARENT=TRUE (jen PNG).
    """
    xmin, ymin, xmax, ymax = bounds_5514
    jpeg = image_format == "image/jpeg"
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    windows = _split_pixel_grid(width, height)
    if len(windows) == 1:
        url = _wms_getmap_url(
            wms_url,
            layer,
            xmin,
            ymin,
            xmax,
            ymax,
            width,
            height,
            image_format,
            transparent=transparent,
        )
        data = _http_get_bytes(url)
        if not _write_image_bytes(dest_png, data, image_format):
            return False
        _write_pgw_for_extent(dest_pgw, xmin, ymin, xmax, ymax, width, height)
        return dest_png.is_file() and dest_png.stat().st_size > 500

    if log:
        log(f"WMS {layer}: {len(windows)} dlaždic (limit {WMS_MAX_GETMAP_PX} px)…")
    work = dest_png.parent / f"_{dest_png.stem}_wms_tiles"
    work.mkdir(parents=True, exist_ok=True)
    ext = ".jpg" if image_format == "image/jpeg" else ".png"
    tiles: list[tuple[Path, int, int, int, int]] = []
    try:
        for i, window in enumerate(windows):
            x0, y0, x1, y1 = window
            bxmin, bymin, bxmax, bymax = _pixel_window_bounds(
                bounds_5514, width, height, window
            )
            tw, th = x1 - x0, y1 - y0
            url = _wms_getmap_url(
                wms_url,
                layer,
                bxmin,
                bymin,
                bxmax,
                bymax,
                tw,
                th,
                image_format,
                transparent=transparent,
            )
            data = _http_get_bytes(url)
            tile_path = work / f"t{i}{ext}"
            if not _write_image_bytes(tile_path, data, image_format):
                return False
            _write_pgw_for_extent(
                tile_path.with_suffix(".pgw"),
                bxmin,
                bymin,
                bxmax,
                bymax,
                tw,
                th,
            )
            if ext == ".jpg":
                # GDAL u JPEG hledá .jgw / .wld, ne .pgw.
                pgw = tile_path.with_suffix(".pgw")
                shutil.copy2(pgw, tile_path.with_suffix(".jgw"))
                shutil.copy2(pgw, tile_path.with_suffix(".wld"))
            tiles.append((tile_path, x0, y0, x1, y1))
        return _mosaic_wms_tiles(
            tiles,
            bounds_5514,
            dest_png,
            dest_pgw,
            width,
            height,
            jpeg=jpeg,
            transparent=transparent,
            log=log,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _template_extent(template_png: Path, template_pgw: Path) -> tuple[float, float, float, float, int, int]:
    georef = read_pgw(template_pgw)
    width, height = _raster_size(template_png)
    xmin = georef.origin_x
    ymax = georef.origin_y
    xmax = xmin + width * georef.pixel_x
    ymin = ymax + height * georef.pixel_y
    return xmin, ymin, xmax, ymax, width, height


def _write_pgw_for_extent(
    dest_pgw: Path,
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    width: int,
    height: int,
) -> None:
    PgwGeoref(
        pixel_x=(xmax - xmin) / width,
        rot_row=0.0,
        rot_col=0.0,
        pixel_y=-(ymax - ymin) / height,
        origin_x=xmin,
        origin_y=ymax,
    ).write(dest_pgw)


def _align_to_template(
    src: Path,
    template_png: Path,
    template_pgw: Path,
    dest_png: Path,
    dest_pgw: Path,
    *,
    resample: str = "bilinear",
    max_px: int | None = None,
    out_size: tuple[int, int] | None = None,
    log: callable | None = None,
) -> None:
    xmin, ymin, xmax, ymax, width, height = _template_extent(template_png, template_pgw)
    if out_size is not None:
        tw, th = out_size
    else:
        tw, th = _target_size(width, height, max_px=max_px or MAX_REF_PIXELS)
    gdalwarp = _gdal_tool("gdalwarp")
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        gdalwarp,
        "-t_srs",
        CRS_PROJ4,
        "-te",
        str(xmin),
        str(ymin),
        str(xmax),
        str(ymax),
        "-ts",
        str(tw),
        str(th),
        "-r",
        resample,
        "-of",
        "PNG",
        "-overwrite",
        str(src),
        str(dest_png),
    ]
    log_step(log, "Zarovnávám podklad na výřez mapy")
    run_cmd(cmd, log=log)
    if tw == width and th == height:
        shutil.copy2(template_pgw, dest_pgw)
    else:
        _write_pgw_for_extent(dest_pgw, xmin, ymin, xmax, ymax, tw, th)


def _fill_dem_nodata(src: Path, dest: Path, *, log: callable | None = None) -> Path:
    """Vyplní malé díry v DEM před hillshade (pokud je k dispozici gdal_fillnodata)."""
    for name in ("gdal_fillnodata.py", "gdal_fillnodata"):
        try:
            tool = _gdal_tool(name)
        except RuntimeError:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        log_step(log, "Doplňuji díry v rastru (souvislá plocha bez mezer)")
        run_cmd([tool, str(src), str(dest), "-md", "24"], log=log)
        return dest
    shutil.copy2(src, dest)
    return dest


def _pdal_dem_from_laz(
    laz: Path,
    bounds: tuple[float, float, float, float],
    dest_tif: Path,
    *,
    resolution_m: float,
    log: callable | None = None,
    grid: tuple[float, float, int, int] | None = None,
) -> Path:
    """``grid`` = (origin_x, origin_y dolní okraj, width, height) – pevná mřížka.

    Bez ní writers.gdal odvodí extent z bodů, takže DEM a DSM z různých LAZ
    mají posunuté počátky (gdal_calc pak odečítá pixel po pixelu ne ty samé).
    """
    xmin, ymin, xmax, ymax = bounds
    pdal = find_tool("pdal")
    steps: list[object] = [
        str(laz),
        {
            "type": "filters.crop",
            "bounds": f"([{xmin},{xmax}],[{ymin},{ymax}])",
        },
    ]
    writer: dict[str, object] = {
        "type": "writers.gdal",
        "filename": str(dest_tif),
        "resolution": resolution_m,
        # max je stabilnější než idw u DMR 5G.
        "output_type": "max",
        "data_type": "float32",
        "gdaldriver": "GTiff",
        "nodata": -9999,
    }
    if grid is not None:
        ox, oy, gw, gh = grid
        writer.update(
            {"origin_x": float(ox), "origin_y": float(oy), "width": int(gw), "height": int(gh)}
        )
    steps.append(writer)
    pipeline = {"pipeline": steps}
    dest_tif.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(pipeline, tmp)
        pipe_path = tmp.name
    try:
        run_cmd([pdal, "pipeline", pipe_path], log=log)
    finally:
        Path(pipe_path).unlink(missing_ok=True)
    return dest_tif


def fetch_hillshade_wms(
    bounds_5514: tuple[float, float, float, float],
    template_png: Path,
    template_pgw: Path,
    dest_png: Path,
    dest_pgw: Path,
    *,
    layer: str,
    label: str,
    log: callable | None = None,
) -> bool:
    """Stáhne hotový hillshade z ČÚZK WMS (DMR 5G ImageServer)."""
    return fetch_cuzk_wms_png(
        HILLSHADE_WMS,
        layer,
        bounds_5514,
        template_png,
        template_pgw,
        dest_png,
        dest_pgw,
        label=label,
        log=log,
    )


def _ortho_target_size(
    template_png: Path, template_pgw: Path
) -> tuple[int, int, float]:
    """Ortofoto vždy 0,25 m/px – bez ohledu na velikost AOI (strop řeší dlaždice)."""
    xmin, ymin, xmax, ymax, _, _ = _template_extent(template_png, template_pgw)
    tw = max(1, int(round(abs(xmax - xmin) / ORTHO_MPP)))
    th = max(1, int(round(abs(ymax - ymin) / ORTHO_MPP)))
    return tw, th, ORTHO_MPP


def _even_spans(total: int, max_px: int) -> list[tuple[int, int]]:
    """Rozdělí ``total`` px na nejméně dílů ≤ ``max_px``, co nejrovnoměrněji."""
    n = max(1, -(-total // max_px))
    base, extra = divmod(total, n)
    spans: list[tuple[int, int]] = []
    pos = 0
    for i in range(n):
        size = base + (1 if i < extra else 0)
        spans.append((pos, pos + size))
        pos += size
    return spans


def _split_ortho_tiles(
    width: int, height: int, max_px: int = ORTHO_MAX_TILE_PX
) -> list[tuple[int, int, int, int, int, int]]:
    """Mřížka dlaždic ortofota: (řádek, sloupec, x0, y0, x1, y1), x1/y1 exkluzivní."""
    tiles: list[tuple[int, int, int, int, int, int]] = []
    for r, (y0, y1) in enumerate(_even_spans(height, max_px)):
        for c, (x0, x1) in enumerate(_even_spans(width, max_px)):
            tiles.append((r, c, x0, y0, x1, y1))
    return tiles


def ortho_tile_stem(row: int, col: int, *, single: bool) -> str:
    return "orthophoto" if single else f"orthophoto_r{row}c{col}"


_ORTHO_KEY_RE = re.compile(r"^orthophoto(?:_r(\d+)c(\d+))?$")


def orthophoto_items(built: dict[str, Path]) -> list[tuple[str, Path]]:
    """Klíče ortofota v ``built_refs`` (``orthophoto`` nebo ``orthophoto_rXcY``) po řádcích."""
    items: list[tuple[tuple[int, int], str, Path]] = []
    for key, path in built.items():
        m = _ORTHO_KEY_RE.match(key)
        if not m:
            continue
        pos = (int(m.group(1)), int(m.group(2))) if m.group(1) is not None else (0, 0)
        items.append((pos, key, path))
    items.sort(key=lambda t: t[0])
    return [(key, path) for _pos, key, path in items]


def world_file_for(raster: Path) -> Path:
    """Vedlejší world file: ``.png`` → ``.pgw``, ``.jpg`` → ``.jgw``."""
    return raster.with_suffix(".jgw" if raster.suffix.lower() in {".jpg", ".jpeg"} else ".pgw")


def list_reference_rasters(folder: Path) -> list[Path]:
    """Referenční PNG i JPEG (ortofoto) ve složce, seřazené podle názvu."""
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in {".png", ".jpg"}
    )


def fetch_orthophoto_wms(
    bounds_5514: tuple[float, float, float, float],
    template_png: Path,
    template_pgw: Path,
    dest_dir: Path,
    *,
    log: callable | None = None,
) -> dict[str, Path]:
    """Ortofoto 0,25 m/px jako JPEG + .jgw; nad 8192 px mřížka dlaždic.

    Vrací ``{klíč: jpg}`` (``orthophoto`` pro jednu dlaždici, jinak
    ``orthophoto_r0c0`` …). Při selhání jakékoli dlaždice smaže vše a vrátí ``{}``.
    """
    tw, th, mpp = _ortho_target_size(template_png, template_pgw)
    grid = _split_ortho_tiles(tw, th)
    single = len(grid) == 1
    if log:
        log(
            f"Stahuji ortofoto ČÚZK ({tw}×{th} px, {mpp:.2f} m/px, "
            f"{len(grid)} {'soubor' if single else 'dlaždic'} JPEG)…"
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    for old in dest_dir.glob("orthophoto*"):
        old.unlink(missing_ok=True)
    built: dict[str, Path] = {}
    try:
        for r, c, x0, y0, x1, y1 in grid:
            stem = ortho_tile_stem(r, c, single=single)
            jpg = dest_dir / f"{stem}.jpg"
            tile_bounds = _pixel_window_bounds(bounds_5514, tw, th, (x0, y0, x1, y1))
            if log and not single:
                log(f"Ortofoto: dlaždice r{r}c{c} ({x1 - x0}×{y1 - y0} px)…")
            ok = _download_wms_raster(
                ORTOFOTO_WMS,
                "0",
                tile_bounds,
                jpg,
                world_file_for(jpg),
                width=x1 - x0,
                height=y1 - y0,
                image_format="image/jpeg",
                log=log,
            )
            if not ok:
                raise RuntimeError(f"dlaždice {stem} se nepodařila")
            built[stem] = jpg
    except Exception:
        for old in dest_dir.glob("orthophoto*"):
            old.unlink(missing_ok=True)
        raise
    if log:
        log("Ortofoto: " + ", ".join(p.name for p in built.values()))
    return built


def _lon_lat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    n = 1 << zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def _mercator_tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 1 << z
    tile_size = 2 * WEB_MERCATOR_HALF / n
    xmin = -WEB_MERCATOR_HALF + x * tile_size
    xmax = xmin + tile_size
    ymax = WEB_MERCATOR_HALF - y * tile_size
    ymin = ymax - tile_size
    return xmin, ymin, xmax, ymax


def _pick_osm_zoom(west: float, south: float, east: float, north: float) -> int:
    width_m = max(abs(east - west), abs(north - south)) * 111_000
    for z in range(19, 11, -1):
        res = 156543.03 * math.cos(math.radians((south + north) / 2)) / (1 << z)
        px = width_m / max(res, 1)
        if px <= MAX_OSM_REF_PIXELS * 1.2:
            return z
    return 12


def _write_osm_vrt(
    tiles: list[tuple[Path, int, int]],
    z: int,
    x0: int,
    y0: int,
    vrt_path: Path,
) -> None:
    """RGB VRT (3 pásma) – jednopásmové VRT zbarví OSM do šeda."""
    width_px = (max(t[1] for t in tiles) - x0 + 1) * 256
    height_px = (max(t[2] for t in tiles) - y0 + 1) * 256
    ulx, _, _, uly = _mercator_tile_bounds(z, x0, y0)
    pixel = (2 * WEB_MERCATOR_HALF / (1 << z)) / 256
    lines = [
        f'<VRTDataset rasterXSize="{width_px}" rasterYSize="{height_px}">',
        f"  <GeoTransform>{ulx}, {pixel}, 0, {uly}, 0, {-pixel}</GeoTransform>",
        "  <SRS>EPSG:3857</SRS>",
    ]
    for band, color in ((1, "Red"), (2, "Green"), (3, "Blue")):
        lines.append(f'  <VRTRasterBand dataType="Byte" band="{band}">')
        lines.append(f"    <ColorInterp>{color}</ColorInterp>")
        for path, x, y in tiles:
            dx = (x - x0) * 256
            dy = (y - y0) * 256
            lines.extend(
                [
                    "    <SimpleSource>",
                    f'      <SourceFilename relativeToVRT="0">{path.as_posix()}</SourceFilename>',
                    f"      <SourceBand>{band}</SourceBand>",
                    '      <SrcRect xOff="0" yOff="0" xSize="256" ySize="256"/>',
                    f'      <DstRect xOff="{dx}" yOff="{dy}" xSize="256" ySize="256"/>',
                    "    </SimpleSource>",
                ]
            )
        lines.append("  </VRTRasterBand>")
    lines.append("</VRTDataset>")
    vrt_path.write_text("\n".join(lines), encoding="utf-8")


def _lonlat_to_mercator(lon: float, lat: float) -> tuple[float, float]:
    x = lon * WEB_MERCATOR_HALF / 180.0
    lat_c = max(min(lat, 85.05112878), -85.05112878)
    y = math.log(math.tan(math.pi / 4.0 + math.radians(lat_c) / 2.0)) * (
        WEB_MERCATOR_HALF / math.pi
    )
    return x, y


def _build_osm_from_wms(
    bbox_wgs84: tuple[float, float, float, float],
    template_png: Path,
    template_pgw: Path,
    dest_png: Path,
    dest_pgw: Path,
    *,
    log: callable | None = None,
) -> bool:
    """OSM přes veřejný WMS (Terrestris) – robustnější než dlaždice v Dockeru."""
    west, south, east, north = bbox_wgs84
    xmin, ymax = _lonlat_to_mercator(west, north)
    xmax, ymin = _lonlat_to_mercator(east, south)
    if xmax <= xmin or ymax <= ymin:
        return False
    tw, th, mpp = _osm_target_size(template_png, template_pgw)
    params = {
        "SERVICE": "WMS",
        "REQUEST": "GetMap",
        "VERSION": "1.1.1",
        "LAYERS": "OSM-WMS",
        "STYLES": "",
        "SRS": "EPSG:3857",
        "BBOX": f"{xmin},{ymin},{xmax},{ymax}",
        "WIDTH": str(tw),
        "HEIGHT": str(th),
        "FORMAT": "image/png",
        "TRANSPARENT": "false",
    }
    url = OSM_WMS + "?" + urllib.parse.urlencode(params)
    if log:
        log(f"OSM WMS fallback ({tw}×{th} px, ~{mpp:.2f} m/px)…")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    if len(data) < 500 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    work = dest_png.parent / "_osm_wms"
    work.mkdir(parents=True, exist_ok=True)
    raw_png = work / "osm_3857.png"
    raw_png.write_bytes(data)
    # World file pro Web Mercator (střed pixelu).
    pixel_x = (xmax - xmin) / tw
    pixel_y = (ymax - ymin) / th
    (work / "osm_3857.pgw").write_text(
        f"{pixel_x}\n0.0\n0.0\n{-pixel_y}\n{xmin + pixel_x / 2}\n{ymax - pixel_y / 2}\n",
        encoding="ascii",
    )
    # GDAL potřebuje SRS – VRT s EPSG:3857.
    vrt = work / "osm_3857.vrt"
    band_blocks: list[str] = []
    for band, color in ((1, "Red"), (2, "Green"), (3, "Blue")):
        band_blocks.extend(
            [
                f'  <VRTRasterBand dataType="Byte" band="{band}">',
                f"    <ColorInterp>{color}</ColorInterp>",
                "    <SimpleSource>",
                f'      <SourceFilename relativeToVRT="0">{raw_png.as_posix()}</SourceFilename>',
                f"      <SourceBand>{band}</SourceBand>",
                f'      <SrcRect xOff="0" yOff="0" xSize="{tw}" ySize="{th}"/>',
                f'      <DstRect xOff="0" yOff="0" xSize="{tw}" ySize="{th}"/>',
                "    </SimpleSource>",
                "  </VRTRasterBand>",
            ]
        )
    vrt.write_text(
        "\n".join(
            [
                f'<VRTDataset rasterXSize="{tw}" rasterYSize="{th}">',
                f"  <GeoTransform>{xmin}, {pixel_x}, 0, {ymax}, 0, {-pixel_y}</GeoTransform>",
                "  <SRS>EPSG:3857</SRS>",
                *band_blocks,
                "</VRTDataset>",
            ]
        ),
        encoding="utf-8",
    )
    _align_to_template(
        vrt,
        template_png,
        template_pgw,
        dest_png,
        dest_pgw,
        out_size=(tw, th),
        log=log,
    )
    if log:
        log(f"OSM podklad: {dest_png.name} (WMS, © OpenStreetMap)")
    return dest_png.is_file() and dest_png.stat().st_size > 500


def _write_osm_xyz_wms_xml(path: Path, server_url: str) -> None:
    """GDAL WMS/TMS XML – barevné OSM dlaždice (Mapnik), ne šedý Terrestris WMS."""
    path.write_text(
        "\n".join(
            [
                "<GDAL_WMS>",
                '  <Service name="TMS">',
                f"    <ServerUrl>{server_url}</ServerUrl>",
                "  </Service>",
                "  <DataWindow>",
                "    <UpperLeftX>-20037508.342789244</UpperLeftX>",
                "    <UpperLeftY>20037508.342789244</UpperLeftY>",
                "    <LowerRightX>20037508.342789244</LowerRightX>",
                "    <LowerRightY>-20037508.342789244</LowerRightY>",
                "    <TileLevel>19</TileLevel>",
                "    <TileCountX>1</TileCountX>",
                "    <TileCountY>1</TileCountY>",
                "    <YOrigin>top</YOrigin>",
                "  </DataWindow>",
                "  <Projection>EPSG:3857</Projection>",
                "  <BlockSizeX>256</BlockSizeX>",
                "  <BlockSizeY>256</BlockSizeY>",
                "  <BandsCount>3</BandsCount>",
                # Chybějící / rate-limit dlaždice → prázdný blok místo pádu gdalwarp.
                "  <ZeroBlockHttpCodes>204,404,429,500,502,503,504</ZeroBlockHttpCodes>",
                "  <ZeroBlockOnServerException>true</ZeroBlockOnServerException>",
                f"  <UserAgent>{USER_AGENT}</UserAgent>",
                "  <Cache/>",
                "</GDAL_WMS>",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _build_osm_from_xyz(
    bbox_wgs84: tuple[float, float, float, float],
    template_png: Path,
    template_pgw: Path,
    dest_png: Path,
    dest_pgw: Path,
    *,
    log: callable | None = None,
) -> bool:
    """Stáhne barevné OSM XYZ přes GDAL (spolehlivější než ruční urllib v Dockeru)."""
    del bbox_wgs84  # extent bere _align_to_template z PNG šablony
    tw, th, mpp = _osm_target_size(template_png, template_pgw)
    work = dest_png.parent / "_osm_xyz"
    work.mkdir(parents=True, exist_ok=True)
    for url in OSM_XYZ_URLS:
        xml = work / "osm_xyz.xml"
        try:
            _write_osm_xyz_wms_xml(xml, url)
            if log:
                host = url.split("/")[2]
                log(
                    f"OSM XYZ dlaždice ({host}) → {tw}×{th} px "
                    f"(~{mpp:.2f} m/px)…"
                )
            _align_to_template(
                xml,
                template_png,
                template_pgw,
                dest_png,
                dest_pgw,
                resample="cubic",
                out_size=(tw, th),
                log=log,
            )
        except Exception as exc:
            if log:
                log(f"OSM XYZ {url.split('/')[2]}: {exc}")
            continue
        if dest_png.is_file() and dest_png.stat().st_size > 20_000:
            # Jednopásmový / prázdný výstup odmítnout.
            head = dest_png.read_bytes()[:32]
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[25] in (2, 6, 3):
                if log:
                    log(f"OSM podklad: {dest_png.name} (XYZ, © OpenStreetMap)")
                return True
    return False


def build_osm_reference(
    bbox_wgs84: tuple[float, float, float, float],
    template_png: Path,
    template_pgw: Path,
    dest_png: Path,
    dest_pgw: Path,
    *,
    log: callable | None = None,
) -> bool:
    # 1) GDAL XYZ (barevný Mapnik) – preferováno
    try:
        if _build_osm_from_xyz(
            bbox_wgs84, template_png, template_pgw, dest_png, dest_pgw, log=log
        ):
            return True
    except Exception as exc:
        if log:
            log(f"OSM XYZ selhal ({exc})")

    west, south, east, north = bbox_wgs84
    try:
        z = _pick_osm_zoom(west, south, east, north)
        x0, y1 = _lon_lat_to_tile(west, north, z)
        x1, y0 = _lon_lat_to_tile(east, south, z)
        tiles: list[tuple[Path, int, int]] = []
        failed = 0
        work = dest_png.parent / "_osm_tiles"
        work.mkdir(parents=True, exist_ok=True)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                try:
                    src = fetch_tile(z, x, y)
                except Exception:
                    failed += 1
                    continue
                dest = work / f"{z}_{x}_{y}.png"
                if not dest.exists():
                    dest.write_bytes(src.read_bytes())
                tiles.append((dest, x, y))
        if log and failed:
            log(f"OSM dlaždice: {failed} stažení selhalo (zoom {z})")
        if tiles:
            if log:
                log(f"OSM dlaždice: zoom {z}, {len(tiles)} ks")
            vrt = work / "mosaic.vrt"
            _write_osm_vrt(tiles, z, x0, y0, vrt)
            raw_tif = work / "mosaic_3857.tif"
            gdaltranslate = _gdal_tool("gdal_translate")
            log_step(log, "Sestavuji OSM dlaždice do jednoho rastru (podklad mapy)")
            run_cmd([gdaltranslate, str(vrt), str(raw_tif), "-of", "GTiff"], log=log)
            _align_to_template(
                raw_tif,
                template_png,
                template_pgw,
                dest_png,
                dest_pgw,
                out_size=_osm_target_size(template_png, template_pgw)[:2],
                log=log,
            )
            if dest_png.is_file() and dest_png.stat().st_size > 500:
                if log:
                    log(f"OSM podklad: {dest_png.name} (© OpenStreetMap)")
                return True
    except Exception as exc:
        if log:
            log(f"OSM dlaždice selhaly ({exc}), zkouším WMS…")

    if log:
        log("OSM: poslední fallback Terrestris WMS (méně detailní)")
    return _build_osm_from_wms(
        bbox_wgs84, template_png, template_pgw, dest_png, dest_pgw, log=log
    )


def _ref_png_key(filename: str) -> str | None:
    # Ortofoto je JPEG (orthophoto.jpg / orthophoto_rXcY.jpg); starý orthophoto.png
    # se nenačítá (cache s ním je stejně neplatná – viz REF_CACHE_FORMAT).
    if filename.lower().endswith(".jpg"):
        stem = filename[:-4]
        return stem if _ORTHO_KEY_RE.match(stem) else None
    mapping = {
        "osm.png": "osm",
        "mapa_ztm.png": "ztm",
        "katastr.png": "katastr",
        "dmpok_nahled.png": "dmpok",
    }
    for key, _layer, name, _label, _op, _vis in HILLSHADE_VARIANTS:
        mapping[name] = key
    return mapping.get(filename)


def _try_load_references_cache(
    cache_dir: Path,
    out_dir: Path,
    *,
    log: callable | None = None,
) -> dict[str, Path] | None:
    """Vrátí built_refs z cache, nebo None při miss."""
    meta = read_meta(cache_dir)
    if not meta or meta.get("ref_format") != REF_CACHE_FORMAT:
        # Stará cache (ortofoto PNG, nepruhledný katastr) → znovu stáhnout.
        return None
    ortho = sorted(cache_dir.glob("orthophoto*.jpg"))
    primary = ortho[0] if ortho else cache_dir / "osm.png"
    if not is_fresh(
        cache_dir, primary, REF_CACHE_MAX_AGE_DAYS, min_size=500
    ):
        return None
    built: dict[str, Path] = {}
    for src in list_reference_rasters(cache_dir):
        key = _ref_png_key(src.name)
        if not key:
            continue
        dest = out_dir / src.name
        link_or_copy(src, dest)
        world = world_file_for(src)
        if world.is_file():
            link_or_copy(world, world_file_for(dest))
        if dest.is_file() and dest.stat().st_size > 500:
            built[key] = dest
    if not built:
        return None
    if log:
        from app.download_cache import age_days

        age = age_days(meta.get("downloaded_at"))
        age_s = f", stáří {age:.1f} d" if age is not None else ""
        log(
            f"Referenční PNG: cache hit ({len(built)} vrstev{age_s}) "
            f"← {cache_dir.name}"
        )
    return built


def _store_references_cache(
    cache_dir: Path,
    out_dir: Path,
    built: dict[str, Path],
    *,
    bbox_wgs84: tuple[float, float, float, float],
    ref_wh: tuple[int, int],
    osm_wh: tuple[int, int],
    log: callable | None = None,
) -> None:
    if not built:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Zbytky starého formátu / jiné mřížky dlaždic (orthophoto.png, r0c2 navíc, …).
    keep = {p.name for p in built.values()} | {
        world_file_for(p).name for p in built.values()
    }
    for old in cache_dir.glob("orthophoto*"):
        if old.name not in keep:
            old.unlink(missing_ok=True)
    stored = 0
    for path in built.values():
        if not path.is_file():
            continue
        shutil.copy2(path, cache_dir / path.name)
        world = world_file_for(path)
        if world.is_file():
            shutil.copy2(world, cache_dir / world.name)
        stored += 1
    write_meta(
        cache_dir,
        kind="references",
        ref_format=REF_CACHE_FORMAT,
        downloaded_at=utcnow_iso(),
        bbox_wgs84=list(bbox_wgs84),
        ref_wh=list(ref_wh),
        osm_wh=list(osm_wh),
        layers=sorted(built.keys()),
        files=stored,
    )
    if log:
        log(f"Referenční PNG: uloženo do cache ({stored} vrstev) → {cache_dir.name}")


def build_reference_layers(
    job_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    template_png: Path,
    template_pgw: Path,
    out_dir: Path,
    *,
    log: callable | None = None,
    force_refresh: bool = False,
) -> dict[str, Path]:
    """Vytvoří referenční PNG+PGW pro OOM (hillshade, ortofoto, OSM)."""
    if not template_png.is_file() or not template_pgw.is_file():
        return {}
    out_dir.mkdir(parents=True, exist_ok=True)
    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north)

    ref_wh = _ref_target_size(template_png, template_pgw)[:2]
    osm_wh = _osm_target_size(template_png, template_pgw)[:2]
    cache_dir = references_cache_dir(bbox_wgs84, ref_wh=ref_wh, osm_wh=osm_wh)
    if not force_refresh:
        cached = _try_load_references_cache(cache_dir, out_dir, log=log)
        if cached is not None:
            return _with_katastr_vectors(cached, bbox_wgs84, out_dir, log=log)
    elif log:
        log("Force refresh: referenční PNG cache se přegeneruje")

    built: dict[str, Path] = {}

    try:
        built.update(
            fetch_orthophoto_wms(bounds, template_png, template_pgw, out_dir, log=log)
        )
    except Exception as exc:
        if log:
            log(f"Ortofoto: přeskočeno ({exc})")

    osm_png = out_dir / "osm.png"
    osm_pgw = osm_png.with_suffix(".pgw")
    try:
        if build_osm_reference(
            bbox_wgs84, template_png, template_pgw, osm_png, osm_pgw, log=log
        ):
            built["osm"] = osm_png
    except Exception as exc:
        if log:
            log(f"OpenStreetMap: přeskočeno ({exc})")

    ztm_png = out_dir / "mapa_ztm.png"
    ztm_pgw = ztm_png.with_suffix(".pgw")
    try:
        if fetch_cuzk_wms_png(
            ZTM_WMS,
            "0",
            bounds,
            template_png,
            template_pgw,
            ztm_png,
            ztm_pgw,
            label="Základní topografická mapa ČR (ZTM)",
            log=log,
        ):
            built["ztm"] = ztm_png
    except Exception as exc:
        if log:
            log(f"Mapa ZTM: přeskočeno ({exc})")

    km_png = out_dir / "katastr.png"
    km_pgw = km_png.with_suffix(".pgw")
    try:
        if fetch_cuzk_wms_png(
            KM_WMS,
            KM_LAYER,
            bounds,
            template_png,
            template_pgw,
            km_png,
            km_pgw,
            label="Katastrální mapa",
            transparent=True,
            log=log,
        ):
            built["katastr"] = km_png
    except Exception as exc:
        if log:
            log(f"Katastrální mapa: přeskočeno ({exc})")

    dmpok_png = out_dir / "dmpok_nahled.png"
    dmpok_pgw = dmpok_png.with_suffix(".pgw")
    try:
        if fetch_cuzk_wms_png(
            DMPOK_WMS,
            DMPOK_PREVIEW_LAYER,
            bounds,
            template_png,
            template_pgw,
            dmpok_png,
            dmpok_pgw,
            label="Náhled DMP OK",
            log=log,
        ):
            built["dmpok"] = dmpok_png
    except Exception as exc:
        if log:
            log(f"Náhled DMP OK: přeskočeno ({exc})")

    hill_ok = False
    for key, wms_layer, filename, label, _opacity, _visible in HILLSHADE_VARIANTS:
        dest_png = out_dir / filename
        dest_pgw = dest_png.with_suffix(".pgw")
        try:
            if fetch_hillshade_wms(
                bounds,
                template_png,
                template_pgw,
                dest_png,
                dest_pgw,
                layer=wms_layer,
                label=label,
                log=log,
            ):
                built[key] = dest_png
                hill_ok = True
        except Exception as exc:
            if log:
                log(f"{label}: přeskočeno ({exc})")
    if log and not hill_ok:
        log("Hillshade: WMS ČÚZK nevrátil žádnou vrstvu")

    try:
        _store_references_cache(
            cache_dir,
            out_dir,
            built,
            bbox_wgs84=bbox_wgs84,
            ref_wh=ref_wh,
            osm_wh=osm_wh,
            log=log,
        )
    except Exception as exc:
        if log:
            log(f"Referenční PNG: cache zápis selhal ({exc})")

    return _with_katastr_vectors(built, bbox_wgs84, out_dir, log=log)


def _with_katastr_vectors(
    built: dict[str, Path],
    bbox_wgs84: tuple[float, float, float, float],
    out_dir: Path,
    *,
    log: callable | None = None,
) -> dict[str, Path]:
    """Vektorový katastr (GeoPackage + DXF) vedle rastrů – mimo rastrovou cache
    (má vlastní cache po KÚ; ořez na výřez je jen pár sekund)."""
    from app.pipeline.fetch_katastr import build_katastr_vectors

    try:
        built.update(build_katastr_vectors(bbox_wgs84, out_dir, log=log))
    except Exception as exc:
        if log:
            log(f"Katastr (vektor): přeskočeno ({exc})")
    return built
