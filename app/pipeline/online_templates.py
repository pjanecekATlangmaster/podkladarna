"""Online šablony podkladů pro OOM (Petr 2026-11-08): malé soubory GDAL WMS ``.xml`` místo stažených rastrů.

Mapper (``GdalTemplate``) si podklad stáhne až při zapnutí šablony, ZIP zůstává malý. Každý soubor pokrývá
výřez mapy v pevném rozlišení (nejvýš ``MAX_PX`` px na delší straně – Mapper načte celý rastr do paměti).
OSM je dlaždicová služba v EPSG:3857: celosvětová TMS ``.xml`` a ``.vrt`` převedený gdalwarp do S-JTSK
(Mapper okno v EPSG:3857 nenačte, Petr 2026-11-08); GDAL si z TMS vezme úroveň podle rozlišení.
OCAD ``.xml`` neotevře – adresy WMS jsou v README_OOM.
"""

from __future__ import annotations

import html
from pathlib import Path

from app.pipeline.crs_5514 import CRS_LABEL, CRS_PROJ4
from app.pipeline.fetch_openzu import USER_AGENT
from app.pipeline.prepare_lidar import find_tool, run_cmd
from app.pipeline.reference_nl import (
    NL_AHN_WMS,
    NL_BGT_TILE_URL,
    NL_KADASTER_LAYER,
    NL_KADASTER_WMS,
    NL_LUCHTFOTO_LAYER,
    NL_LUCHTFOTO_WMS,
    NL_TOP10NL_LAYER,
    NL_TOP10NL_WMS,
    RD_TILE_ORIGIN,
    RD_TILE_RES0,
)
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
    ORTOFOTO_ARCHIV_LAYERS,
    ORTOFOTO_ARCHIV_WMS,
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
# OSM maximální zoom 1 m/px (dlaždice z17): 0,5 m (z18, ~400 dlaždic) Mapper u Petra nenačetl (2026-11-08).
OSM_MPP = 1.0
# Dvě varianty každé vrstvy (Petr 2026-11-08): maximální zoom (nejjemnější rozlišení do ``MAX_PX``) a střední
# (4× hrubší, načte se rychle). Mapper šablonu nezoomuje – načte jeden obrázek v pevném rozlišení.
# (přípona klíče a souboru, násobek m/px, přípona popisku)
ZOOM_VARIANTS: tuple[tuple[str, float, str], ...] = (
    ("", 1.0, ""),
    ("_stredni", 4.0, " (střední zoom)"),
)

# DMVS WMS styl „default“ odmítá (500) – stejně jako ``_wms_getmap_url``; archiv ortofota (geoportal) chce prázdný styl.
NO_DEFAULT_STYLE_HOSTS = ("dmvs.cuzk.gov.cz", "geoportal.cuzk.gov.cz")

# Vrstvy jen online (bez staženého rastru): přidají se i se zapnutým „Stáhnout referenční šablony předem“.
ONLINE_ONLY_KEYS = frozenset({"orthophoto_archiv"})

# (klíč, soubor, popis pro README, WMS, vrstva, formát, průhledná, m/px)
ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("orthophoto", "ortofoto_cuzk.xml", "Ortofoto ČÚZK", ORTOFOTO_WMS, "0", "image/jpeg", False, 0.125),
    (
        "orthophoto_archiv", "ortofoto_archiv.xml", "Archivní ortofoto ČÚZK 2003–2005",
        ORTOFOTO_ARCHIV_WMS, ORTOFOTO_ARCHIV_LAYERS, "image/png", True, 0.5,
    ),
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

# Nizozemsko (PDOK, citace v ``configs/countries.yaml``): ortofoto, TOP10NL, katastr a barevný výškopis AHN.
NL_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("nl_luchtfoto", "luchtfoto_pdok.xml", "Luchtfoto PDOK (ortofoto)", NL_LUCHTFOTO_WMS, NL_LUCHTFOTO_LAYER,
     "image/jpeg", False, 0.08),
    ("nl_top10nl", "top10nl.xml", "BRT TOP10NL (topografická mapa)", NL_TOP10NL_WMS, NL_TOP10NL_LAYER,
     "image/png", False, 1.0),
    ("nl_kadaster", "kadastrale_kaart.xml", "Kadastrale kaart (katastr)", NL_KADASTER_WMS, NL_KADASTER_LAYER,
     "image/png", True, 0.25),
    ("nl_ahn", "ahn_dtm.xml", "AHN – výškový model terénu (barevně)", NL_AHN_WMS,
     "dtm_05m", "image/png", False, 0.5),
)
# BGT je veřejně jen WMTS v mřížce RD (EPSG:28992): dlaždice přes GDAL TMS + okno výřezu jako VRT.
NL_ONLINE_BGT = ("nl_bgt", "bgt.vrt", "BGT (velkoměřítková mapa)")
NL_BGT_LEVEL = 15  # 0,105 m/px
NL_BGT_MPP = 0.25


def wms_layers_for(country: str) -> tuple[tuple[str, str, str, str, str, str, bool, float], ...]:
    """Online WMS vrstvy země výřezu (Česko ČÚZK, Nizozemsko PDOK, jinde jen OSM)."""
    return {"CZ": ONLINE_WMS_LAYERS, "NL": NL_ONLINE_WMS_LAYERS}.get(country, ())


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
    """GDAL WMS popis jedné vrstvy ve výřezu (souřadnice jobu, WMS 1.3.0)."""
    xmin, ymin, xmax, ymax = bounds_5514
    sx, sy = _size(xmax - xmin, ymax - ymin, mpp)
    styles = "" if any(host in url for host in NO_DEFAULT_STYLE_HOSTS) else "default"
    sep = "&" if "?" in url else "?"
    return (
        "<GDAL_WMS>\n"
        '  <Service name="WMS">\n'
        "    <Version>1.3.0</Version>\n"
        f"    <ServerUrl>{html.escape(url + sep)}</ServerUrl>\n"
        f"    <CRS>{CRS_LABEL}</CRS>\n"
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
        f"  <Projection>{CRS_LABEL}</Projection>\n"
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
        "  <MaxConnections>4</MaxConnections>\n"
        "  <Timeout>60</Timeout>\n"
        f"  <UserAgent>{html.escape(USER_AGENT)}</UserAgent>\n"
        "  <ZeroBlockHttpCodes>204,404</ZeroBlockHttpCodes>\n"
        "</GDAL_WMS>\n"
    )


def rd_tms_xml(url: str, level: int) -> str:
    """Dlaždice WMTS v nizozemské mřížce RD (EPSG:28992, 256 px, počátek vlevo nahoře) jako GDAL TMS."""
    x0, y0 = RD_TILE_ORIGIN
    span = RD_TILE_RES0 * 256
    return (
        "<GDAL_WMS>\n"
        '  <Service name="TMS">\n'
        f"    <ServerUrl>{html.escape(url)}</ServerUrl>\n"
        "  </Service>\n"
        "  <DataWindow>\n"
        f"    <UpperLeftX>{x0}</UpperLeftX>\n"
        f"    <UpperLeftY>{y0}</UpperLeftY>\n"
        f"    <LowerRightX>{x0 + span}</LowerRightX>\n"
        f"    <LowerRightY>{y0 - span}</LowerRightY>\n"
        f"    <TileLevel>{level}</TileLevel>\n"
        "    <TileCountX>1</TileCountX>\n"
        "    <TileCountY>1</TileCountY>\n"
        "    <YOrigin>top</YOrigin>\n"
        "  </DataWindow>\n"
        "  <Projection>EPSG:28992</Projection>\n"
        "  <BlockSizeX>256</BlockSizeX>\n"
        "  <BlockSizeY>256</BlockSizeY>\n"
        "  <BandsCount>4</BandsCount>\n"
        "  <MaxConnections>4</MaxConnections>\n"
        "  <Timeout>60</Timeout>\n"
        f"  <UserAgent>{html.escape(USER_AGENT)}</UserAgent>\n"
        "  <ZeroBlockHttpCodes>204,404</ZeroBlockHttpCodes>\n"
        "</GDAL_WMS>\n"
    )


def osm_sjtsk_vrt(
    tms: Path, vrt: Path, bounds_5514: tuple[float, float, float, float], mpp: float = OSM_MPP
) -> bool:
    """gdalwarp ``-of VRT``: okno výřezu v S-JTSK nad TMS (nic nestahuje, zdroj relativně k VRT)."""
    xmin, ymin, xmax, ymax = bounds_5514
    sx, sy = _size(xmax - xmin, ymax - ymin, mpp)
    try:
        gdalwarp = find_tool("gdalwarp")
    except RuntimeError:
        return False
    cmd = [
        gdalwarp, "-q", "-overwrite", "-of", "VRT", "-t_srs", CRS_PROJ4,
        "-te", f"{xmin:.2f}", f"{ymin:.2f}", f"{xmax:.2f}", f"{ymax:.2f}",
        "-ts", str(sx), str(sy), "-r", "bilinear", tms.name, vrt.name,
    ]
    result = run_cmd(cmd, cwd=vrt.parent)
    return result.returncode == 0 and vrt.is_file()


def write_online_templates(
    bounds_5514: tuple[float, float, float, float],
    out_dir: Path,
    *,
    only: frozenset[str] | None = None,
    country: str = "CZ",
) -> dict[str, Path]:
    """Zapíše online šablony do ``out_dir`` (``references``); klíč ``online_<vrstva>`` → soubor.

    ``only``: jen vybrané vrstvy (např. ``ONLINE_ONLY_KEYS`` vedle stažených rastrů), OSM jen je-li v něm.
    ``country``: země výřezu – místní služby (Česko ČÚZK, Nizozemsko PDOK); OSM všude.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    if country == "NL" and (only is None or NL_ONLINE_BGT[0] in only):
        key, filename, _label = NL_ONLINE_BGT
        tms = out_dir / "bgt_dlazdice.xml"
        tms.write_text(rd_tms_xml(NL_BGT_TILE_URL, NL_BGT_LEVEL), encoding="utf-8")
        for suffix, factor, _lsuffix in ZOOM_VARIANTS:
            vrt = out_dir / f"{Path(filename).stem}{suffix}.vrt"
            if osm_sjtsk_vrt(tms, vrt, bounds_5514, NL_BGT_MPP * factor):
                written[f"online_{key}{suffix}"] = vrt
    for key, filename, _label, url, layer, fmt, transparent, mpp in wms_layers_for(country):
        if only is not None and key not in only:
            continue
        for suffix, factor, _lsuffix in ZOOM_VARIANTS:
            path = out_dir / f"{Path(filename).stem}{suffix}.xml"
            path.write_text(
                wms_xml(url, layer, bounds_5514, image_format=fmt, transparent=transparent, mpp=mpp * factor),
                encoding="utf-8",
            )
            written[f"online_{key}{suffix}"] = path
    key, filename, _label = ONLINE_OSM
    if only is not None and key not in only:
        return written
    tms = out_dir / "osm_dlazdice.xml"
    tms.write_text(osm_tms_xml(OSM_MAX_ZOOM), encoding="utf-8")
    for suffix, factor, _lsuffix in ZOOM_VARIANTS:
        vrt = out_dir / f"{Path(filename).stem}{suffix}.vrt"
        if osm_sjtsk_vrt(tms, vrt, bounds_5514, OSM_MPP * factor):
            written[f"online_{key}{suffix}"] = vrt
    if not any(k.startswith(f"online_{key}") for k in written):
        tms.unlink(missing_ok=True)  # bez gdalwarp OSM online nebude (okno v EPSG:3857 Mapper nenačte)
    return written


def online_labels() -> dict[str, str]:
    """Klíč ``online_<vrstva>`` → popisek šablony."""
    layers = [
        (k, label) for k, _f, label, *_rest in (*ONLINE_WMS_LAYERS, *NL_ONLINE_WMS_LAYERS)
    ] + [(NL_ONLINE_BGT[0], NL_ONLINE_BGT[2]), (ONLINE_OSM[0], ONLINE_OSM[2])]
    return {
        f"online_{k}{suffix}": f"{label}{lsuffix}"
        for k, label in layers
        for suffix, _factor, lsuffix in ZOOM_VARIANTS
    }


def wms_readme_lines(country: str = "CZ") -> list[str]:
    """Adresy WMS pro ruční připojení (OCAD, QGIS): popis – URL – vrstva."""
    crs = "S-JTSK EPSG:5514" if country == "CZ" else CRS_LABEL
    seen: list[str] = []
    for _key, _f, label, url, layer, *_rest in wms_layers_for(country):
        seen.append(f"- {label}: {url} (vrstva {layer}, {crs})")
    if country == "NL":
        seen.append(f"- {NL_ONLINE_BGT[2]}: WMTS {NL_BGT_TILE_URL.split('/standaard')[0]} (standaardvisualisatie, EPSG:28992)")
    return seen
