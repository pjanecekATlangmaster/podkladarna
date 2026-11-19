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
        ),
        tiles="bw_zip",
    ),
}


def state_for_country(code: str) -> tuple[str, DeWcsState] | None:
    for key, st in DE_WCS_STATES.items():
        if st.code == code:
            return key, st
    return None


def wcs_coverage_url(service: tuple[str, str], box: tuple[float, float, float, float]) -> str:
    """GetCoverage (WCS 2.0.1) jedné dlaždice v nativním CRS služby; pixel 1 m (hrany na celé metry)."""
    url, coverage = service
    x0, y0, x1, y1 = box
    return (
        f"{url}?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&COVERAGEID={coverage}&FORMAT=image/tiff"
        f"&SUBSET=x({x0:.0f},{x1:.0f})&SUBSET=y({y0:.0f},{y1:.0f})"
    )


def bw_zip_url(service: tuple[str, str], east_km: int, north_km: int) -> str:
    """ZIP dlaždice 2 km LGL BW (levý dolní roh v km, E liché, N sudé)."""
    base, prefix = service
    return f"{base}/{prefix}_32_{east_km}_{north_km}_2_bw.zip"


def elevation_probe(key: str) -> str:
    """Sonda výšek: malý GetCoverage, u souborových zdrojů index nebo ZIP dlaždice (jen HEAD)."""
    st = DE_WCS_STATES[key]
    if st.tiles == "meta4":
        return st.dtm[0]
    if st.tiles == "bw_zip":
        return bw_zip_url(st.dtm, int(st.probe_box[0] // 1000), int(st.probe_box[1] // 1000))
    return wcs_coverage_url(st.dtm, st.probe_box)


def elevation_probe_method(key: str) -> str:
    return "GET" if DE_WCS_STATES[key].tiles == "wcs" else "HEAD"


def ortho_probe(key: str) -> str:
    st = DE_WCS_STATES[key]
    _k, _f, _label, url, layer, fmt, _transparent, _res = st.wms[0]
    x0, y0, x1, y1 = st.probe_box
    return (
        f"{url}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS={layer}&STYLES=&CRS={st.crs}"
        f"&BBOX={x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}&WIDTH=16&HEIGHT=16&FORMAT={fmt}"
    )
