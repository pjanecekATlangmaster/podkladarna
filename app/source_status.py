"""Kontrola dostupnosti zdrojů dat (ČÚZK, DTM, OSM, Sentinel-2) pro web.

Kontrola běží líně (Petr 2026-11-12): až když někdo otevře stránku (``GET /api/source_status``)
nebo začne job, a jen když je poslední výsledek starší než ``SOURCE_CHECK_TTL_MIN`` (výchozí 3 min).
Na každý zdroj jde jeden lehký dotaz (HEAD na soubor, popis služby ``?f=json``, malý JSON) –
žádné stahování dat; víc návštěvníků naráz spustí nejvýš jednu kontrolu. Výsledek se ukládá
do ``DATA_ROOT/source_status.json``, takže ho vidí i proces jobu.

Zdroj je „mimo provoz“, když neodpoví (timeout, síť) nebo vrátí HTTP 5xx; po
chybě se dotaz jednou zopakuje, ať krátký výpadek nevyvolá planý poplach. Odpověď 5xx s textem
„odstávka“ (DMVS 2026-11: „Plánovaná odstávka“) je plánovaná odstávka (``planned``).

Každé selhání a obnovení se zapíše do deníku ``source_events`` (souhrnný e-mail jednou za
``SOURCE_DIGEST_HOURS``, čtení přes ``GET /api/source_events`` s tokenem). Okamžitě se hlásí jen
zdroj, bez kterého job selže, v zemích ``SOURCE_ALERT_COUNTRIES`` (výchozí Česko, Petr 2026-11-20):
trvá-li výpadek nepřetržitě aspoň ``SOURCE_ALERT_AFTER_MIN`` (od prvního zjištěného selhání
``down_since``), pošle se provozovateli (``FEEDBACK_TO``) jeden e-mail a po obnovení druhý.
Měří se líně při kontrolách (žádný běh na pozadí); proti dvojímu mailu z webu a jobu
chrání značka v ``DATA_ROOT/source_alerts/`` založená výhradně (``O_EXCL``).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from app import countries, i18n, settings, source_events

from app.pipeline.dtm_sources import DMVS_API
from app.pipeline.fetch_openzu import OPENZU_DMPOK, OPENZU_DMR, USER_AGENT
from app.pipeline.osm_paths import OVERPASS_URLS
from app.pipeline.bgt_nl import BGT_API
from app.pipeline.reference_at import AT_BASEMAP_PROBE, AT_BEV_PROBE, AT_FMZK_PROBE, AT_LB_PROBE
from app.pipeline.reference_de import DE_BE_DGM_PROBE, DE_BE_DOP_PROBE, DE_BE_SB_PROBE
from app.pipeline.reference_fr import FR_LIDAR_PROBE, FR_ORTHO_PROBE, FR_PDV_PROBE
from app.pipeline.reference_ee import EE_ELEVATION_PROBE, EE_ETAK_PROBE, EE_ORTHO_PROBE
from app.pipeline.zbgis_sk import DATASETS as SK_ZBGIS_DATASETS
from app.pipeline.reference_it import TN_ELEVATION_PROBE, TN_ORTHO_PROBE
from app.pipeline.geodanmark_dk import GEODK_PROBE
from app.pipeline.reference_dk import DK_ELEVATION_PROBE, with_key as dk_with_key
from app.pipeline.reference_no import NO_ELEVATION_PROBE
from app.pipeline.reference_si import SI_ELEVATION_PROBE
from app.pipeline.reference_us import US_ELEVATION_PROBE, US_INDEX_PROBE
from app.pipeline.reference_be import BE_ELEVATION_PROBE, BE_GRB_PROBE, BE_ORTHO_PROBE, BE_URBIS_PROBE
from app.pipeline.reference_de_nw import DE_NW_DOP_PROBE, DE_NW_ELEVATION_PROBE
from app.pipeline.reference_de_states import DE_WCS_STATES, elevation_probe, elevation_probe_method, ortho_probe
from app.pipeline.reference_by import BY_ELEVATION_PROBE, BY_ORTHO_PROBE
from app.pipeline.reference_gb import GB_EA_PROBE, SCT_ELEVATION_PROBE, WLS_ELEVATION_PROBE
from app.pipeline.reference_pl import PL_ELEVATION_PROBE, PL_ORTHO_PROBE
from app.pipeline.reference_lu import LU_ELEVATION_PROBE, LU_ORTHO_PROBE
from app.pipeline.reference_es import ES_ORTHO_PROBE, NAV_ELEVATION_PROBE
from app.pipeline.mtk_fi import FI_MTK_PROBE
from app.pipeline.reference_fi import FI_CHM_PROBE, FI_DTM_PROBE, FI_HEL_WFS_PROBE, FI_ORTHO_PROBE
from app.pipeline.reference_ch import CH_AV_PROBE, CH_STAC_ALTI, CH_WMS_PROBE
from app.pipeline.reference_nl import NL_AHN_WCS, NL_KADASTER_WMS, NL_LUCHTFOTO_WMS
from app.pipeline.sentinel2 import STAC_SEARCH_URL

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 15
PROBE_RETRY_WAIT_S = 3.0
# Overpass: stačí, když odpoví některé z prvních zrcadel (pipeline zkouší další).
OSM_PROBE_MIRRORS = 3
# List SM5 v Praze, který na OpenZU určitě je (HEAD bez stahování).
LIDAR_PROBE_SHEET = "PRAH77"
# Text odpovědi 5xx, podle kterého se výpadek pozná jako plánovaná odstávka.
PLANNED_OUTAGE_MARKERS = ("odstávk",)
NO_RESPONSE = "neodpovídá"
# Slovensko: hlavička celostátního ZIPu budov INSPIRE ZBGIS (bez stahování).
SK_ZBGIS_PROBE = SK_ZBGIS_DATASETS["bu"][0]
_ALERT_DIR = "source_alerts"
_ALERT_KEEP_DAYS = 60


@dataclass(frozen=True)
class Probe:
    key: str
    label: str
    urls: tuple[str, ...]
    impact: str
    method: str = "GET"
    # fatal = bez zdroje job nejspíš selže; jinak jen chybí část kresby / podkladu.
    fatal: bool = False
    # Volba formuláře, bez které se zdroj nepoužije (web podle ní varování skryje).
    needs: str | None = None
    # Vedle 2xx/3xx bere jako „běží“ i 4xx (Overpass bez dotazu vrací 400).
    any_4xx_ok: bool = False
    # Země výřezu, pro které se zdroj používá (``*`` = všude); web ukáže jen zdroje vybrané země.
    countries: tuple[str, ...] = ("CZ",)


PROBES: tuple[Probe, ...] = (
    Probe(
        "lidar",
        "LiDAR ČÚZK (DMR 5G, DMP OK)",
        (OPENZU_DMR.format(mapnom=LIDAR_PROBE_SHEET), OPENZU_DMPOK.format(mapnom=LIDAR_PROBE_SHEET)),
        "Bez LiDARu se mapa nevytvoří (pomůže jen list, který už je v cache).",
        method="HEAD",
        fatal=True,
    ),
    Probe(
        "dtm",
        "DTM krajů (DMVS ČÚZK)",
        (f"{DMVS_API}/is-dtmk/info",),
        "Mapa bude nejspíš bez DTM (pomůže jen balík, který už je v cache): budovy, ploty, "
        "chodníky a šířky cest jen z OSM a ZABAGED.",
        needs="dtm",
    ),
    Probe(
        "osm",
        "OpenStreetMap (Overpass)",
        OVERPASS_URLS[:OSM_PROBE_MIRRORS],
        "Bez OSM se nestáhne základ kresby (cesty, budovy, objekty); job nejspíš selže.",
        fatal=True,
        any_4xx_ok=True,
        countries=("*",),
    ),
    Probe(
        "zabaged",
        "ZABAGED ČÚZK",
        ("https://ags.cuzk.gov.cz/arcgis/rest/services/ZABAGED_POLOHOPIS/MapServer?f=json",),
        "Mapa bude bez doplňků ze ZABAGED (vodstvo, areály, srázy, kříže …).",
    ),
    Probe(
        "ruian",
        "RÚIAN ČÚZK (obce, parcely)",
        ("https://ags.cuzk.gov.cz/arcgis/rest/services/RUIAN/MapServer/12?f=json",),
        "Bez RÚIAN se nenajdou balíky DTM ani parcely ulic (sprint).",
    ),
    Probe(
        "ortofoto",
        "Ortofoto ČÚZK",
        ("https://ags.cuzk.gov.cz/arcgis1/rest/services/ORTOFOTO/MapServer?f=json",),
        "Bez ortofota chybí stromy na dlažbě, herní prvky a ulice z parcel (sprint), "
        "louky v lese a šablona ortofota.",
    ),
    Probe(
        "katastr",
        "Katastrální mapa ČÚZK",
        ("https://services.cuzk.cz/dxf/ku/",),
        "Šablona katastrální mapy bude chybět.",
        method="HEAD",
    ),
    Probe(
        "sentinel",
        "Sentinel-2 (AWS)",
        (STAC_SEARCH_URL.rsplit("/", 1)[0] + "/",),
        "Šablona se snímky Sentinel-2 bude chybět.",
        countries=("*",),
    ),
    # Nizozemsko (PDOK): HEAD na popis služby, nic se nestahuje.
    Probe(
        "nl_ahn",
        "AHN – výšky terénu a povrchu (PDOK)",
        (f"{NL_AHN_WCS}?service=WCS&request=GetCapabilities",),
        "Bez AHN se mapa nevytvoří (terén ani výška porostu).",
        method="HEAD",
        fatal=True,
        countries=("NL",),
    ),
    Probe(
        "nl_luchtfoto",
        "Luchtfoto – ortofoto (PDOK)",
        (f"{NL_LUCHTFOTO_WMS}?service=WMS&request=GetCapabilities",),
        "Šablona ortofota bude chybět.",
        method="HEAD",
        countries=("NL",),
    ),
    Probe(
        "nl_bgt",
        "BGT – velkoměřítková mapa (PDOK)",
        (f"{BGT_API}/collections/pand?f=json",),  # OGC API Features (kresba); HEAD API neumí (404)
        "Budovy, silnice, chodníky, ploty a soukromé pozemky se nakreslí jen z OSM.",
        needs="dtm",
        countries=("NL",),
    ),
    Probe(
        "nl_kadaster",
        "Kadastrale kaart (PDOK)",
        (f"{NL_KADASTER_WMS}?service=WMS&request=GetCapabilities",),
        "Šablona katastrální mapy bude chybět.",
        method="HEAD",
        countries=("NL",),
    ),
    # Švýcarsko (swisstopo, geodienste.ch): malé GET dotazy (STAC sady, 1 objekt AV, 1 px ortofota).
    Probe(
        "ch_swisstopo",
        "swissALTI3D a swissSURFACE3D – výšky terénu a povrchu (swisstopo)",
        (CH_STAC_ALTI,),
        "Bez výšek swisstopo se mapa nevytvoří (terén ani výška porostu).",
        fatal=True,
        countries=("CH",),
    ),
    Probe(
        "ch_av",
        "AV – úřední vyměřování kantonů (geodienste.ch)",
        (CH_AV_PROBE,),
        "Budovy, silnice, chodníky, schody a zahrady se nakreslí jen z OSM.",
        needs="dtm",
        countries=("CH",),
    ),
    Probe(
        "ch_wms",
        "SWISSIMAGE, národní mapa a AV jako šablony (swisstopo WMS)",
        (CH_WMS_PROBE,),
        "Šablony ortofota, mapy a katastru budou chybět.",
        countries=("CH",),
    ),
    # Rakousko: hlavička dlaždice BEV (COG má 10 GB), 1 objekt FMZK (Vídeň), dlaždice basemap.at a ortofota Vídně.
    Probe(
        "at_bev",
        "BEV ALS – výšky terénu a povrchu",
        (AT_BEV_PROBE,),
        "Bez výšek BEV se mapa nevytvoří (terén ani výška porostu).",
        method="HEAD",
        fatal=True,
        countries=("AT",),
    ),
    Probe(
        "at_fmzk",
        "FMZK – Flächen-Mehrzweckkarte (Stadt Wien)",
        (AT_FMZK_PROBE,),
        "Budovy, vozovky, chodníky, parkování a schody se nakreslí jen z OSM.",
        needs="dtm",
        countries=("AT",),
    ),
    Probe(
        "at_basemap",
        "basemap.at – ortofoto a mapa",
        (AT_BASEMAP_PROBE,),
        "Šablony ortofota, mapy a stínování budou chybět.",
        countries=("AT",),
    ),
    Probe(
        "at_luftbild",
        "Luftbild Wien – ortofoto (Stadt Wien)",
        (AT_LB_PROBE,),
        "Šablona ortofota bude chybět.",
        countries=("AT",),
    ),
    # Německo (Berlín): hlavička dlaždice DGM1, 1 objekt Straßenbefahrung, malý výřez ortofota.
    Probe(
        "de_berlin",
        "DGM1 a DOM1 Berlín – výšky terénu a povrchu",
        (DE_BE_DGM_PROBE,),
        "Bez výšek z GDI Berlin se mapa nevytvoří (terén ani výška porostu).",
        method="HEAD",
        fatal=True,
        countries=("DE",),
    ),
    Probe(
        "de_sb",
        "Straßenbefahrung a ALKIS (GDI Berlin)",
        (DE_BE_SB_PROBE,),
        "Budovy, vozovky, chodníky, parkování a schody se nakreslí jen z OSM.",
        needs="dtm",
        countries=("DE",),
    ),
    Probe(
        "de_truedop",
        "TrueDOP Berlín – ortofoto (GDI Berlin)",
        (DE_BE_DOP_PROBE,),
        "Šablona ortofota bude chybět.",
        countries=("DE",),
    ),
    # Francie (Paříž): malý výřez MNT LiDAR HD, 1 záznam Plan de voirie, malý výřez ortofota.
    Probe(
        "fr_lidarhd",
        "LiDAR HD (IGN) – výšky terénu a povrchu",
        (FR_LIDAR_PROBE,),
        "Bez výšek LiDAR HD se mapa nevytvoří (terén ani výška porostu).",
        fatal=True,
        countries=("FR",),
    ),
    Probe(
        "fr_pdv",
        "Plan de voirie (Ville de Paris)",
        (FR_PDV_PROBE,),
        "Vozovky, chodníky a schodiště se nakreslí jen z OSM.",
        needs="dtm",
        countries=("FR",),
    ),
    Probe(
        "fr_ortho",
        "Ortofoto IGN (Géoplateforme)",
        (FR_ORTHO_PROBE,),
        "Šablona ortofota bude chybět.",
        countries=("FR",),
    ),
    # Estonsko: hlavička listu DSM, 1 objekt ETAK, malý výřez ortofota.
    Probe(
        "ee_elevation",
        "Výšky Maa- ja Ruumiamet – DTM a DSM",
        (EE_ELEVATION_PROBE,),
        "Bez výšek se mapa nevytvoří (terén ani výška porostu).",
        method="HEAD",
        fatal=True,
        countries=("EE",),
    ),
    Probe(
        "ee_etak",
        "ETAK (Maa- ja Ruumiamet)",
        (EE_ETAK_PROBE,),
        "Budovy, ulice, pěší plochy, parkoviště, zeleň, dvory a ploty se nakreslí jen z OSM.",
        needs="dtm",
        countries=("EE",),
    ),
    Probe(
        "ee_ortho",
        "Ortofoto Maa- ja Ruumiamet",
        (EE_ORTHO_PROBE,),
        "Šablona ortofota bude chybět.",
        countries=("EE",),
    ),
    # Slovensko: hlavička celostátního ZIPu budov INSPIRE ZBGIS (výšky jsou na disku instance).
    Probe(
        "sk_zbgis",
        "INSPIRE ZBGIS (ÚGKK SR)",
        (SK_ZBGIS_PROBE,),
        "Budovy a silnice se nakreslí jen z OSM.",
        method="HEAD",
        needs="dtm",
        countries=("SK",),
    ),
    # Itálie – Trentino: index dlaždic LiDARu a malý výřez ortofota (kresba z OSM).
    Probe(
        "it_elevation",
        "Výšky LiDAR Provincia di Trento – DTM a DSM",
        (TN_ELEVATION_PROBE,),
        "Bez výšek se mapa nevytvoří (terén ani výška porostu).",
        fatal=True,
        countries=("IT",),
    ),
    Probe(
        "it_ortho",
        "Ortofoto Provincia di Trento",
        (TN_ORTHO_PROBE,),
        "Šablona ortofota bude chybět.",
        countries=("IT",),
    ),
    # Flandry: malé okno DTM z WCS, 1 budova GRB, malý výřez ortofota.
    Probe(
        "be_elevation",
        "Výšky DHMV II (Digitaal Vlaanderen) – DTM a DSM",
        (BE_ELEVATION_PROBE,),
        "Bez výšek se mapa nevytvoří (terén ani výška porostu).",
        fatal=True,
        countries=("BE",),
    ),
    # Dánsko: okno 8 × 8 m terénu DHM z WCS Datafordeleru (klíč se do URL dosadí až při dotazu, ``_probe_url``).
    Probe(
        "dk_elevation",
        "Výšky DHM (Datafordeler) – terén a povrch",
        (DK_ELEVATION_PROBE,),
        "Bez výšek se mapa nevytvoří (terén ani výška porostu).",
        fatal=True,
        countries=("DK",),
    ),
    Probe(
        "dk_geodanmark",
        "GeoDanmark (Datafordeler)",
        (GEODK_PROBE,),
        "Budovy, vozovky, parkoviště a ploty se nakreslí jen z OSM.",
        countries=("DK",),
    ),
    # Norsko: okno 8 × 8 m terénu høydemodell z WCS Geonorge (bez klíče).
    Probe(
        "no_elevation",
        "Výšky høydemodell (Kartverket) – terén a povrch",
        (NO_ELEVATION_PROBE,),
        "Bez výšek se mapa nevytvoří (terén ani výška porostu).",
        fatal=True,
        countries=("NO",),
    ),
    # Slovinsko: HEAD listu mračna ARSO u Ljubljany (bez klíče, ~50 MB se nestahuje).
    Probe(
        "si_elevation",
        "Mračno LiDAR (ARSO) – terén a povrch",
        (SI_ELEVATION_PROBE,),
        "Bez výšek se mapa nevytvoří (terén ani výška porostu).",
        method="HEAD",
        fatal=True,
        countries=("SI",),
    ),
    # USA: index projektů WESM (výběr mračna QL 0 – QL 2) a ept.json jednoho projektu v bucketu 3DEP (~3 kB).
    Probe(
        "us_index",
        "Index projektů LiDAR WESM (USGS)",
        (US_INDEX_PROBE,),
        "Bez indexu nejde vybrat mračno výřezu, mapa se nevytvoří.",
        fatal=True,
        countries=("US",),
    ),
    Probe(
        "us_elevation",
        "Mračno LiDAR 3DEP (USGS, AWS) – terén a povrch",
        (US_ELEVATION_PROBE,),
        "Bez výšek se mapa nevytvoří (terén ani výška porostu).",
        fatal=True,
        countries=("US",),
    ),
    # Skotsko: HEAD malé dlaždice terénu v S3 bucketu Scottish Remote Sensing Portal.
    Probe(
        "sct_elevation",
        "Výšky Scottish Remote Sensing Portal – LiDAR DTM a DSM",
        (SCT_ELEVATION_PROBE,),
        "Bez výšek se mapa nevytvoří (terén ani výška porostu).",
        method="HEAD",
        fatal=True,
        countries=("GB-SCT",),
    ),
    # Wales: HEAD na celostátní COG terénu DataMapWales (Azure Blob).
    Probe(
        "wls_elevation",
        "Výšky DataMapWales – LiDAR DTM a DSM",
        (WLS_ELEVATION_PROBE,),
        "Bez výšek se mapa nevytvoří (terén ani výška porostu).",
        method="HEAD",
        fatal=True,
        countries=("GB-WLS",),
    ),
    # Navarra: HEAD listu mračna LiDAR 2024 (Pamplona) a malý výřez ortofota PNOA (IGN).
    Probe(
        "nav_elevation",
        "Mračno LiDAR 2024 (Gobierno de Navarra) – terén a povrch",
        (NAV_ELEVATION_PROBE,),
        "Bez výšek se mapa nevytvoří (pomůže jen list, který už je v mezipaměti).",
        method="HEAD",
        fatal=True,
        countries=("ES-NC",),
    ),
    Probe(
        "es_ortho",
        "Ortofoto PNOA (IGN)",
        (ES_ORTHO_PROBE,),
        "Šablona ortofota bude chybět.",
        countries=("ES-NC",),
    ),
    Probe(
        "be_grb",
        "GRB (Digitaal Vlaanderen)",
        (BE_GRB_PROBE,),
        "Budovy, vozovky a chodníky se nakreslí jen z OSM.",
        needs="dtm",
        countries=("BE",),
    ),
    Probe(
        "be_urbis",
        "UrbIS (Paradigm, Brusel)",
        (BE_URBIS_PROBE,),
        "Budovy, vozovky a chodníky v Bruselu se nakreslí jen z OSM.",
        needs="dtm",
        countries=("BE",),
    ),
    Probe(
        "be_ortho",
        "Ortofoto Digitaal Vlaanderen",
        (BE_ORTHO_PROBE,),
        "Šablona ortofota bude chybět.",
        countries=("BE",),
    ),
    # Finsko: hlavička jednoho listu výšek (zrcadlo Funet) a porostu (Metsäkeskus), 1 budova Helsinek, malý výřez ortofota.
    Probe(
        "fi_elevation",
        "Výškový model 2 m MML (zrcadlo Funet)",
        (FI_DTM_PROBE,),
        "Bez výšek se mapa nevytvoří (terén ani výška porostu).",
        method="HEAD",
        fatal=True,
        countries=("FI",),
    ),
    Probe(
        "fi_chm",
        "Výška porostu (latvusmalli) Metsäkeskus",
        (FI_CHM_PROBE,),
        "Bez výšky porostu se nerozliší les a vegetace (mapa jen s terénem).",
        method="HEAD",
        countries=("FI",),
    ),
    Probe(
        "fi_ylre",
        "YLRE a budovy Helsinek (kartta.hel.fi)",
        (FI_HEL_WFS_PROBE,),
        "Budovy, vozovky a chodníky v Helsinkách se nakreslí jen z OSM.",
        needs="dtm",
        countries=("FI",),
    ),
    Probe(
        "fi_ortho",
        "Ortofoto MML (kartat.kapsi.fi)",
        (FI_ORTHO_PROBE,),
        "Šablona ortofota bude chybět.",
        countries=("FI",),
    ),
    Probe(
        "fi_mtk",
        "Maastotietokanta MML (kartat.kapsi.fi)",
        (FI_MTK_PROBE,),
        "Bažiny, skály, balvany a pole se nakreslí jen z OSM.",
        method="HEAD",
        countries=("FI",),
    ),
    # Lucembursko: hlavička COG terénu (S3) a malý výřez ortofota (kresba z OSM).
    Probe(
        "lu_elevation",
        "Výšky LiDAR 2024 (ACT) – MNT a MNS",
        (LU_ELEVATION_PROBE,),
        "Bez výšek se mapa nevytvoří (terén ani výška porostu).",
        method="HEAD",
        fatal=True,
        countries=("LU",),
    ),
    Probe(
        "lu_ortho",
        "Ortofoto ACT (geoportail.lu)",
        (LU_ORTHO_PROBE,),
        "Šablona ortofota bude chybět.",
        countries=("LU",),
    ),
    # Severní Porýní-Vestfálsko: malý výřez DGM z WCS a malý výřez ortofota (kresba z OSM).
    Probe(
        "de_nw_elevation",
        "Výšky Geobasis NRW – DGM1 a nDOM50",
        (DE_NW_ELEVATION_PROBE,),
        "Bez výšek se mapa nevytvoří (terén ani výška porostu).",
        fatal=True,
        countries=("DE-NW",),
    ),
    Probe(
        "de_nw_dop",
        "Ortofoto DOP (Geobasis NRW)",
        (DE_NW_DOP_PROBE,),
        "Šablona ortofota bude chybět.",
        countries=("DE-NW",),
    ),
    # Polsko: malý výřez NMT z WCS a malý výřez ortofota (kresba z OSM).
    Probe(
        "pl_elevation",
        "Výšky GUGiK – NMT a NMPT",
        (PL_ELEVATION_PROBE,),
        "Bez výšek se mapa nevytvoří (terén ani výška porostu).",
        fatal=True,
        countries=("PL",),
    ),
    Probe(
        "pl_ortho",
        "Ortofotomapa GUGiK",
        (PL_ORTHO_PROBE,),
        "Šablona ortofota bude chybět.",
        countries=("PL",),
    ),
    # Anglie: HEAD na dlaždici EA (server HEAD nepodporuje a vrátí 405 = běží; GET by stáhl 70 MB).
    Probe(
        "gb_elevation",
        "Výšky Environment Agency – LIDAR Composite",
        (GB_EA_PROBE,),
        "Bez výšek se mapa nevytvoří (pomůže jen dlaždice, která už je v mezipaměti).",
        method="HEAD",
        fatal=True,
        any_4xx_ok=True,
        countries=("GB-ENG",),
    ),
    # Bavorsko: HEAD na dlaždici DGM1 a malý výřez ortofota DOP40 (kresba z OSM).
    Probe(
        "de_by_elevation",
        "Výšky LDBV Bayern – DGM1 a DOM20",
        (BY_ELEVATION_PROBE,),
        "Bez výšek se mapa nevytvoří (terén ani výška porostu).",
        method="HEAD",
        fatal=True,
        countries=("DE-BY",),
    ),
    Probe(
        "de_by_dop",
        "Ortofoto DOP40 (LDBV Bayern)",
        (BY_ORTHO_PROBE,),
        "Šablona ortofota bude chybět.",
        countries=("DE-BY",),
    ),
    # Německé země s výškami z WCS (``reference_de_states``): malý výřez DGM1 a ortofota (kresba z OSM).
    *(
        probe
        for key, st in DE_WCS_STATES.items()
        for probe in (
            Probe(
                f"{key}_elevation",
                f"Výšky {st.name} – DGM1 a DOM1",
                (elevation_probe(key),),
                "Bez výšek se mapa nevytvoří (terén ani výška porostu).",
                method=elevation_probe_method(key),
                fatal=True,
                countries=(st.code,),
            ),
            *(() if not st.wms else (Probe(
                f"{key}_dop",
                f"Ortofoto ({st.name})",
                (ortho_probe(key),),
                "Šablona ortofota bude chybět.",
                countries=(st.code,),
            ),)),
        )
    ),
)



# Zdroje výšek z rastru mimo Česko (``prepare_raster_surfaces``), jejichž klíč nekončí ``_elevation``.
_ELEVATION_KEYS = frozenset({"at_bev", "ch_swisstopo", "nl_ahn", "de_berlin", "fr_lidarhd", "us_index"})


def probe_role(key: str | None) -> str | None:
    """Druh dat zdroje, který job umí potvrdit jako opravdu stažený (``job_progress.SOURCE_*``), jinak None.

    Český LiDAR (``lidar``) ani ostatní zdroje se nepotvrzují: jejich varování ze startu zůstává chybou jobu jako dřív.
    """
    from app.pipeline.job_progress import SOURCE_ELEVATION, SOURCE_OSM, SOURCE_SURFACE

    if not key:
        return None
    if key == "osm":
        return SOURCE_OSM
    if key == "fi_chm":
        return SOURCE_SURFACE
    if key in _ELEVATION_KEYS or key.endswith("_elevation"):
        return SOURCE_ELEVATION
    return None


def probes_for(country: str | None = None) -> list[Probe]:
    """Zdroje, které používá job v zemi ``country`` (výchozí Česko)."""
    code = (country or countries.DEFAULT_COUNTRY).strip().upper()
    return [p for p in PROBES if "*" in p.countries or code in p.countries]


def active_probes() -> list[Probe]:
    """Zdroje zemí, které instance nabízí (vypnuté země se nekontrolují)."""
    offered = countries.offered_codes() | {countries.DEFAULT_COUNTRY}
    return [p for p in PROBES if "*" in p.countries or offered & set(p.countries)]


_STATUS_FILE = "source_status.json"
_refresh_lock = threading.Lock()  # jedna kontrola naráz v procesu webu
_merge_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _status_path() -> Path:
    return settings.DATA_ROOT / _STATUS_FILE


def _load() -> dict:
    try:
        data = json.loads(_status_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("services"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"checked_at": None, "services": {}}


def _save(state: dict) -> None:
    path = _status_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.warning("Stav zdrojů dat nejde uložit do %s", path, exc_info=True)


def _age_s(state: dict) -> float | None:
    try:
        checked = datetime.fromisoformat(state["checked_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - checked).total_seconds()


def ttl_s() -> float:
    return max(0.0, float(settings.SOURCE_CHECK_TTL_MIN)) * 60


def _probe_url(url: str, method: str) -> tuple[int | None, str | None, str]:
    """HTTP kód (nebo None), krátký popis chyby sítě a začátek textu chybové odpovědi."""
    try:
        url = dk_with_key(url)  # zástupný klíč Datafordeleru; chyby níž URL neobsahují
    except RuntimeError as exc:
        return None, str(exc)[:200], ""
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            if method != "HEAD":
                resp.read(64 * 1024)
            return resp.status, None, ""
    except urllib.error.HTTPError as exc:
        text = ""
        try:
            text = exc.read(2048).decode("utf-8", "replace")
        except Exception:
            pass
        return exc.code, None, text
    except Exception as exc:  # timeout, DNS, TLS, odmítnuté spojení
        reason = getattr(exc, "reason", None) or exc
        return None, f"{type(exc).__name__}: {reason}"[:200], ""


def _is_planned(code: int | None, text: str) -> bool:
    low = (text or "").lower()
    return code is not None and code >= 500 and any(m in low for m in PLANNED_OUTAGE_MARKERS)


def _is_up(code: int | None, probe: Probe) -> bool:
    if code is None:
        return False
    if 200 <= code < 400:
        return True
    return probe.any_4xx_ok and 400 <= code < 500


def check_probe(probe: Probe) -> dict:
    """Jeden zdroj: běží, když odpoví aspoň jedna URL (po chybě jeden opakovaný pokus)."""
    t0 = time.monotonic()
    code: int | None = None
    error: str | None = None
    ok = False
    planned = False
    for attempt in range(2):
        planned = False
        for url in probe.urls:
            code, error, text = _probe_url(url, probe.method)
            if _is_up(code, probe):
                ok = True
                break
            planned = planned or _is_planned(code, text)
        if ok or attempt:
            break
        time.sleep(PROBE_RETRY_WAIT_S)
    detail = None
    if not ok:
        detail = f"HTTP {code}" if code is not None else (error or NO_RESPONSE)
        if planned:
            detail = f"plánovaná odstávka, {detail}"
    return {
        "key": probe.key,
        "label": probe.label,
        "ok": ok,
        "planned": planned and not ok,
        "detail": detail,
        "impact": probe.impact,
        "fatal": probe.fatal,
        "needs": probe.needs,
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "code": None if ok else code,
        "countries": list(probe.countries),
    }


def check_all() -> dict:
    """Zkontroluje všechny zdroje souběžně a uloží výsledek; vrací snímek stavu."""
    probes = active_probes()
    with ThreadPoolExecutor(max_workers=len(probes), thread_name_prefix="src-check") as pool:
        results = list(pool.map(check_probe, probes))
    now = _now_iso()
    prev = _load()["services"]
    services = {}
    for r in results:
        old = prev.get(r["key"]) or {}
        if r["ok"]:
            r["down_since"] = None
            r["alerted"] = False
        else:
            r["down_since"] = (old.get("down_since") if not old.get("ok", True) else None) or now
            r["alerted"] = bool(old.get("alerted")) and r["down_since"] == old.get("down_since")
        services[r["key"]] = r
    alerts = _due_alerts(prev, services)
    _save({"checked_at": now, "services": services})
    source_events.record(results, prev, now)
    down = [r["label"] for r in results if not r["ok"]]
    if down:
        logger.warning("Zdroje dat mimo provoz: %s", ", ".join(down))
    if alerts:
        _send_alerts(alerts)
    try:
        source_events.maybe_send_digest(services)
    except Exception:
        logger.warning("Souhrn zdrojů dat selhal", exc_info=True)
    return snapshot()


def merge_results(results: list[dict]) -> None:
    """Výsledky dílčí kontroly (``source_gate.recheck``) do uloženého stavu; čas celé kontroly se nemění.

    ``down_since`` se drží jako v ``check_all``; e-maily posílá jen celá kontrola."""
    if not results:
        return
    with _merge_lock:
        state = _load()
        now = _now_iso()
        known = [r for r in results if r.get("key") in state["services"]]
        source_events.record(known, {r["key"]: state["services"][r["key"]] for r in known}, now)
        for r in results:
            key = r.get("key")
            old = state["services"].get(key)
            if not key or old is None:
                continue
            new = {**old, **{k: v for k, v in r.items() if k in ("ok", "planned", "detail", "code", "latency_ms")}}
            if new.get("ok"):
                # Po odeslaném mailu o výpadku drží ``down_since`` a ``alerted``: mail o obnovení pošle až celá kontrola.
                keep = bool(old.get("alerted"))
                new["down_since"] = old.get("down_since") if keep else None
                new["alerted"] = keep
            else:
                new["down_since"] = (old.get("down_since") if not old.get("ok", True) else None) or now
            state["services"][key] = new
        _save(state)


def alert_after_s() -> float:
    return max(0.0, float(settings.SOURCE_ALERT_AFTER_MIN)) * 60


def _since_s(iso: str | None) -> float | None:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return None


def _claim(name: str) -> bool:
    """Značka odeslání (jednou napříč procesy webu a jobů); False = už ji má někdo jiný."""
    folder = settings.DATA_ROOT / _ALERT_DIR
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(folder / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        logger.warning("Značku upozornění na výpadek nejde založit", exc_info=True)
        return False


def _prune_claims() -> None:
    folder = settings.DATA_ROOT / _ALERT_DIR
    limit = time.time() - _ALERT_KEEP_DAYS * 86400
    try:
        for f in folder.iterdir():
            if f.stat().st_mtime < limit:
                f.unlink()
    except OSError:
        pass


def _claim_name(key: str, down_since: str, kind: str) -> str:
    stamp = "".join(ch for ch in down_since if ch.isalnum())
    return f"{key}-{stamp}.{kind}"


def alerts_immediately(svc: dict) -> bool:
    """Okamžitý e-mail jen u zdrojů, bez kterých job selže, v zemích ``SOURCE_ALERT_COUNTRIES``
    (výchozí ``CZ``; ``*`` = všechny, prázdné = žádné). Ostatní jdou jen do souhrnu (``source_events``)."""
    if not svc.get("fatal"):
        return False
    wanted = {c.strip().upper() for c in str(settings.SOURCE_ALERT_COUNTRIES or "").split(",") if c.strip()}
    if not wanted:
        return False
    codes = set(svc.get("countries") or ["CZ"])
    return "*" in wanted or "*" in codes or bool(wanted & codes)


def _due_alerts(prev: dict, services: dict) -> list[tuple[str, dict]]:
    """Upozornění k odeslání: ("down", služba) po prahu výpadku, ("up", služba) po obnovení.

    Mail o obnovení jen tehdy, když odešel mail o výpadku (krátké výpadky ticho)."""
    after = alert_after_s()
    if after <= 0:
        return []
    out: list[tuple[str, dict]] = []
    for key, r in services.items():
        old = prev.get(key) or {}
        if not r["ok"] and not alerts_immediately(r):
            continue
        if not r["ok"] and not r["alerted"]:
            age = _since_s(r["down_since"])
            if age is not None and age >= after:
                r["alerted"] = True  # i když značku má jiný proces: mail už odešel
                if _claim(_claim_name(key, r["down_since"], "down")):
                    out.append(("down", r))
        elif r["ok"] and old.get("alerted") and old.get("down_since"):
            if _claim(_claim_name(key, old["down_since"], "up")):
                out.append(("up", {**r, "down_since": old["down_since"]}))
    return out


def _cs_time(iso: str | None) -> str:
    try:
        dt = datetime.fromisoformat(iso).astimezone(ZoneInfo("Europe/Prague"))
    except Exception:
        return str(iso or "?")
    return f"{dt.day}. {dt.month}. {dt.year} {dt:%H:%M}"


def _duration(iso: str | None) -> str:
    sec = _since_s(iso)
    if sec is None:
        return "?"
    minutes = int(sec // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def build_alert_email(kind: str, svc: dict) -> tuple[str, str]:
    """Předmět a text e-mailu provozovateli o výpadku / obnovení zdroje."""
    label = svc.get("label") or svc.get("key")
    since = _cs_time(svc.get("down_since"))
    base = (settings.PUBLIC_BASE_URL or "").strip()
    note = (
        "Kontrola běží jen při otevření webu nebo startu jobu, čas je první zjištěné selhání.\n"
        + (f"Stav zdrojů: {base.rstrip('/')}/\n" if base else "")
    )
    if kind == "down":
        what = "má plánovanou odstávku" if svc.get("planned") else "je mimo provoz"
        subject = f"Podkladárna: zdroj {label} {what}"
        body = (
            f"Zdroj dat {label} {what} nepřetržitě od {since} ({_duration(svc.get('down_since'))}).\n"
            f"Odpověď: {svc.get('detail') or '?'}\n"
            f"Dopad na mapy: {svc.get('impact') or '?'}\n\n"
            "Až zdroj znovu odpoví, přijde jeden e-mail o obnovení.\n" + note
        )
    else:
        subject = f"Podkladárna: zdroj {label} znovu běží"
        body = (
            f"Zdroj dat {label} znovu odpovídá (výpadek od {since}, celkem {_duration(svc.get('down_since'))}).\n\n"
            + note
        )
    return subject, body


def _send_alerts(alerts: list[tuple[str, dict]]) -> None:
    to = (settings.FEEDBACK_TO or "").strip()
    if not to:
        return
    from app.mail import send_mail  # líně: modul čte SMTP nastavení

    prefix = "[test] " if settings.INSTANCE_CHANNEL == "test" else ""
    for kind, svc in alerts:
        subject, body = build_alert_email(kind, svc)
        subject = prefix + subject
        try:
            send_mail(to, subject, body)
            logger.info("Upozornění na zdroj %s (%s) odesláno", svc.get("key"), kind)
        except Exception:
            logger.warning("Upozornění na zdroj %s (%s) nejde odeslat", svc.get("key"), kind, exc_info=True)
    _prune_claims()


def snapshot(country: str | None = None) -> dict:
    """Poslední uložený stav pro API (``services`` země ``country`` v pořadí ``PROBES``)."""
    state = _load()
    services = [dict(state["services"][p.key]) for p in probes_for(country) if p.key in state["services"]]
    return {"checked_at": state.get("checked_at"), "services": services}


def localized(snap: dict, lang: str) -> dict:
    """Snímek pro web v jazyce stránky: názvy a dopady zdrojů z ``api.source.<klíč>.label|impact``.

    Čeština beze změny (texty ``Probe``, stejně jako log jobu a upozornění e-mailem)."""
    if lang == i18n.SOURCE_LANG:
        return snap
    texts = i18n.catalog(lang)
    services = []
    for svc in snap.get("services") or []:
        svc = dict(svc)
        for field in ("label", "impact"):
            text = texts.get(f"api.source.{svc.get('key')}.{field}")
            if text:
                svc[field] = text
        if svc.get("detail") == NO_RESPONSE:
            svc["detail"] = i18n.t("api.source.no_response", lang)
        services.append(svc)
    return {**snap, "services": services}


def is_stale() -> bool:
    state = _load()
    age = _age_s(state)
    if age is None or age >= ttl_s():
        return True
    # Nově nabízená země (beta zapnutá za běhu) ještě nemá výsledek.
    return any(p.key not in state["services"] for p in active_probes())


def refresh_if_stale() -> bool:
    """Web: je-li výsledek starší než TTL, spustí kontrolu na pozadí (nejvýš jednu naráz).

    Vrací True, když kontrola právě běží (web se za chvíli zeptá znovu)."""
    if ttl_s() <= 0:
        return False
    if not _refresh_lock.acquire(blocking=False):
        return True
    if not is_stale():
        _refresh_lock.release()
        return False

    def _run() -> None:
        try:
            check_all()
        except Exception:
            logger.exception("Kontrola zdrojů dat selhala")
        finally:
            _refresh_lock.release()

    try:
        threading.Thread(target=_run, name="source-status", daemon=True).start()
    except Exception:
        _refresh_lock.release()
        raise
    return True


def down_services(*, use_dtm: bool = True, country: str | None = None) -> list[dict]:
    """Job: zdroje mimo provoz; starý výsledek nejdřív ověří znovu (pár sekund)."""
    if ttl_s() <= 0:
        return []
    if is_stale():
        check_all()
    return [
        s
        for s in snapshot(country)["services"]
        if not s["ok"] and (use_dtm or s.get("needs") != "dtm")
    ]
