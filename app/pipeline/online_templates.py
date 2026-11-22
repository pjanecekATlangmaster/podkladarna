"""Online šablony podkladů pro OOM (Petr 2026-11-08): malé soubory GDAL WMS ``.xml`` místo stažených rastrů.

Mapper (``GdalTemplate``) si podklad stáhne až při zapnutí šablony, ZIP zůstává malý. Každý soubor pokrývá
výřez mapy v pevném rozlišení (nejvýš ``MAX_PX`` px na delší straně – Mapper načte celý rastr do paměti).
OSM je dlaždicová služba v EPSG:3857: celosvětová TMS ``.xml`` a ``.vrt`` převedený gdalwarp do S-JTSK
(Mapper okno v EPSG:3857 nenačte, Petr 2026-11-08); GDAL si z TMS vezme úroveň podle rozlišení.
OCAD ``.xml`` neotevře – adresy WMS jsou v README_OOM.
"""

from __future__ import annotations

import html
import time
from pathlib import Path

from app.pipeline.crs_5514 import CRS_LABEL, CRS_PROJ4
from app.pipeline.fetch_openzu import USER_AGENT
from app.pipeline.prepare_lidar import find_tool, run_cmd
from app.pipeline.reference_at import (
    AT_BASEMAP_GRAU_URL,
    AT_BASEMAP_ORTHO_URL,
    AT_BASEMAP_RELIEF_URL,
    AT_BEV_KAT_LAYER,
    AT_BEV_KAT_WMS,
    AT_FMZK_WMS_LAYERS,
    AT_WIEN_LB_TILE_URL,
    AT_WIEN_LB_ZOOM,
    AT_WIEN_WMS,
)
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
from app.pipeline.reference_fi import (
    FI_HEL_BASEMAP_LAYER,
    FI_HEL_ORTHO_LAYER,
    FI_HEL_WMS,
    FI_KAPSI_BASEMAP_LAYER,
    FI_KAPSI_BASEMAP_WMS,
    FI_KAPSI_CADASTRE_LAYER,
    FI_KAPSI_CADASTRE_WMS,
    FI_KAPSI_ORTHO_LAYER,
    FI_KAPSI_ORTHO_WMS,
)
from app.pipeline.reference_de_nw import (
    DE_NW_ABK_LAYER,
    DE_NW_ABK_WMS,
    DE_NW_ALKIS_LAYER,
    DE_NW_ALKIS_WMS,
    DE_NW_DOP_LAYER,
    DE_NW_DOP_WMS,
)
from app.pipeline.reference_by import (
    BY_ALKIS_LAYER,
    BY_ALKIS_WMS,
    BY_DOK_LAYER,
    BY_DOK_WMS,
    BY_DOP_LAYER,
    BY_DOP_WMS,
)
from app.pipeline.reference_gb import GB_HMLR_LAYER, GB_HMLR_WMS
from app.pipeline.reference_no import NO_MATRIKKEL_LAYER, NO_MATRIKKEL_WMS, NO_TOPO_LAYER, NO_TOPO_WMS
from app.pipeline.reference_us import US_IMAGERY_WMS, US_TOPO_WMS, US_WMS_LAYER
from app.pipeline.reference_pl import PL_ORTHO_LAYER, PL_ORTHO_WMS, PL_TOPO_LAYER, PL_TOPO_WMS
from app.pipeline.reference_sk import (
    SK_CADASTRE_LAYERS,
    SK_CADASTRE_WMS,
    SK_ORTHO_LAYERS,
    SK_ORTHO_WMS,
    SK_ZBGIS_LAYERS,
    SK_ZBGIS_WMS,
)
from app.pipeline.reference_de_states import DE_WCS_STATES
from app.pipeline.reference_lu import LU_CADASTRE_LAYER, LU_ORTHO_LAYER, LU_TOPO_LAYER, LU_WMS
from app.pipeline.reference_es import ES_PNOA_LAYER, ES_PNOA_WMS
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
                          "geo.api.vlaanderen.be", "kartta.hel.fi", "tiles.kartat.kapsi.fi", "www.wms.nrw.de",
                          "geoservices.bayern.de", "zbgisws.skgeodesy.sk", "kataster.skgeodesy.sk",
                          # ALKIS WMS AdV (BB, SN, MV, ST), BEV a HMLR odmítají „default“ (část cesty – DOP téhož serveru ho bere)
                          "/ows/alkis_wms", "wms_geosn_alkis-adv", "/dienste/alkis_wms", "ALKIS_WMS_AdV_konform",
                          "/BEVdataKAT/", "inspire.landregistry.gov.uk")

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
# Lichtenštejnsko: ortofoto, národní mapa a stínování swisstopo pokrývají i LI; AV (katastr) jen Švýcarsko.
LI_ONLINE_WMS_LAYERS = tuple(layer for layer in CH_ONLINE_WMS_LAYERS if layer[0] != "ch_av")
# Rakousko: FMZK Vídně (Stadt Wien OGD) jako WMS; ortofoto a mapy jen WMTS 3857 (``ONLINE_TMS_LAYERS``).
# Server převádí do jiných systémů bez posunu data MGI (~290 m vedle): WMS v nativním systému + okno výřezu jako VRT.
AT_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("at_fmzk", "fmzk_wien.xml", "FMZK Wien (Flächen-Mehrzweckkarte)", AT_WIEN_WMS, AT_FMZK_WMS_LAYERS,
     "image/png", True, 0.25),
    ("at_cadastre", "katastr_bev.xml", "Katastr BEV (DKM – parcely)", AT_BEV_KAT_WMS, AT_BEV_KAT_LAYER, "image/png", True,
     0.25),
)
WMS_NATIVE_CRS = {"at_fmzk": "EPSG:31256"}
# CRS, jejichž osy jsou v EPSG sever–východ: WMS 1.3.0 chce BBOX v tomto pořadí (jinak prázdný obrázek), GDAL ho samo neotočí.
NORTHING_FIRST_CRS = frozenset({"EPSG:3301", "EPSG:2169", "EPSG:2180"})
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
# Finsko (CC BY 4.0): ortofoto a základní mapa MML ze zrcadla kartat.kapsi.fi (celá země, EPSG:3067), v Helsinkách
# navíc ortofoto 5 cm a kantakartta města (kartta.hel.fi) – jen na území YLRE (``TECHNICAL_AREA_KEYS``).
FI_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("fi_ortho", "ortokuva_mml.xml", "Ortokuva MML (kartat.kapsi.fi)", FI_KAPSI_ORTHO_WMS, FI_KAPSI_ORTHO_LAYER, "image/jpeg",
     False, 0.5),
    ("fi_basemap", "peruskartta_mml.xml", "Peruskartta MML 1 : 20 000 (kartat.kapsi.fi)", FI_KAPSI_BASEMAP_WMS,
     FI_KAPSI_BASEMAP_LAYER, "image/png", False, 1.0),
    ("fi_cadastre", "kiinteistorajat_mml.xml", "Kiinteistörajat MML (parcely, kartat.kapsi.fi)", FI_KAPSI_CADASTRE_WMS,
     FI_KAPSI_CADASTRE_LAYER, "image/png", True, 0.25),
    ("fi_hel_ortho", "ortoilmakuva_helsinki.xml", "Ortoilmakuva Helsinki (5 cm)", FI_HEL_WMS, FI_HEL_ORTHO_LAYER,
     "image/jpeg", False, 0.25),
    ("fi_hel_basemap", "kantakartta_helsinki.xml", "Kantakartta Helsinki", FI_HEL_WMS, FI_HEL_BASEMAP_LAYER,
     "image/jpeg", False, 0.5),  # barevná mapa s vyhlazením: PNG mělo 19 MB na 2 km² (0,25 m) a 22 MB na 8 km² (0,5 m)
)
# Španělské regiony (IGN, CC BY 4.0 scne.es): ortofoto PNOA máxima actualidad (EPSG:25830).
ES_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("es_ortho", "ortho_pnoa.xml", "Ortofoto PNOA (IGN)", ES_PNOA_WMS, ES_PNOA_LAYER, "image/jpeg", False, 0.25),
)
# Lucembursko (ACT, CC0): ortofoto, topografická mapa 1 : 5 000 a parcely PCN z WMS geoportail.lu (EPSG:2169).
LU_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("lu_ortho", "ortho_act.xml", "Orthophoto ACT (geoportail.lu)", LU_WMS, LU_ORTHO_LAYER, "image/jpeg", False, 0.25),
    ("lu_topo", "topo5k_act.xml", "Carte topographique 1 : 5 000 (ACT)", LU_WMS, LU_TOPO_LAYER, "image/jpeg", False, 0.5),
    ("lu_cadastre", "parcelles_act.xml", "Katastr (parcelles PCN)", LU_WMS, LU_CADASTRE_LAYER, "image/png", True, 0.25),
)
# Severní Porýní-Vestfálsko (Geobasis NRW, dl-de/zero): ortofoto DOP, Amtliche Basiskarte 1 : 5 000 a parcely ALKIS (EPSG:25832).
DE_NW_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("de_nw_dop", "dop_nrw.xml", "DOP NRW (ortofoto)", DE_NW_DOP_WMS, DE_NW_DOP_LAYER, "image/jpeg", False, 0.25),
    ("de_nw_abk", "abk_nrw.xml", "Amtliche Basiskarte NRW 1 : 5 000 (ABK)", DE_NW_ABK_WMS, DE_NW_ABK_LAYER, "image/png",
     False, 0.25),
    ("de_nw_alkis", "alkis_nrw.xml", "ALKIS NRW – parcely", DE_NW_ALKIS_WMS, DE_NW_ALKIS_LAYER, "image/png", True, 0.25),
)
# Polsko (GUGiK, bezplatně, uvést zdroj): ortofoto a topografická mapa (EPSG:2180, osy sever–východ).
PL_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("pl_ortho", "ortofoto_gugik.xml", "Ortofotomapa GUGiK", PL_ORTHO_WMS, PL_ORTHO_LAYER, "image/jpeg", False, 0.25),
    ("pl_topo", "mapa_topo_gugik.xml", "Mapa topograficzna GUGiK", PL_TOPO_WMS, PL_TOPO_LAYER, "image/png", False, 1.0),
)
# Bavorsko (LDBV OpenData, CC BY 4.0): ortofoto DOP40, Digitale Ortskarte 1 : 10 000 a ALKIS Parzellarkarte (EPSG:25832).
DE_BY_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("de_by_dop", "dop_bayern.xml", "DOP40 Bayern (ortofoto)", BY_DOP_WMS, BY_DOP_LAYER, "image/jpeg", False, 0.4),
    ("de_by_dok", "dok_bayern.xml", "Digitale Ortskarte Bayern 1 : 10 000 (DOK)", BY_DOK_WMS, BY_DOK_LAYER, "image/png",
     False, 0.5),
    ("de_by_alkis", "alkis_bayern.xml", "ALKIS Parzellarkarte Bayern", BY_ALKIS_WMS, BY_ALKIS_LAYER, "image/png", True,
     0.25),
)
# Slovensko (ÚGKK SR, S-JTSK): ortofoto ZBGIS (obrázek licence do ZIPu nepouští, jen online), katastr ESKN a mapa ZBGIS.
SK_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("sk_ortho", "ortofoto_zbgis.xml", "Ortofotomozaika SR (ZBGIS)", SK_ORTHO_WMS, SK_ORTHO_LAYERS, "image/jpeg", False,
     0.15),
    ("sk_zbgis", "mapa_zbgis.xml", "ZBGIS (topografická mapa ÚGKK SR)", SK_ZBGIS_WMS, SK_ZBGIS_LAYERS, "image/png", False,
     0.5),
    ("sk_cadastre", "katastr_eskn.xml", "Katastrální mapa ESKN (parcely)", SK_CADASTRE_WMS, SK_CADASTRE_LAYERS, "image/png",
     True, 0.25),
)
# Anglie: parcely HM Land Registry (INSPIRE, obrázek do ZIPu ne – geometrie OS pod INSPIRE end user licence).
GB_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("gb_cadastre", "inspire_parcels_hmlr.xml", "INSPIRE Index Polygons (HM Land Registry, parcely)", GB_HMLR_WMS,
     GB_HMLR_LAYER, "image/png", True, 0.25),
)
# Norsko (Kartverket): topografická mapa a matrikkelkart (obrázek matrikkelkart do ZIPu ne – nejasná licence).
NO_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("no_topo", "topo_kartverket.xml", "Topografisk norgeskart (Kartverket)", NO_TOPO_WMS, NO_TOPO_LAYER, "image/png",
     False, 1.0),
    ("no_cadastre", "matrikkelkart.xml", "Matrikkelkart (Kartverket, parcely)", NO_MATRIKKEL_WMS, NO_MATRIKKEL_LAYER,
     "image/png", True, 0.25),
)
US_ONLINE_WMS_LAYERS: tuple[tuple[str, str, str, str, str, str, bool, float], ...] = (
    ("us_ortho", "orthophoto_usgs.xml", "USGS Imagery (NAIP, ortofoto)", US_IMAGERY_WMS, US_WMS_LAYER, "image/jpeg",
     False, 0.5),
    ("us_topo", "topo_usgs.xml", "USGS Topo (The National Map)", US_TOPO_WMS, US_WMS_LAYER, "image/png", False, 1.0),
)
# Dlaždicové služby v EPSG:3857 (GDAL TMS + okno výřezu jako VRT): klíč, soubor, popisek, URL, úroveň, m/px, snímek (JPEG).
AT_ONLINE_TMS: tuple[tuple[str, str, str, str, int, float, bool], ...] = (
    ("at_ortho", "orthofoto_at.vrt", "basemap.at Orthofoto (Rakousko)", AT_BASEMAP_ORTHO_URL, 19, 0.25, True),
    ("at_basemap", "basemap_at.vrt", "basemap.at (šedá mapa)", AT_BASEMAP_GRAU_URL, 18, 0.5, False),
    ("at_relief", "gelaende_at.vrt", "basemap.at Gelände (stínování terénu)", AT_BASEMAP_RELIEF_URL, 17, 1.0, True),
    ("at_luftbild", "luftbild_wien.vrt", "Luftbild Wien (ortofoto Stadt Wien)", AT_WIEN_LB_TILE_URL, AT_WIEN_LB_ZOOM, 0.15,
     True),
)
ONLINE_TMS_LAYERS = {"AT": AT_ONLINE_TMS}
# Vrstvy jen pro území technické mapy (``technical_map_area``: Vídeň, Helsinky): mimo něj jsou prázdné.
TECHNICAL_AREA_KEYS = frozenset({"at_fmzk", "at_luftbild", "fi_hel_ortho", "fi_hel_basemap"})
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
        "LI": LI_ONLINE_WMS_LAYERS,
        "AT": AT_ONLINE_WMS_LAYERS,
        "DE": DE_ONLINE_WMS_LAYERS,
        "FR": FR_ONLINE_WMS_LAYERS,
        "EE": EE_ONLINE_WMS_LAYERS,
        "IT": IT_ONLINE_WMS_LAYERS,
        "BE": BE_ONLINE_WMS_LAYERS,
        "FI": FI_ONLINE_WMS_LAYERS,
        "LU": LU_ONLINE_WMS_LAYERS,
        "DE-NW": DE_NW_ONLINE_WMS_LAYERS,
        "PL": PL_ONLINE_WMS_LAYERS,
        "DE-BY": DE_BY_ONLINE_WMS_LAYERS,
        "SK": SK_ONLINE_WMS_LAYERS,
        "GB-ENG": GB_ONLINE_WMS_LAYERS,
        "GB-WLS": GB_ONLINE_WMS_LAYERS,  # INSPIRE Index Polygons HMLR kryjí Anglii i Wales
        "ES-NC": ES_ONLINE_WMS_LAYERS,
        "ES-PV": ES_ONLINE_WMS_LAYERS,
        "ES-AN": ES_ONLINE_WMS_LAYERS,
        "ES-CT": ES_ONLINE_WMS_LAYERS,
        "NO": NO_ONLINE_WMS_LAYERS,
        "US": US_ONLINE_WMS_LAYERS,
        **{st.code: st.wms for st in DE_WCS_STATES.values()},
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
    technical_area: bool = True,
) -> dict[str, Path]:
    """Zapíše online šablony do ``out_dir`` (``references``); klíč ``online_<vrstva>`` → soubor.

    ``only``: jen vybrané vrstvy (např. ``ONLINE_ONLY_KEYS`` vedle stažených rastrů), OSM jen je-li v něm.
    ``country``: země výřezu – místní služby (Česko ČÚZK, Nizozemsko PDOK); OSM všude.
    ``technical_area``: výřez zasahuje do území technické mapy; jinak bez vrstev ``TECHNICAL_AREA_KEYS``.
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
    for key, filename, _label, url, zoom, mpp, _photo in ONLINE_TMS_LAYERS.get(country, ()):
        if (only is not None and key not in only) or (not technical_area and key in TECHNICAL_AREA_KEYS):
            continue
        tms = out_dir / _tms_name(filename)
        tms.write_text(osm_tms_xml(zoom, url), encoding="utf-8")
        for suffix, factor, _lsuffix in ZOOM_VARIANTS:
            vrt = out_dir / f"{Path(filename).stem}{suffix}.vrt"
            if osm_sjtsk_vrt(tms, vrt, bounds_5514, mpp * factor):
                written[f"online_{key}{suffix}"] = vrt
        if not any(k.startswith(f"online_{key}") for k in written):
            tms.unlink(missing_ok=True)
    for key, filename, _label, url, layer, fmt, transparent, mpp in wms_layers_for(country):
        if (only is not None and key not in only) or (not technical_area and key in TECHNICAL_AREA_KEYS):
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


def _tms_name(filename: str) -> str:
    """Dlaždicová šablona vedle okna výřezu (``luftbild_wien.vrt`` → ``luftbild_wien_dlazdice.xml``)."""
    return f"{Path(filename).stem}_dlazdice.xml"


# Fotografické podklady do JPEG (menší ZIP), mapy a průhledné vrstvy do PNG.
PHOTO_TMS_KEYS = frozenset(k for layers in ONLINE_TMS_LAYERS.values() for k, *_r, photo in layers if photo)
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


TEMPLATE_DOWNLOAD_ATTEMPTS = 2
TEMPLATE_RETRY_WAIT_S = 5.0


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
    wld = dest.with_suffix(".wld")
    result = None
    for attempt in range(TEMPLATE_DOWNLOAD_ATTEMPTS):  # servery občas vrátí chybu dlaždice a podruhé projdou
        if attempt:
            time.sleep(TEMPLATE_RETRY_WAIT_S)
        try:
            result = subprocess.run(cmd, cwd=src.parent, capture_output=True, text=True, timeout=900, check=False)
        except subprocess.TimeoutExpired:
            result = None
            break  # 15 minut na šablonu stačí, znovu se nezkouší
        if result.returncode == 0 and dest.is_file() and wld.is_file():
            break
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

    from app.pipeline.zip_images import is_withheld

    # Zdroje, jejichž obrázek licence do ZIPu nepouští (zip_images), zůstanou online šablonou.
    main = {
        k: p
        for k, p in written.items()
        if not any(k.endswith(sfx) for sfx, _f, _l in ZOOM_VARIANTS if sfx) and not is_withheld(k)
    }
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
    tms_names = [_tms_name(f) for layers in ONLINE_TMS_LAYERS.values() for _k, f, *_r in layers]
    for tms in ("bgt_dlazdice.xml", *tms_names, "osm_dlazdice.xml"):
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
            *BE_ONLINE_WMS_LAYERS, *FI_ONLINE_WMS_LAYERS, *LU_ONLINE_WMS_LAYERS, *DE_NW_ONLINE_WMS_LAYERS,
            *PL_ONLINE_WMS_LAYERS, *DE_BY_ONLINE_WMS_LAYERS, *SK_ONLINE_WMS_LAYERS, *GB_ONLINE_WMS_LAYERS, *NO_ONLINE_WMS_LAYERS,
            *ES_ONLINE_WMS_LAYERS,
            *(w for st in DE_WCS_STATES.values() for w in st.wms),
        )
    ] + [(NL_ONLINE_BGT[0], NL_ONLINE_BGT[2]), (ONLINE_OSM[0], ONLINE_OSM[2])]
    layers += [(k, label) for tms in ONLINE_TMS_LAYERS.values() for k, _f, label, *_r in tms]
    return {
        f"online_{k}{suffix}": f"{label}{lsuffix}"
        for k, label in layers
        for suffix, _factor, lsuffix in ZOOM_VARIANTS
    }


# Popisky šablon v anglickém README (job v en); česky zůstávají popisky v tabulkách výše (README a názvy pro cs).
ONLINE_LABELS_EN = {
    "orthophoto": "ČÚZK orthophoto",
    "orthophoto_archiv": "ČÚZK archive orthophoto 2003–2005",
    "ztm": "ČÚZK base map (ZTM)",
    "katastr": "Cadastral map",
    "dtm_plochy": "Regional DTM – areas (ZPS)",
    "dtm_linie": "Regional DTM – lines and points (ZPS)",
    "dmpok": "DMP OK preview",
    "sklon": "Terrain slope DMR 5G",
    "nl_luchtfoto": "Luchtfoto PDOK (orthophoto)",
    "nl_top10nl": "BRT TOP10NL (topographic map)",
    "nl_kadaster": "Kadastrale kaart (cadastre)",
    "nl_ahn": "AHN – terrain model (coloured)",
    "nl_bgt": "BGT (large-scale map)",
    "ch_swissimage": "SWISSIMAGE (swisstopo orthophoto)",
    "ch_landeskarte": "swisstopo national map (Landeskarte)",
    "ch_av": "AV – official cadastral survey (Cadastral Web Map)",
    "ch_relief": "swissALTI3D hillshade",
    "at_ortho": "basemap.at Orthofoto (Austria)",
    "at_basemap": "basemap.at (grey map)",
    "at_relief": "basemap.at Gelände (hillshade)",
    "at_luftbild": "Luftbild Wien (City of Vienna orthophoto)",
    "de_truedop": "TrueDOP Berlin 2024 (orthophoto)",
    "de_alkis": "ALKIS Berlin – parcels",
    "fr_ortho": "IGN orthophoto (BD ORTHO)",
    "fr_cadastre": "Parcellaire Express (cadastre)",
    "ee_ortho": "Maa- ja Ruumiamet orthophoto",
    "ee_cadastre": "Cadastre (katastriüksused)",
    "it_hillshade": "LiDAR hillshade (Provincia di Trento)",
    "be_ortho": "Orthofoto Vlaanderen (winter 2025)",
    "be_cadastre": "Cadastre (administratieve percelen)",
    "de_st_dop": "DOP20 Sachsen-Anhalt (orthophoto)",
    "de_st_dtk": "DTK25 Sachsen-Anhalt (topographic map 1 : 25 000)",
    "de_mv_dop": "DOP Mecklenburg-Vorpommern (orthophoto)",
    "de_mv_dtk": "DTK10 Mecklenburg-Vorpommern (topographic map 1 : 10 000)",
    "de_ni_dop": "DOP20 Niedersachsen (orthophoto)",
    "de_ni_dtk": "DTK25 Niedersachsen (topographic map 1 : 25 000)",
    "de_bb_dop": "DOP20 Brandenburg (orthophoto)",
    "de_rp_dop": "DOP20 Rheinland-Pfalz (orthophoto)",
    "de_rp_dtk": "DTK25 Rheinland-Pfalz (topographic map 1 : 25 000)",
    "de_bw_dop": "DOP20 Baden-Württemberg (orthophoto)",
    "de_sn_dop": "DOP20 Sachsen (orthophoto)",
    "de_he_dop": "DOP20 Hessen (orthophoto)",
    "de_he_dtk": "DTK25 Hessen (topographic map 1 : 25 000)",
    "de_sl_dop": "DOP20 Saarland (orthophoto)",
    "de_hb_dop": "DOP10 Bremen (orthophoto)",
    "de_sh_dop": "DOP20 Schleswig-Holstein (orthophoto)",
    "at_cadastre": "BEV cadastre (DKM – parcels)",
    "fi_cadastre": "Kiinteistörajat MML (parcels, kartat.kapsi.fi)",
    "gb_cadastre": "INSPIRE Index Polygons (HM Land Registry, parcels)",
    "no_topo": "Topografisk norgeskart (Kartverket)",
    "no_cadastre": "Matrikkelkart (Kartverket, parcels)",
    "de_st_alkis": "ALKIS Sachsen-Anhalt – parcels",
    "de_mv_alkis": "ALKIS Mecklenburg-Vorpommern – parcels",
    "de_bb_alkis": "ALKIS Brandenburg – parcels",
    "de_bw_alkis": "ALKIS Baden-Württemberg – parcels",
    "de_sn_alkis": "ALKIS Sachsen – parcels",
    "de_he_alkis": "Liegenschaftskarte Hessen (ALKIS)",
    "sk_ortho": "Orthophotomosaic of Slovakia (ZBGIS)",
    "sk_zbgis": "ZBGIS (topographic map, ÚGKK SR)",
    "sk_cadastre": "ESKN cadastral map (parcels)",
}


def wms_readme_lines(country: str = "CZ", lang: str = "cs") -> list[str]:
    """Adresy WMS pro ruční připojení (OCAD, QGIS): popis – URL – vrstva; ``lang`` = jazyk README (cs beze změny)."""
    crs = "S-JTSK EPSG:5514" if country == "CZ" else CRS_LABEL
    en = lang == "en"

    def _label(key: str, label: str) -> str:
        return ONLINE_LABELS_EN.get(key, label) if en else label

    seen: list[str] = []
    for key, _f, label, url, layer, *_rest in wms_layers_for(country):
        native = WMS_NATIVE_CRS.get(key)
        if en:
            where = f"{native}, the server reprojects other systems" if native else crs
            seen.append(f"- {_label(key, label)}: {url} (layer {layer}, {where})")
        else:
            seen.append(f"- {label}: {url} (vrstva {layer}, {f'{native}, jiný systém server posune' if native else crs})")
    if country == "NL":
        seen.append(
            f"- {_label(NL_ONLINE_BGT[0], NL_ONLINE_BGT[2])}: WMTS {NL_BGT_TILE_URL.split('/standaard')[0]} "
            "(standaardvisualisatie, EPSG:28992)"
        )
    for key, _f, label, url, *_r in ONLINE_TMS_LAYERS.get(country, ()):
        seen.append(f"- {_label(key, label)}: WMTS {url.split('/google3857')[0]} (EPSG:3857)")
    return seen
