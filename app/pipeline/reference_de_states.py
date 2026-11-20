"""Německé spolkové země s výškami z WCS 2.0.1 (zahraniční build): Sasko-Anhaltsko, Meklenbursko-Přední Pomořansko,
Dolní Sasko, Braniborsko, Porýní-Falc (dlaždice 1 km podle indexu Metalink místo WCS) a Bádensko-Württembersko
(ZIP po 2 km: terén jako text XYZ, výška porostu nDOM jako GeoTIFF, převod a cache na serveru).

Jedna tabulka ``DE_WCS_STATES`` (klíč = ``elevation`` v ``configs/countries.yaml``): terén DGM1 a povrch DOM1 po 1 m
z WCS zemského geodetického úřadu, bez klíče, v nativním UTM země; šablony WMS (ortofoto, topografická mapa).
Výšky se stahují po dlaždicích nejvýš 2 × 2 km (``elevation_raster.de_wcs_provider``). Některé služby (MapServer
Sasko-Anhaltska) vracejí GeoTIFF jen jako multipart (GML + TIFF); ``elevation_raster._download`` TIFF vyjme.

Adresy a licence ověřeny z cloudu (GetCapabilities, podmínky poskytovatele, zkušební dlaždice); data se jen čtou,
žádný kód odjinud se nepřebírá. Severní Porýní-Vestfálsko (nDOM ze snímků) a Bavorsko (dlaždice) mají vlastní moduly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeWcsState:
    code: str                    # kód v countries.yaml (DE-ST …)
    name: str                    # česky do logu
    crs: str                     # nativní CRS služby (osy východ–sever)
    dtm: tuple[str, str]         # (URL WCS, COVERAGEID) terénu DGM1; u ``meta4`` (URL indexu Metalink, předpona souboru)
    dsm: tuple[str, str]         # totéž pro povrch DOM1
    probe_box: tuple[float, float, float, float]  # malý výřez pro sondu stavu (16 × 16 m)
    wms: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = ()  # šablony jako ``online_templates``
    tiles: str = "wcs"           # wcs = GetCoverage po 2 km | meta4 = GeoTIFF po 1 km podle indexu Metalink
                                 # | bw_zip = ZIP po 2 km (``dtm`` XYZ, ``dsm`` = nDOM, výška nad terénem), cache
                                 # | sn_zip = ZIP po 2 km s jedním GeoTIFF (DGM1 i DOM1 absolutně), cache
                                 # | zip_range = GeoTIFF po 1 km čtené rozsahy z velkých ZIPů (``dtm`` = (šablona
                                 #   URL s ``{part}``, předpona souboru), díly v ``parts``)
                                 # | sh_geojson = dlaždice 1 km podle GeoJSON indexu (``dtm`` XYZ, ``dsm`` bDOM 0,2 m),
                                 #   převod a cache na serveru (Šlesvicko-Holštýnsko)
    parts: tuple[str, ...] = ("",)  # díly ZIPů u ``zip_range`` (okresy Sárska)
    axes: tuple[str, str] = ("x", "y")  # popisky os WCS v SUBSET (Hesensko „E N“)


ST_WCS = "https://www.geodatenportal.sachsen-anhalt.de"
ST_WSS = f"{ST_WCS}/wss/service"
ST_DOP_WMS = f"{ST_WSS}/ST_LVermGeo_DOP_WMS_OpenData/guest"
ST_DOP_LAYER = "lsa_lvermgeo_dop20_2"
ST_DTK_WMS = f"{ST_WSS}/ST_LVermGeo_DTK_WMS_OpenData/guest"
ST_DTK_LAYER = "lsa_lvermgeo_dtk25_col_1"
MV_DIENSTE = "https://www.geodaten-mv.de/dienste"
NI_WCS = "https://opendata.geoservices.lgln.niedersachsen.de"
NI_WMS = "https://opendata.lgln.niedersachsen.de/doorman/noauth"
BB_OWS = "https://isk.geobasis-bb.de"
RP_DATA = "https://geobasis-rlp.de/data"
RP_WMS = "https://geo4.service24.rlp.de/wms"
BW_DATA = "https://opengeodata.lgl-bw.de/data"
BW_OWS = "https://owsproxy.lgl-bw.de/owsproxy/ows"
SN_GEOCLOUD = "https://geocloud.landesvermessung.sachsen.de/public.php/dav/files"
SN_WMS = "https://geodienste.sachsen.de"
HE_WCS = "https://inspire-hessen.de/raster"
HE_WMS = "https://www.gds-srv.hessen.de/cgi-bin/lika-services"
SL_DAV = "https://www.shop.lvgl.saarland.de/cloud/public.php/dav/files/NK8ndP55qAqGEZD"
SL_WMS = "https://geoportal.saarland.de/freewms"
SL_COUNTIES = ("MZG", "NK", "SB", "SLS", "SPK", "WND")  # okresy (Landkreise), jeden ZIP na okres
HH_ARCHIVE = "https://archiv.transparenz.hamburg.de/hmbtgarchive/HMDK"
HH_DATA = "https://www.daten-hamburg.de/opendata"
HB_DATA = "https://gdi2.geo.bremen.de/inspire/download"
HB_WMS = "https://geodienste.bremen.de/wms_dop_lb"
SH_DL = "https://geodaten.schleswig-holstein.de/gaialight-sh/_apps/dladownload/single.php"
SH_WMS = "https://dienste.gdi-sh.de/WMS_SH_DOP20col_OpenGBD"

# Sasko-Anhaltsko (LVermGeo ST): Datenlizenz Deutschland – Namensnennung 2.0, poskytovatel „© GeoBasis-DE / LVermGeo ST“
# (Nutzungsbedingungen v5.0, 1. 3. 2024, bod 2). WCS ``ows_WCS_ST_DGM1`` / ``ows_WCS_ST_DOM1`` (coverage ``Coverage1``,
# EPSG:25832, 1 m, Float32, multipart), DOP20 a DTK25 přes WMS „OpenData/guest“.
DE_WCS_STATES: dict[str, DeWcsState] = {
    "de_st": DeWcsState(
        code="DE-ST",
        name="Sasko-Anhaltsko",
        crs="EPSG:25832",
        dtm=(f"{ST_WCS}/ows_WCS_ST_DGM1", "Coverage1"),
        dsm=(f"{ST_WCS}/ows_WCS_ST_DOM1", "Coverage1"),
        probe_box=(623000, 5743000, 623016, 5743016),  # Wernigerode
        wms=(
            ("de_st_dop", "dop_sachsen_anhalt.xml", "DOP20 Sachsen-Anhalt (ortofoto)", ST_DOP_WMS, ST_DOP_LAYER,
             "image/jpeg", False, 0.2),
            ("de_st_dtk", "dtk25_sachsen_anhalt.xml", "DTK25 Sachsen-Anhalt (topografická mapa 1 : 25 000)", ST_DTK_WMS,
             ST_DTK_LAYER, "image/png", False, 1.0),
            ("de_st_alkis", "alkis_sachsen_anhalt.xml", "ALKIS Sachsen-Anhalt – parcely",
             f"{ST_WSS}/ST_LVermGeo_ALKIS_WMS_AdV_konform_App/guest", "adv_alkis_flurstuecke", "image/png", True, 0.25),
        ),
    ),
    # Meklenbursko-Přední Pomořansko (LAiV M-V): CC BY 4.0 (metadata GeoMIS), citace „© GeoBasis-DE/M-V <rok>“
    # (AccessConstraints služeb). WCS dgm_wcs / dom_wcs (mv_dgm, mv_dom1), EPSG:25833, čistý GeoTIFF.
    "de_mv": DeWcsState(
        code="DE-MV",
        name="Meklenbursko-Přední Pomořansko",
        crs="EPSG:25833",
        dtm=(f"{MV_DIENSTE}/dgm_wcs", "mv_dgm"),
        dsm=(f"{MV_DIENSTE}/dom_wcs", "mv_dom1"),
        probe_box=(312000, 5998000, 312016, 5998016),  # Rostock
        wms=(
            ("de_mv_dop", "dop_mv.xml", "DOP Mecklenburg-Vorpommern (ortofoto)", f"{MV_DIENSTE}/adv_dop", "mv_dop",
             "image/jpeg", False, 0.2),
            ("de_mv_dtk", "dtk10_mv.xml", "DTK10 Mecklenburg-Vorpommern (topografická mapa 1 : 10 000)",
             f"{MV_DIENSTE}/gdimv_dtk", "mv_dtk10", "image/png", False, 0.5),
            ("de_mv_alkis", "alkis_mv.xml", "ALKIS Mecklenburg-Vorpommern – parcely", f"{MV_DIENSTE}/alkis_wms",
             "adv_alkis_flurstuecke", "image/png", True, 0.25),
        ),
    ),
    # Dolní Sasko (LGLN): CC BY 4.0, citace „LGLN (<rok>)“ (AccessConstraints služeb). WCS dgm_wcs / dom_wcs
    # (ni_dgm1, ni_dom1 z ALS od 2019), EPSG:25832, čistý GeoTIFF; DOP20 a DTK25 přes WMS „doorman/noauth“.
    "de_ni": DeWcsState(
        code="DE-NI",
        name="Dolní Sasko",
        crs="EPSG:25832",
        dtm=(f"{NI_WCS}/dgm_wcs", "ni_dgm1"),
        dsm=(f"{NI_WCS}/dom_wcs", "ni_dom1"),
        probe_box=(600000, 5750000, 600016, 5750016),  # Goslar
        wms=(
            ("de_ni_dop", "dop_niedersachsen.xml", "DOP20 Niedersachsen (ortofoto)", f"{NI_WMS}/dop_wms", "ni_dop20",
             "image/jpeg", False, 0.2),
            ("de_ni_dtk", "dtk25_niedersachsen.xml", "DTK25 Niedersachsen (topografická mapa 1 : 25 000)",
             f"{NI_WMS}/dtk25_wms", "ni_dtk25_farbe", "image/png", False, 1.0),
        ),
    ),
    # Braniborsko (LGB): dl-de/by-2.0, citace „© GeoBasis-DE/LGB, dl-de/by-2-0 (Daten geändert)“ (AccessConstraints).
    # WCS dgm_wcs / bdom_wcs (bb_dgm, bb_bdom), EPSG:25833, čistý GeoTIFF; povrch je **obrazový** bDOM (green: photo).
    "de_bb": DeWcsState(
        code="DE-BB",
        name="Braniborsko",
        crs="EPSG:25833",
        dtm=(f"{BB_OWS}/ows/dgm_wcs", "bb_dgm"),
        dsm=(f"{BB_OWS}/ows/bdom_wcs", "bb_bdom"),
        probe_box=(365000, 5805000, 365016, 5805016),  # Potsdam
        wms=(
            ("de_bb_dop", "dop_brandenburg.xml", "DOP20 Brandenburg (ortofoto)", f"{BB_OWS}/mapproxy/dop20c/service/wms",
             "bebb_dop20c", "image/jpeg", False, 0.2),
            ("de_bb_alkis", "alkis_brandenburg.xml", "ALKIS Brandenburg – parcely", f"{BB_OWS}/ows/alkis_wms",
             "adv_alkis_flurstuecke", "image/png", True, 0.25),
        ),
    ),
    # Porýní-Falc (LVermGeo RP): dl-de/by-2.0, citace „©GeoBasis-DE / LVermGeoRP (rok), dl-de/by-2-0“ (Fees WMS).
    # WCS ne: DGM1 a DOM1 (ALS) jako GeoTIFF po 1 km (EPSG:25832, název s rokem náletu) – index Metalink
    # ``…/current/meta4/*.meta4`` (~12 MB) se drží v cache a obnovuje po týdnu (``elevation_raster.rp_tile_index``).
    "de_rp": DeWcsState(
        code="DE-RP",
        name="Porýní-Falc",
        crs="EPSG:25832",
        dtm=(f"{RP_DATA}/dgm1/current/meta4/dgm1_tif_07.meta4", "dgm1"),
        dsm=(f"{RP_DATA}/dom1/current/meta4/dom1_tif_07.meta4", "dom1"),
        probe_box=(417000, 5485000, 417016, 5485016),  # Pfälzerwald
        wms=(
            ("de_rp_dop", "dop_rlp.xml", "DOP20 Rheinland-Pfalz (ortofoto)", f"{RP_WMS}/rp_dop20.fcgi", "rp_dop20",
             "image/jpeg", False, 0.2),
            ("de_rp_dtk", "dtk25_rlp.xml", "DTK25 Rheinland-Pfalz (topografická mapa 1 : 25 000)",
             f"{RP_WMS}/rp_dtk25.fcgi", "rp_dtk25", "image/png", False, 1.0),
        ),
        tiles="meta4",
    ),
    # Bádensko-Württembersko (LGL): dl-de/by-2.0, „Datenquelle: LGL, www.lgl-bw.de, dl-de/by-2-0“ (licence v ZIPu).
    # ZIP po 2 × 2 km (E liché, N sudé, EPSG:25832): DGM1 (ALS) 4 × XYZ po 1 km (~13 MB), nDOM1 4 × GeoTIFF po 1 km
    # z obrazového bDOM20 (léto, ~9 MB) → green: photo. Převedené dlaždice se drží v cache (``elevation_raster._bw_tile``).
    "de_bw": DeWcsState(
        code="DE-BW",
        name="Bádensko-Württembersko",
        crs="EPSG:25832",
        dtm=(f"{BW_DATA}/dgm", "dgm1"),
        dsm=(f"{BW_DATA}/ndom1", "ndom1"),
        probe_box=(489000, 5402000, 489016, 5402016),  # Schwarzwald u Freiburgu
        wms=(
            ("de_bw_dop", "dop_bw.xml", "DOP20 Baden-Württemberg (ortofoto)", f"{BW_OWS}/WMS_LGL-BW_ATKIS_DOP_20_C",
             "IMAGES_DOP_20_RGB", "image/jpeg", False, 0.2),
            ("de_bw_alkis", "alkis_bw.xml", "ALKIS Baden-Württemberg – parcely", f"{BW_OWS}/WMS_LGL-BW_ALKIS_Basis_transparent",
             "nora:ALKIS_Basis_transparent", "image/png", True, 0.25),
        ),
        tiles="bw_zip",
    ),
    # Sasko (GeoSN): dl-de/by-2.0, „Quelle: GeoSN, dl-de/by-2-0“ (Allgemeine Nutzungsbedingungen GeoSN). Dlaždice
    # Batch-Download (veřejné sdílení GeoCloud, jen GET – HEAD vrací 401): ZIP po 2 × 2 km (E i N sudé, EPSG:25833)
    # s jedním GeoTIFF 1 m (~14 MB), DGM1 i DOM1 z téhož ALS (Aktualität v CSV v ZIPu). DTK webové služby končí, jen DOP20.
    "de_sn": DeWcsState(
        code="DE-SN",
        name="Sasko",
        crs="EPSG:25833",
        dtm=(f"{SN_GEOCLOUD}/JCcXyifaNdLDnxZ", "dgm1"),
        dsm=(f"{SN_GEOCLOUD}/S6wwnFwX7882sZm", "dom1"),
        probe_box=(410000, 5656000, 410016, 5656016),  # Dresden
        wms=(
            ("de_sn_dop", "dop_sachsen.xml", "DOP20 Sachsen (ortofoto)", f"{SN_WMS}/wms_geosn_dop-rgb/guest",
             "sn_dop_020", "image/jpeg", False, 0.2),
            ("de_sn_alkis", "alkis_sachsen.xml", "ALKIS Sachsen – parcely", f"{SN_WMS}/wms_geosn_alkis-adv/guest",
             "adv_alkis_flurstuecke", "image/png", True, 0.25),
        ),
        tiles="sn_zip",
    ),
    # Hesensko (HVBG): Datenlizenz Deutschland – Zero 2.0 (Fees WCS), WMS „ohne Einschränkung“ (§ 18 HVGG: při uvedení
    # zdroje upozornit na změny). INSPIRE WCS 2.0.1 ``he_dgm1`` / ``dom1`` (EPSG:25832, osy „E N“, 1 m, GeoTIFF s CRS,
    # 2 × 2 km ~16 MB za ~2 s), DOM1 z ALS. DOP20 a DTK25 přes WMS „ogc-free“.
    "de_he": DeWcsState(
        code="DE-HE",
        name="Hesensko",
        crs="EPSG:25832",
        dtm=(f"{HE_WCS}/dgm1/ows", "he_dgm1"),
        dsm=(f"{HE_WCS}/dom1/ows", "dom1"),
        probe_box=(480000, 5560000, 480016, 5560016),  # Taunus u Frankfurtu
        wms=(
            ("de_he_dop", "dop_hessen.xml", "DOP20 Hessen (ortofoto)", f"{HE_WMS}/ogc-free-images.ows", "he_dop20_rgb",
             "image/jpeg", False, 0.2),
            ("de_he_dtk", "dtk25_hessen.xml", "DTK25 Hessen (topografická mapa 1 : 25 000)",
             f"{HE_WMS}/ogc-free-maps.ows", "he_dtk25", "image/png", False, 1.0),
            # he_alk_t má bílé čáry pro podklad z ortofota – na bílé mapě neviditelné, proto barevná Liegenschaftskarte
            ("de_he_alkis", "alk_hessen.xml", "Liegenschaftskarte Hessen (ALKIS)", f"{HE_WMS}/ogc-free-maps.ows", "he_alk",
             "image/png", True, 0.25),
        ),
        axes=("E", "N"),
    ),
    # Sársko (LVGL): dl-de/by-2.0, „© GeoBasis DE/LVGL-SL (Jahr)“ (metadata ATKIS DGM1 2025). Volná data jen jako ZIP
    # na okres (0,6–1,3 GB, veřejné sdílení Nextcloud „freiegeobasisdaten“), uvnitř GeoTIFF po 1 km (~1,8 MB, deflate).
    # Server podporuje Range → z ZIPu se čte jen adresář a potřebné dlaždice. DGM1 i DOM1 z ALS 2025.
    "de_sl": DeWcsState(
        code="DE-SL",
        name="Sársko",
        crs="EPSG:25832",
        dtm=(f"{SL_DAV}/OD_DGM1_2025_tif_LK/DGM1_tif_{{part}}_EPSG-25832_Entstehung-2025.zip", "dgm1"),
        dsm=(f"{SL_DAV}/OD_DOM1_2025_tif_LK/DOM1_tif_{{part}}_EPSG-25832_Entstehung-2025.zip", "dom1"),
        probe_box=(350000, 5475000, 350016, 5475016),  # Neunkirchen
        wms=(
            ("de_sl_dop", "dop_saarland.xml", "DOP20 Saarland (ortofoto)", f"{SL_WMS}/dop2025", "sl_dop20_rgb",
             "image/jpeg", False, 0.2),
        ),
        tiles="zip_range",
        parts=SL_COUNTIES,
    ),
    # Hamburk (LGV): dl-de/by-2.0, „Freie und Hansestadt Hamburg, Landesbetrieb Geoinformation und Vermessung (LGV)“
    # (Transparenzportal). Celé město v jednom ZIPu (DGM1 2022 1,36 GB, bDOM 2022 1,34 GB), uvnitř GeoTIFF po 1 km →
    # čte se rozsahy jako Sársko. DGM1 odkazuje Transparenzportal jen na archivní kopii (přímá adresa daten-hamburg.de
    # vrací 404). Povrch je obrazový bDOM → green: photo. Volné ortofoto WMS bez cizí licence nenalezeno (časová řada
    # obsahuje snímky Maxar s uzavřenou licencí) → bez šablony.
    "de_hh": DeWcsState(
        code="DE-HH",
        name="Hamburk",
        crs="EPSG:25832",
        dtm=(f"{HH_ARCHIVE}/dgm1_hh_2022-04-30_258334_snap_1.zip", "dgm1"),
        dsm=(f"{HH_DATA}/Digitales_Hoehenmodell_bDOM/dom1_hh_2022-11-21.zip", "dom1"),
        probe_box=(565000, 5935000, 565016, 5935016),  # centrum
        tiles="zip_range",
    ),
    # Brémy (Landesamt GeoInformation Bremen): CC BY 4.0, Quellenvermerk „Landesamt GeoInformation Bremen“ (metadata
    # ATKIS DGM1 Land Bremen, Fees WMS). ZIP na město (Brémy 2017 1,1 / 1,2 GB, Bremerhaven 2015 0,6 / 0,3 GB), uvnitř XYZ
    # po 1 km s hlavičkou „x y z“ (~3 MB) → čte se rozsahy, převádí na GeoTIFF. DOP10 2025 obou měst jednou vrstvou WMS.
    "de_hb": DeWcsState(
        code="DE-HB",
        name="Brémy",
        crs="EPSG:25832",
        dtm=(f"{HB_DATA}/DGM/data/Gitternetz_DGM1_{{part}}_ASCII_XYZ.zip", "dgm1"),
        dsm=(f"{HB_DATA}/DOM/data/Gitternetz_DOM1_{{part}}_ASCII_XYZ.zip", "dom1"),
        probe_box=(487000, 5882000, 487016, 5882016),  # Brémy
        wms=(
            ("de_hb_dop", "dop_bremen.xml", "DOP10 Bremen (ortofoto)", HB_WMS, "dop10_2025_HB,dop10_2025_BHV",
             "image/jpeg", False, 0.1),
        ),
        tiles="zip_range",
        parts=("2017_HB", "2015_BHV"),
    ),
    # Šlesvicko-Holštýnsko (LVermGeo SH): CC BY 4.0, Quellenvermerk „© GeoBasis-DE/LVermGeo SH/CC BY 4.0“ (Fees WMS DOP20,
    # Open-Data-Portal SH, ověřeno 2026-11-20). Hromadné stahování po 1 km podle GeoJSON indexu: DGM1 jako XYZ (lety
    # 2005–2025, ~28 MB), obrazový bDOM20 GeoTIFF 0,2 m z letů 2023–24 (~105 MB) → green: photo. Převod a cache na serveru.
    "de_sh": DeWcsState(
        code="DE-SH",
        name="Šlesvicko-Holštýnsko",
        crs="EPSG:25832",
        dtm=(f"{SH_DL}?file=DGM1_SH__Massendownload.geojson&id=4", "dgm1"),
        dsm=(f"{SH_DL}?file=bDOM_SH_Massendownload.geojson&id=4", "bdom20nc"),
        probe_box=(580000, 5980000, 580016, 5980016),  # Segeberger Forst
        wms=(
            ("de_sh_dop", "dop_schleswig_holstein.xml", "DOP20 Schleswig-Holstein (ortofoto)", SH_WMS, "sh_dop20_rgb",
             "image/jpeg", False, 0.2),
        ),
        tiles="sh_geojson",
    ),
}


def state_for_country(code: str) -> tuple[str, DeWcsState] | None:
    for key, st in DE_WCS_STATES.items():
        if st.code == code:
            return key, st
    return None


def wcs_coverage_url(service: tuple[str, str], box: tuple[float, float, float, float],
                     axes: tuple[str, str] = ("x", "y")) -> str:
    """GetCoverage (WCS 2.0.1) jedné dlaždice v nativním CRS služby; pixel 1 m (hrany na celé metry)."""
    url, coverage = service
    x0, y0, x1, y1 = box
    return (
        f"{url}?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&COVERAGEID={coverage}&FORMAT=image/tiff"
        f"&SUBSET={axes[0]}({x0:.0f},{x1:.0f})&SUBSET={axes[1]}({y0:.0f},{y1:.0f})"
    )


def bw_zip_url(service: tuple[str, str], east_km: int, north_km: int) -> str:
    """ZIP dlaždice 2 km LGL BW (levý dolní roh v km, E liché, N sudé)."""
    base, prefix = service
    return f"{base}/{prefix}_32_{east_km}_{north_km}_2_bw.zip"


def sn_zip_url(service: tuple[str, str], east_km: int, north_km: int) -> str:
    """ZIP dlaždice 2 km GeoSN (levý dolní roh v km, obojí sudé)."""
    base, prefix = service
    return f"{base}/{prefix}_33{east_km}_{north_km}_2_sn_tiff.zip"


def zip_range_parts(st: DeWcsState, kind: str) -> list[str]:
    """ZIPy jedné vrstvy (``dtm`` / ``dsm``) u ``zip_range`` (Sársko po okresech, Hamburk jeden ZIP)."""
    template = (st.dtm if kind == "dtm" else st.dsm)[0]
    return [template.format(part=part) for part in st.parts]


def elevation_probe(key: str) -> str:
    """Sonda výšek: malý GetCoverage, u souborových zdrojů index nebo ZIP dlaždice (jen HEAD)."""
    st = DE_WCS_STATES[key]
    if st.tiles == "meta4":
        return st.dtm[0]
    if st.tiles == "bw_zip":
        return bw_zip_url(st.dtm, int(st.probe_box[0] // 1000), int(st.probe_box[1] // 1000))
    if st.tiles == "zip_range":
        return zip_range_parts(st, "dtm")[-1]
    if st.tiles == "sh_geojson":
        return st.dtm[0]
    if st.tiles == "sn_zip":
        return sn_zip_url(st.dtm, int(st.probe_box[0] // 2000) * 2, int(st.probe_box[1] // 2000) * 2)
    return wcs_coverage_url(st.dtm, st.probe_box, st.axes)


def elevation_probe_method(key: str) -> str:
    # GeoCloud Saska na HEAD odpovídá 401, GET čte sonda jen prvních 64 kB
    return "GET" if DE_WCS_STATES[key].tiles in ("wcs", "sn_zip") else "HEAD"


def ortho_probe(key: str) -> str:
    st = DE_WCS_STATES[key]
    if not st.wms:
        raise ValueError(f"{st.name}: bez ortofota")
    _k, _f, _label, url, layer, fmt, _transparent, _res = st.wms[0]
    x0, y0, x1, y1 = st.probe_box
    return (
        f"{url}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS={layer}&STYLES=&CRS={st.crs}"
        f"&BBOX={x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}&WIDTH=16&HEIGHT=16&FORMAT={fmt}"
    )
