"""Německé spolkové země s výškami z WCS 2.0.1 (zahraniční build): Sasko-Anhaltsko, Meklenbursko-Přední Pomořansko,
Dolní Sasko a Braniborsko.

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
    dtm: tuple[str, str]         # (URL WCS, COVERAGEID) terénu DGM1
    dsm: tuple[str, str]         # (URL WCS, COVERAGEID) povrchu DOM1
    probe_box: tuple[float, float, float, float]  # malý výřez pro sondu stavu (16 × 16 m)
    wms: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = ()  # šablony jako ``online_templates``


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


def elevation_probe(key: str) -> str:
    st = DE_WCS_STATES[key]
    return wcs_coverage_url(st.dtm, st.probe_box)


def ortho_probe(key: str) -> str:
    st = DE_WCS_STATES[key]
    _k, _f, _label, url, layer, fmt, _transparent, _res = st.wms[0]
    x0, y0, x1, y1 = st.probe_box
    return (
        f"{url}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS={layer}&STYLES=&CRS={st.crs}"
        f"&BBOX={x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}&WIDTH=16&HEIGHT=16&FORMAT={fmt}"
    )
