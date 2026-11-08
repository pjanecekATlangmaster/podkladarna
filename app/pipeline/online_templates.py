"""Online šablony podkladů pro OOM (Petr 2026-11-08): malé soubory GDAL WMS ``.xml`` místo stažených rastrů.

Mapper (``GdalTemplate``) si podklad stáhne až při zapnutí šablony, ZIP zůstává malý. Každý soubor pokrývá
výřez mapy v pevném rozlišení (nejvýš ``MAX_PX`` px na delší straně – Mapper načte celý rastr do paměti).
OSM je dlaždicová služba v EPSG:3857: ``.vrt`` s oknem výřezu nad celosvětovou TMS ``.xml``.
OCAD ``.xml`` neotevře – adresy WMS jsou v README_OOM.
"""

from __future__ import annotations

import html
import math
from pathlib import Path

from app.pipeline.fetch_openzu import USER_AGENT
from app.pipeline.reference_layers import (
    DMPOK_PREVIEW_LAYER,
    DMPOK_WMS,
    DMVS_ZPS_LINIE_LAYER,
    DMVS_ZPS_LINIE_WMS,
    DMVS_ZPS_PLOCHY_LAYER,
    DMVS_ZPS_PLOCHY_WMS,
    HILLSHADE_VARIANTS,
    HILLSHADE_WMS,
    KM_LAYER,
    KM_WMS,
    ORTOFOTO_WMS,
    WEB_MERCATOR_HALF,
    ZTM_WMS,
)

# Strop jedné šablony (Qt v Mapperu má limit obrázku 256 MB, viz ``ORTHO_MAX_TILE_PX``).
MAX_PX = 8000
# Bloky dotazů: ČÚZK ORTOFOTO WMS kreslí nad ~1000 px černé okraje (viz ``_wms_getmap_url``).
BLOCK_PX = 500
OSM_TILE_URL = "https://a.tile.openstreetmap.fr/osmfr/${z}/${x}/${y}.png"
OSM_MAX_ZOOM = 18

# (klíč, soubor, popis pro README, WMS, vrstva, formát, průhledná, m/px)
ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("orthophoto", "ortofoto_cuzk.xml", "Ortofoto ČÚZK", ORTOFOTO_WMS, "0", "image/jpeg", False, 0.25),
    ("ztm", "mapa_ztm.xml", "Základní mapa ČÚZK (ZTM)", ZTM_WMS, "0", "image/png", False, 1.0),
    ("katastr", "katastr.xml", "Katastrální mapa", KM_WMS, KM_LAYER, "image/png", True, 0.25),
    (
        "dtm_plochy", "dtm_plochy.xml", "DTM kraje – plochy (ZPS)",
        DMVS_ZPS_PLOCHY_WMS, DMVS_ZPS_PLOCHY_LAYER, "image/png", True, 0.25,
    ),
    (
        "dtm_linie", "dtm_linie.xml", "DTM kraje – linie a body (ZPS)",
        DMVS_ZPS_LINIE_WMS, DMVS_ZPS_LINIE_LAYER, "image/png", True, 0.25,
    ),
    ("dmpok", "dmpok_nahled.xml", "Náhled DMP OK", DMPOK_WMS, DMPOK_PREVIEW_LAYER, "image/png", False, 0.5),
    *(
        (key, Path(filename).with_suffix(".xml").name, label, HILLSHADE_WMS, layer, "image/png", False, 0.5)
        for key, layer, filename, label, _opacity, _visible in HILLSHADE_VARIANTS
    ),
)
ONLINE_OSM = ("osm", "osm.vrt", "OpenStreetMap")


def _size(width_m: float, height_m: float, mpp: float) -> tuple[int, int]:
    mpp = max(mpp, max(width_m, height_m) / MAX_PX)
    return max(1, round(width_m / mpp)), max(1, round(height_m / mpp))


def wms_xml(
    url: str,
    layer: str,
    bounds_5514: tuple[float, float, float, float],
    *,
    image_format: str,
    transparent: bool,
    mpp: float,
) -> str:
    """GDAL WMS popis jedné vrstvy ČÚZK ve výřezu (S-JTSK, WMS 1.3.0)."""
    xmin, ymin, xmax, ymax = bounds_5514
    sx, sy = _size(xmax - xmin, ymax - ymin, mpp)
    # DMVS WMS styl „default“ odmítá (500) – stejně jako ``_wms_getmap_url``.
    styles = "" if "dmvs.cuzk.gov.cz" in url else "default"
    sep = "&" if "?" in url else "?"
    return (
        "<GDAL_WMS>\n"
        '  <Service name="WMS">\n'
        "    <Version>1.3.0</Version>\n"
        f"    <ServerUrl>{html.escape(url + sep)}</ServerUrl>\n"
        "    <CRS>EPSG:5514</CRS>\n"
        f"    <ImageFormat>{image_format}</ImageFormat>\n"
        f"    <Transparent>{'TRUE' if transparent else 'FALSE'}</Transparent>\n"
        f"    <Layers>{html.escape(layer)}</Layers>\n"
        f"    <Styles>{styles}</Styles>\n"
        "  </Service>\n"
        "  <DataWindow>\n"
        f"    <UpperLeftX>{xmin:.2f}</UpperLeftX>\n"
        f"    <UpperLeftY>{ymax:.2f}</UpperLeftY>\n"
        f"    <LowerRightX>{xmax:.2f}</LowerRightX>\n"
        f"    <LowerRightY>{ymin:.2f}</LowerRightY>\n"
        f"    <SizeX>{sx}</SizeX>\n"
        f"    <SizeY>{sy}</SizeY>\n"
        "  </DataWindow>\n"
        "  <Projection>EPSG:5514</Projection>\n"
        f"  <BandsCount>{4 if transparent else 3}</BandsCount>\n"
        f"  <BlockSizeX>{BLOCK_PX}</BlockSizeX>\n"
        f"  <BlockSizeY>{BLOCK_PX}</BlockSizeY>\n"
        "  <MaxConnections>4</MaxConnections>\n"
        f"  <UserAgent>{html.escape(USER_AGENT)}</UserAgent>\n"
        "  <ZeroBlockHttpCodes>204,404</ZeroBlockHttpCodes>\n"
        "</GDAL_WMS>\n"
    )


def osm_tms_xml(zoom: int) -> str:
    """Celosvětová dlaždicová služba OSM (EPSG:3857) do úrovně ``zoom``."""
    h = WEB_MERCATOR_HALF
    return (
        "<GDAL_WMS>\n"
        '  <Service name="TMS">\n'
        f"    <ServerUrl>{OSM_TILE_URL}</ServerUrl>\n"
        "  </Service>\n"
        "  <DataWindow>\n"
        f"    <UpperLeftX>{-h}</UpperLeftX>\n"
        f"    <UpperLeftY>{h}</UpperLeftY>\n"
        f"    <LowerRightX>{h}</LowerRightX>\n"
        f"    <LowerRightY>{-h}</LowerRightY>\n"
        f"    <TileLevel>{zoom}</TileLevel>\n"
        "    <TileCountX>1</TileCountX>\n"
        "    <TileCountY>1</TileCountY>\n"
        "    <YOrigin>top</YOrigin>\n"
        "  </DataWindow>\n"
        "  <Projection>EPSG:3857</Projection>\n"
        "  <BlockSizeX>256</BlockSizeX>\n"
        "  <BlockSizeY>256</BlockSizeY>\n"
        "  <BandsCount>3</BandsCount>\n"
        "  <MaxConnections>2</MaxConnections>\n"
        f"  <UserAgent>{html.escape(USER_AGENT)}</UserAgent>\n"
        "  <ZeroBlockHttpCodes>204,404</ZeroBlockHttpCodes>\n"
        "</GDAL_WMS>\n"
    )


def _mercator(lon: float, lat: float) -> tuple[float, float]:
    x = math.radians(lon) * 6378137.0
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * 6378137.0
    return x, y


def osm_window(
    bbox_wgs84: tuple[float, float, float, float],
) -> tuple[int, int, int, int, int]:
    """(zoom, x0, y0, šířka, výška) v pixelech celosvětové mřížky OSM, okno do ``MAX_PX``."""
    west, south, east, north = bbox_wgs84
    x0m, y0m = _mercator(west, north)
    x1m, y1m = _mercator(east, south)
    for zoom in range(OSM_MAX_ZOOM, 0, -1):
        pixel = 2 * WEB_MERCATOR_HALF / (256 << zoom)
        px0 = math.floor((x0m + WEB_MERCATOR_HALF) / pixel)
        py0 = math.floor((WEB_MERCATOR_HALF - y0m) / pixel)
        px1 = math.ceil((x1m + WEB_MERCATOR_HALF) / pixel)
        py1 = math.ceil((WEB_MERCATOR_HALF - y1m) / pixel)
        if max(px1 - px0, py1 - py0) <= MAX_PX:
            return zoom, px0, py0, max(1, px1 - px0), max(1, py1 - py0)
    raise AssertionError("unreachable")


def osm_vrt(tms_name: str, zoom: int, px0: int, py0: int, width: int, height: int) -> str:
    """Okno výřezu (EPSG:3857) nad dlaždicovou ``tms_name`` vedle souboru."""
    pixel = 2 * WEB_MERCATOR_HALF / (256 << zoom)
    ulx = -WEB_MERCATOR_HALF + px0 * pixel
    uly = WEB_MERCATOR_HALF - py0 * pixel
    lines = [
        f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">',
        "  <SRS>EPSG:3857</SRS>",
        f"  <GeoTransform>{ulx:.6f}, {pixel:.12f}, 0, {uly:.6f}, 0, {-pixel:.12f}</GeoTransform>",
    ]
    for band, color in ((1, "Red"), (2, "Green"), (3, "Blue")):
        lines += [
            f'  <VRTRasterBand dataType="Byte" band="{band}">',
            f"    <ColorInterp>{color}</ColorInterp>",
            "    <SimpleSource>",
            f'      <SourceFilename relativeToVRT="1">{html.escape(tms_name)}</SourceFilename>',
            f"      <SourceBand>{band}</SourceBand>",
            f'      <SrcRect xOff="{px0}" yOff="{py0}" xSize="{width}" ySize="{height}"/>',
            f'      <DstRect xOff="0" yOff="0" xSize="{width}" ySize="{height}"/>',
            "    </SimpleSource>",
            "  </VRTRasterBand>",
        ]
    lines.append("</VRTDataset>")
    return "\n".join(lines) + "\n"


def write_online_templates(
    bounds_5514: tuple[float, float, float, float],
    bbox_wgs84: tuple[float, float, float, float],
    out_dir: Path,
) -> dict[str, Path]:
    """Zapíše online šablony do ``out_dir`` (``references``); klíč ``online_<vrstva>`` → soubor."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for key, filename, _label, url, layer, fmt, transparent, mpp in ONLINE_WMS_LAYERS:
        path = out_dir / filename
        path.write_text(
            wms_xml(url, layer, bounds_5514, image_format=fmt, transparent=transparent, mpp=mpp),
            encoding="utf-8",
        )
        written[f"online_{key}"] = path
    key, filename, _label = ONLINE_OSM
    zoom, px0, py0, width, height = osm_window(bbox_wgs84)
    tms = out_dir / "osm_dlazdice.xml"
    tms.write_text(osm_tms_xml(zoom), encoding="utf-8")
    vrt = out_dir / filename
    vrt.write_text(osm_vrt(tms.name, zoom, px0, py0, width, height), encoding="utf-8")
    written[f"online_{key}"] = vrt
    return written


def online_labels() -> dict[str, str]:
    """Klíč ``online_<vrstva>`` → popisek šablony."""
    labels = {f"online_{k}": label for k, _f, label, *_rest in ONLINE_WMS_LAYERS}
    labels[f"online_{ONLINE_OSM[0]}"] = ONLINE_OSM[2]
    return labels


def wms_readme_lines() -> list[str]:
    """Adresy WMS pro ruční připojení (OCAD, QGIS): popis – URL – vrstva."""
    seen: list[str] = []
    for _key, _f, label, url, layer, *_rest in ONLINE_WMS_LAYERS:
        seen.append(f"- {label}: {url} (vrstva {layer}, S-JTSK EPSG:5514)")
    return seen
