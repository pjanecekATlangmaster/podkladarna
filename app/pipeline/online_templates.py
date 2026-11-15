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
from app.pipeline.reference_at import AT_FMZK_WMS_LAYERS, AT_WIEN_LB_TILE_URL, AT_WIEN_LB_ZOOM, AT_WIEN_WMS
from app.pipeline.reference_de import (
    DE_BE_ALKIS_LAYER,
    DE_BE_ALKIS_WMS,
    DE_BE_K5_LAYER,
    DE_BE_K5_WMS,
    DE_BE_TRUEDOP_LAYER,
    DE_BE_TRUEDOP_WMS,
)
from app.pipeline.reference_fr import FR_CADASTRE_LAYER, FR_ORTHO_LAYER, FR_PLAN_LAYER, FR_WMS_R
from app.pipeline.reference_ee import (
    EE_ALUS_WMS,
    EE_BASEMAP_LAYER,
    EE_CADASTRE_LAYER,
    EE_CADASTRE_WMS,
    EE_FOTO_WMS,
    EE_ORTHO_LAYER,
)
from app.pipeline.reference_be import BE_ADP_LAYER, BE_ADP_WMS, BE_GRB_LAYER, BE_GRB_WMS, BE_OMW_WMS, BE_ORTHO_LAYER
from app.pipeline.reference_it import TN_CTP_LAYER, TN_HILLSHADE_LAYER, TN_ORTHO_LAYER, TN_WMS
from app.pipeline.reference_ch import (
    CH_CADASTRAL_LAYER,
    CH_PIXELKARTE_LAYER,
    CH_RELIEF_LAYER,
    CH_SWISSIMAGE_LAYER,
    CH_WMS,
)
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
NO_DEFAULT_STYLE_HOSTS = ("dmvs.cuzk.gov.cz", "geoportal.cuzk.gov.cz", "data.wien.gv.at", "gdi.berlin.de",
                          "data.geopf.fr", "gsavalik.envir.ee", "siat.provincia.tn.it",
                          "geo.api.vlaanderen.be")

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
# Švýcarsko (swisstopo WMS v EPSG:2056, citace v ``configs/countries.yaml``): ortofoto, národní mapa, AV, stínování.
CH_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("ch_swissimage", "swissimage.xml", "SWISSIMAGE (ortofoto swisstopo)", CH_WMS, CH_SWISSIMAGE_LAYER,
     "image/jpeg", False, 0.1),
    ("ch_landeskarte", "landeskarte.xml", "Národní mapa swisstopo (Landeskarte)", CH_WMS, CH_PIXELKARTE_LAYER,
     "image/png", False, 1.0),
    ("ch_av", "av_katastr.xml", "AV – úřední vyměřování (Cadastral Web Map)", CH_WMS, CH_CADASTRAL_LAYER,
     "image/png", True, 0.25),
    ("ch_relief", "swissalti3d_relief.xml", "Stínování swissALTI3D", CH_WMS, CH_RELIEF_LAYER,
     "image/png", False, 0.5),
)
# Rakousko (Vídeň, Stadt Wien OGD): FMZK jako WMS; ortofoto jen WMTS 3857 (``AT_ONLINE_LB``).
# Server převádí do jiných systémů bez posunu data MGI (~290 m vedle): WMS v nativním systému + okno výřezu jako VRT.
AT_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("at_fmzk", "fmzk_wien.xml", "FMZK Wien (Flächen-Mehrzweckkarte)", AT_WIEN_WMS, AT_FMZK_WMS_LAYERS,
     "image/png", True, 0.25),
)
WMS_NATIVE_CRS = {"at_fmzk": "EPSG:31256"}
# CRS, jejichž osy jsou v EPSG sever–východ: WMS 1.3.0 chce BBOX v tomto pořadí (jinak prázdný obrázek), GDAL ho samo neotočí.
NORTHING_FIRST_CRS = frozenset({"EPSG:3301"})
# Německo (Berlín, GDI Berlin, dl-de/zero): TrueDOP 2024, mapa 1 : 5 000 (K5) a parcely ALKIS, vše EPSG:25833.
DE_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("de_truedop", "truedop_berlin.xml", "TrueDOP Berlín 2024 (ortofoto)", DE_BE_TRUEDOP_WMS, DE_BE_TRUEDOP_LAYER,
     "image/jpeg", False, 0.2),
    ("de_k5", "k5_berlin.xml", "Karte von Berlin 1 : 5 000 (K5)", DE_BE_K5_WMS, DE_BE_K5_LAYER, "image/png", False, 0.25),
    ("de_alkis", "alkis_berlin.xml", "ALKIS Berlín – parcely", DE_BE_ALKIS_WMS, DE_BE_ALKIS_LAYER, "image/png", True, 0.25),
)
# Francie (Paříž, IGN Géoplateforme WMS-R, Etalab 2.0): ortofoto, Plan IGN v2 a katastr (PCI), CRS jobu Lambert-93.
FR_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("fr_ortho", "ortho_ign.xml", "Ortofoto IGN (BD ORTHO)", FR_WMS_R, FR_ORTHO_LAYER, "image/jpeg", False, 0.2),
    ("fr_plan", "plan_ign.xml", "Plan IGN v2", FR_WMS_R, FR_PLAN_LAYER, "image/png", False, 0.5),
    ("fr_cadastre", "cadastre_ign.xml", "Parcellaire Express (katastr)", FR_WMS_R, FR_CADASTRE_LAYER, "image/png", True,
     0.25),
)
# Estonsko (Maa- ja Ruumiamet, otevřená licence): ortofoto, základní mapa 1 : 10 000 a katastr, CRS jobu L-EST97.
EE_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("ee_ortho", "ortho_maaamet.xml", "Ortofoto Maa- ja Ruumiamet", EE_FOTO_WMS, EE_ORTHO_LAYER, "image/jpeg", False, 0.25),
    ("ee_basemap", "pohikaart.xml", "Eesti põhikaart 1 : 10 000", EE_ALUS_WMS, EE_BASEMAP_LAYER, "image/png", False, 1.0),
    ("ee_cadastre", "kataster_ee.xml", "Katastr (katastriüksused)", EE_CADASTRE_WMS, EE_CADASTRE_LAYER, "image/png", True,
     0.25),
)
IT_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("it_ortho", "ortofoto_trento.xml", "Ortofoto 2015 (Provincia di Trento)", TN_WMS, TN_ORTHO_LAYER, "image/jpeg", False,
     0.25),
    ("it_ctp", "ctp_trento.xml", "Carta Tecnica Provinciale 2025 (1 : 10 000)", TN_WMS, TN_CTP_LAYER, "image/png", False,
     0.5),
    ("it_hillshade", "rilievo_trento.xml", "Stínování LiDAR (Provincia di Trento)", TN_WMS, TN_HILLSHADE_LAYER, "image/png",
     False, 1.0),
)
BE_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("be_ortho", "orthofoto_vl.xml", "Orthofoto Vlaanderen (zima 2025)", BE_OMW_WMS, BE_ORTHO_LAYER, "image/jpeg", False,
     0.25),
    ("be_grb", "grb_basiskaart.xml", "GRB-basiskaart", BE_GRB_WMS, BE_GRB_LAYER, "image/png", False, 0.25),
    ("be_cadastre", "kadaster_vl.xml", "Katastr (administratieve percelen)", BE_ADP_WMS, BE_ADP_LAYER, "image/png", True,
     0.25),
)
AT_ONLINE_LB = ("at_luftbild", "luftbild_wien.vrt", "Luftbild Wien (ortofoto Stadt Wien)")
AT_LB_MPP = 0.15
# BGT je veřejně jen WMTS v mřížce RD (EPSG:28992): dlaždice přes GDAL TMS + okno výřezu jako VRT.
NL_ONLINE_BGT = ("nl_bgt", "bgt.vrt", "BGT (velkoměřítková mapa)")
NL_BGT_LEVEL = 15  # 0,105 m/px
NL_BGT_MPP = 0.25


def wms_layers_for(country: str) -> tuple[tuple[str, str, str, str, str, str, bool, float], ...]:
    """Online WMS vrstvy země výřezu (Česko ČÚZK, Nizozemsko PDOK, jinde jen OSM)."""
    return {
        "CZ": ONLINE_WMS_LAYERS,
        "NL": NL_ONLINE_WMS_LAYERS,
        "CH": CH_ONLINE_WMS_LAYERS,
        "AT": AT_ONLINE_WMS_LAYERS,
        "DE": DE_ONLINE_WMS_LAYERS,
        "FR": FR_ONLINE_WMS_LAYERS,
        "EE": EE_ONLINE_WMS_LAYERS,
        "IT": IT_ONLINE_WMS_LAYERS,
        "BE": BE_ONLINE_WMS_LAYERS,
    }.get(country, ())


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
    crs: str = CRS_LABEL,
) -> str:
    """GDAL WMS popis jedné vrstvy ve výřezu (souřadnice jobu, nebo ``crs`` služby; WMS 1.3.0)."""
    xmin, ymin, xmax, ymax = bounds_5514
    sx, sy = _size(xmax - xmin, ymax - ymin, mpp)
    styles = "" if any(host in url for host in NO_DEFAULT_STYLE_HOSTS) else "default"
    sep = "&" if "?" in url else "?"
    return (
        "<GDAL_WMS>\n"
        '  <Service name="WMS">\n'
        "    <Version>1.3.0</Version>\n"
        f"    <ServerUrl>{html.escape(url + sep)}</ServerUrl>\n"
        f"    <CRS>{crs}</CRS>\n"
        f"    <ImageFormat>{image_format}</ImageFormat>\n"
        f"    <Transparent>{'TRUE' if transparent else 'FALSE'}</Transparent>\n"
        f"    <Layers>{html.escape(layer)}</Layers>\n"
        f"    <Styles>{styles}</Styles>\n"
        + ("    <BBoxOrder>yxYX</BBoxOrder>\n" if crs in NORTHING_FIRST_CRS else "")
        + "  </Service>\n"
        "  <DataWindow>\n"
        f"    <UpperLeftX>{xmin:.2f}</UpperLeftX>\n"
        f"    <UpperLeftY>{ymax:.2f}</UpperLeftY>\n"
        f"    <LowerRightX>{xmax:.2f}</LowerRightX>\n"
        f"    <LowerRightY>{ymin:.2f}</LowerRightY>\n"
        f"    <SizeX>{sx}</SizeX>\n"
        f"    <SizeY>{sy}</SizeY>\n"
        "  </DataWindow>\n"
        f"  <Projection>{crs}</Projection>\n"
        f"  <BandsCount>{4 if transparent else 3}</BandsCount>\n"
        f"  <BlockSizeX>{BLOCK_PX}</BlockSizeX>\n"
        f"  <BlockSizeY>{BLOCK_PX}</BlockSizeY>\n"
        "  <MaxConnections>4</MaxConnections>\n"
        f"  <UserAgent>{html.escape(USER_AGENT)}</UserAgent>\n"
        "  <ZeroBlockHttpCodes>204,404</ZeroBlockHttpCodes>\n"
        "</GDAL_WMS>\n"
    )


def _native_bounds(bounds: tuple[float, float, float, float], crs: str, margin_m: float = 20.0) -> tuple[float, float, float, float]:
    """Okno výřezu (souřadnice jobu) jako obálka v systému služby ``crs`` s rezervou."""
    from pyproj import Transformer

    xmin, ymin, xmax, ymax = Transformer.from_crs(CRS_LABEL, crs, always_xy=True).transform_bounds(*bounds, densify_pts=21)
    return (xmin - margin_m, ymin - margin_m, xmax + margin_m, ymax + margin_m)


def osm_tms_xml(zoom: int, url: str = OSM_TILE_URL) -> str:
    """Celosvětová dlaždicová služba v EPSG:3857 (výchozí OSM) do úrovně ``zoom``."""
    h = WEB_MERCATOR_HALF
    return (
        "<GDAL_WMS>\n"
        '  <Service name="TMS">\n'
        f"    <ServerUrl>{html.escape(url)}</ServerUrl>\n"
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
    if country == "AT" and (only is None or AT_ONLINE_LB[0] in only):
        key, filename, _label = AT_ONLINE_LB
        tms = out_dir / "luftbild_wien_dlazdice.xml"
        tms.write_text(osm_tms_xml(AT_WIEN_LB_ZOOM, AT_WIEN_LB_TILE_URL), encoding="utf-8")
        for suffix, factor, _lsuffix in ZOOM_VARIANTS:
            vrt = out_dir / f"{Path(filename).stem}{suffix}.vrt"
            if osm_sjtsk_vrt(tms, vrt, bounds_5514, AT_LB_MPP * factor):
                written[f"online_{key}{suffix}"] = vrt
    for key, filename, _label, url, layer, fmt, transparent, mpp in wms_layers_for(country):
        if only is not None and key not in only:
            continue
        native = WMS_NATIVE_CRS.get(key)
        for suffix, factor, _lsuffix in ZOOM_VARIANTS:
            if native:
                src = out_dir / f"{Path(filename).stem}{suffix}_zdroj.xml"
                src.write_text(
                    wms_xml(url, layer, _native_bounds(bounds_5514, native), image_format=fmt, transparent=transparent,
                            mpp=mpp * factor, crs=native),
                    encoding="utf-8",
                )
                vrt = out_dir / f"{Path(filename).stem}{suffix}.vrt"
                if osm_sjtsk_vrt(src, vrt, bounds_5514, mpp * factor):
                    written[f"online_{key}{suffix}"] = vrt
                else:
                    src.unlink(missing_ok=True)
                continue
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


# Fotografické podklady do JPEG (menší ZIP), mapy a průhledné vrstvy do PNG.
PHOTO_TMS_KEYS = frozenset({AT_ONLINE_LB[0]})
DOWNLOAD_WORKERS = 3


def _download_format(key: str, country: str) -> str:
    """``JPEG`` u ortofota (WMS ``image/jpeg`` nebo dlaždice snímků), jinak ``PNG``."""
    base = key[len("online_"):]
    if base in PHOTO_TMS_KEYS:
        return "JPEG"
    for k, _f, _label, _url, _layer, fmt, transparent, _mpp in wms_layers_for(country):
        if k == base:
            return "JPEG" if fmt == "image/jpeg" and not transparent else "PNG"
    return "PNG"


def _translate_template(src: Path, fmt: str, log=None) -> Path | None:
    """Online šablona (GDAL WMS ``.xml`` / ``.vrt``) → stažený rastr + world file vedle ní; None při chybě."""
    import subprocess

    ext, wext = (".jpg", ".jgw") if fmt == "JPEG" else (".png", ".pgw")
    dest = src.with_name(f"{src.stem}_stazeno{ext}")
    try:
        gdal_translate = find_tool("gdal_translate")
    except RuntimeError:
        return None
    cmd = [gdal_translate, "-q", "-of", fmt, "-co", "WORLDFILE=YES"]
    if fmt == "JPEG":
        cmd += ["-b", "1", "-b", "2", "-b", "3", "-co", "QUALITY=90"]
    cmd += [src.name, dest.name]
    try:
        result = subprocess.run(cmd, cwd=src.parent, capture_output=True, text=True, timeout=900, check=False)
    except subprocess.TimeoutExpired:
        result = None
    wld = dest.with_suffix(".wld")
    if result is None or result.returncode != 0 or not dest.is_file() or not wld.is_file():
        for leftover in (dest, wld, dest.with_name(dest.name + ".aux.xml")):
            leftover.unlink(missing_ok=True)
        if log and result is not None:
            log(f"  {src.name}: stažení selhalo ({(result.stderr or '').strip()[:200]})")
        return None
    wld.replace(dest.with_suffix(wext))
    dest.with_name(dest.name + ".aux.xml").unlink(missing_ok=True)
    final = src.with_name(f"{src.stem}{ext}")
    dest.replace(final)
    dest.with_suffix(wext).replace(final.with_suffix(wext))
    return final


def download_online_templates(written: dict[str, Path], country: str, *, log=None) -> dict[str, Path]:
    """Se zapnutým „Stáhnout referenční šablony předem“: online šablony mimo Česko stáhne jako rastry.

    Stahuje se varianta s nejjemnějším rozlišením (nejvýš ``MAX_PX``); povedený rastr nahradí obě varianty
    vrstvy (soubory ``.xml`` / ``.vrt`` se smažou), při chybě zůstanou online šablony.
    """
    from concurrent.futures import ThreadPoolExecutor

    main = {k: p for k, p in written.items() if not any(k.endswith(sfx) for sfx, _f, _l in ZOOM_VARIANTS if sfx)}
    jobs = [(k, p, _download_format(k, country)) for k, p in sorted(main.items())]
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS, thread_name_prefix="ref-online") as ex:
        rasters = list(ex.map(lambda j: _translate_template(j[1], j[2], log=log), jobs))
    out = dict(written)
    for (key, _src, _fmt), raster in zip(jobs, rasters):
        if raster is None:
            continue
        for sfx, _f, _l in ZOOM_VARIANTS:
            old = out.pop(f"{key}{sfx}", None)
            if old is not None and old != raster:
                old.unlink(missing_ok=True)
                old.with_name(f"{old.stem}_zdroj.xml").unlink(missing_ok=True)
        out[key] = raster
    used = {p.name for p in out.values()}
    for tms in ("bgt_dlazdice.xml", "luftbild_wien_dlazdice.xml", "osm_dlazdice.xml"):
        folder = next(iter(written.values())).parent if written else None
        if folder is None:
            break
        # dlaždicová šablona zůstává, jen dokud ji používá některý .vrt
        if not any(n.endswith(".vrt") for n in used) or not any(
            tms in (folder / n).read_text(encoding="utf-8", errors="ignore") for n in used if n.endswith(".vrt")
        ):
            (folder / tms).unlink(missing_ok=True)
    return out


def online_labels() -> dict[str, str]:
    """Klíč ``online_<vrstva>`` → popisek šablony."""
    layers = [
        (k, label)
        for k, _f, label, *_rest in (
            *ONLINE_WMS_LAYERS, *NL_ONLINE_WMS_LAYERS, *CH_ONLINE_WMS_LAYERS, *AT_ONLINE_WMS_LAYERS,
            *DE_ONLINE_WMS_LAYERS, *FR_ONLINE_WMS_LAYERS, *EE_ONLINE_WMS_LAYERS, *IT_ONLINE_WMS_LAYERS,
            *BE_ONLINE_WMS_LAYERS,
        )
    ] + [(NL_ONLINE_BGT[0], NL_ONLINE_BGT[2]), (AT_ONLINE_LB[0], AT_ONLINE_LB[2]), (ONLINE_OSM[0], ONLINE_OSM[2])]
    return {
        f"online_{k}{suffix}": f"{label}{lsuffix}"
        for k, label in layers
        for suffix, _factor, lsuffix in ZOOM_VARIANTS
    }


def wms_readme_lines(country: str = "CZ") -> list[str]:
    """Adresy WMS pro ruční připojení (OCAD, QGIS): popis – URL – vrstva."""
    crs = "S-JTSK EPSG:5514" if country == "CZ" else CRS_LABEL
    seen: list[str] = []
    for key, _f, label, url, layer, *_rest in wms_layers_for(country):
        native = WMS_NATIVE_CRS.get(key)
        seen.append(f"- {label}: {url} (vrstva {layer}, {f'{native}, jiný systém server posune' if native else crs})")
    if country == "NL":
        seen.append(f"- {NL_ONLINE_BGT[2]}: WMTS {NL_BGT_TILE_URL.split('/standaard')[0]} (standaardvisualisatie, EPSG:28992)")
    if country == "AT":
        seen.append(f"- {AT_ONLINE_LB[2]}: WMTS {AT_WIEN_LB_TILE_URL.split('/farbe')[0]} (styl farbe, EPSG:3857)")
    return seen
