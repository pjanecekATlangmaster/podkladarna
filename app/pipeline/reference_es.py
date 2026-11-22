"""Služby pro španělské regiony (zahraniční build): klasifikované mračno LiDAR regionálních vlád po listech.

- **Navarra** (Gobierno de Navarra, CC BY 4.0 – „Términos de uso“ na filescartografia.navarra.es): let 2024, sensor
  Optech Galaxy T2000, ~5 bodů/m² (země ~1,7/m²), LAZ 1.4 bodový formát 8 po listech 1 × 1 km (+25 m přesah, ~80 MB):
  ``5_LIDAR/5_6_2024_NAV_cam_EPSG25830/las_cam_<E km>-<N km>_2024_NAV_EPSG25830.laz``, kde E/N je **levý horní** roh.
  EPSG:25830, výšky **elipsoidické** (ověřeno 2026-11-20 proti MDT 2 m: rozdíl 49,4–49,9 m ≈ EGM08-REDNAP 50,0 m)
  → odečte se geoid IGN z PROJ-data. Třídy ASPRS: 2 země, 3–5 vegetace, 6 budovy, 9 voda, 11 vozovka, 17 mosty,
  7 šum; vlastní 64+ se neberou. Seznam listů z výpisu adresáře (HTTPS, Range podporuje).

- **Baskicko** (Eusko Jaurlaritza / Gobierno Vasco, geoEuskadi, CC BY 4.0 – licence celého webu geo.euskadi.eus): let
  2017, LAZ 1.2 bodový formát 3 po listech 500 × 500 m bez přesahu (~6,7 bodu/m², země ~1,3/m², do ~50 MB) v podadresářích
  podle listu MTN50: ``LIDAR/LIDAR_2017_ETRS89/<list>/<E hm>-<N hm>.laz``, kde E/N v hektometrech je **levý dolní** roh.
  EPSG:25830, výšky **ortometrické** (ověřeno 2026-11-20 proti MDT 1 m: medián rozdílu bodů země 0,03 m). Třídy ASPRS:
  2 země, 3–5 vegetace, 6 budovy, 7 šum. Výpis adresářů občas vrací 503 (opakuje se).

- **Andalusie** (Junta de Andalucía, REDIAM, CC BY 4.0): MDT a MDS 1 m z PNOA LiDAR 2. pokrytí (2014–15, ~1,5 bodu/m²)
  jako dva COG na celé území (BigTIFF, float32, dlaždice 512) ve veřejném sdílení Nextcloud REDIAM. Čte se okno přes
  WebDAV veřejného sdílení: Basic auth s tokenem sdílení a prázdným heslem + hlavička ``X-Requested-With`` (bez ní 401).
  EPSG:25830 (UTM 30 rozšířené na celé území), výšky **ortometrické** („Horto“). NoData: MDT 255 (!), MDS −9999 – bere se
  NoData souboru, nic se nepřepisuje.

- **Katalánsko** (ICGC, CC BY 4.0): MET a MDS 1 m z LiDAR Territorial v3 (2021–23) jako dva COG na celé území
  (BigTIFF, float32, dlaždice 512) na ``datacloud.icgc.cat``. EPSG:25831, výšky ortometrické. Z cloudu Claude
  datacloud.icgc.cat neodpovídá (z české sítě ano, Range ověřen 2026-11-22) – ověřit z ostré A.

- Ortofoto PNOA (IGN, CC BY 4.0 „scne.es“, bez klíče): WMS ``pnoa-ma`` v EPSG:25830 pro celé Španělsko.

Stav služby hlídá ``app.source_status``.
"""

from __future__ import annotations

from dataclasses import dataclass

ES_CRS = "EPSG:25830"  # ETRS89 / UTM zone 30N
# Geoid EGM08-REDNAP (IGN, PROJ-data, CC BY 4.0), elipsoidické výšky ETRS89 → nadmořské (Alicante), ~0,8 MB.
ES_GEOID_URL = "https://cdn.proj.org/es_ign_egm08-rednap.tif"


@dataclass(frozen=True)
class EsLazRegion:
    key: str  # provider výšek a složka cache
    label: str  # do logu
    listing: str  # adresář s listy (HTML výpis, končí „/“); URL listu = listing + shoda ``name_re``
    name_re: str  # regulární výraz cesty listu vůči ``listing`` se skupinami E a N (v ``unit_m``)
    tile_m: int  # strana listu v metrech
    top_corner: bool  # N ve jménu je horní okraj listu (jinak dolní)
    ground: tuple[int, ...]  # třídy terénu
    surface: tuple[int, ...]  # třídy povrchu (maximum v buňce)
    ellipsoidal: bool  # výšky elipsoidické → odečíst geoid
    probe: str  # malý list pro HEAD sondu
    unit_m: int = 1000  # jednotka E a N ve jménu listu (Navarra km, Baskicko hm)
    subdir_re: str = ""  # listy jsou v podadresářích výpisu: regulární výraz jména podadresáře (skupina 1)
    workers: int = 3  # souběžná stažení listů v jednom jobu


NAV_BASE = "https://filescartografia.navarra.es/5_LIDAR/5_6_2024_NAV_cam_EPSG25830"
PV_BASE = "https://www.geo.euskadi.eus/lidar/DatosDescarga/LIDAR/LIDAR_2017_ETRS89"

ES_LAZ_REGIONS: dict[str, EsLazRegion] = {
    "es_nav": EsLazRegion(
        key="es_nav",
        label="Navarra LiDAR 2024",
        listing=f"{NAV_BASE}/",
        name_re=r"las_cam_(\d{3})-(\d{4})_2024_NAV_EPSG25830\.laz",
        tile_m=1000,
        top_corner=True,
        ground=(2, 11),
        surface=(2, 3, 4, 5, 6, 9, 11, 17),
        ellipsoidal=True,
        probe=f"{NAV_BASE}/las_cam_610-4741_2024_NAV_EPSG25830.laz",
    ),
    "es_pv": EsLazRegion(
        key="es_pv",
        label="Euskadi LiDAR 2017",
        listing=f"{PV_BASE}/",
        name_re=r"\d{3}/(\d{4})-(\d{5})\.laz",
        tile_m=500,
        top_corner=False,
        ground=(2,),
        surface=(2, 3, 4, 5, 6),
        ellipsoidal=False,
        probe=f"{PV_BASE}/060/4655-47870.laz",
        unit_m=100,
        subdir_re=r'HREF="[^"]*/LIDAR_2017_ETRS89/(\d{3})/"',
        workers=2,  # server pod souběžnými staženími vrací 503
    ),
}

NAV_ELEVATION_PROBE = ES_LAZ_REGIONS["es_nav"].probe
PV_ELEVATION_PROBE = ES_LAZ_REGIONS["es_pv"].probe

# Andalusie: veřejné sdílení REDIAM (Nextcloud), WebDAV s tokenem sdílení jako jménem a prázdným heslem.
AN_SHARE_TOKEN = "mxHMWXyHfrCxyNK"
AN_SHARE_PAGE = f"https://portalrediam.cica.es/descargas/index.php/s/{AN_SHARE_TOKEN}"
AN_COG_BASE = (
    f"https://portalrediam.cica.es/descargas/public.php/dav/files/{AN_SHARE_TOKEN}/01_CARACTERIZACION_TERRITORIO/"
    "07_BASES_REF_ELEV/01_ELEVACIONES/01_PROYECTOS_REGIONALES/2020_AND_PNOA_LIDAR_V226_el/InfGeografica/InfRaster"
)
AN_DTM_URL = f"{AN_COG_BASE}/MDT/ETRS89_h30_Horto/COG/Mos_1m/Mos_MDT_1m_COG.tif"
AN_DSM_URL = f"{AN_COG_BASE}/MDS/ETRS89_h30_Horto/COG/Mos_1m/Mos_MDS_1m_COG.tif"
AN_GDAL_CONFIG = {
    "GDAL_HTTP_USERPWD": f"{AN_SHARE_TOKEN}:",
    "GDAL_HTTP_AUTH": "BASIC",
    "GDAL_HTTP_HEADERS": "X-Requested-With: XMLHttpRequest",
}
AN_ELEVATION_PROBE = AN_SHARE_PAGE  # HEAD stránky sdílení: zrušené sdílení = 404

# Katalánsko: ICGC datacloud, jeden COG na model.
CT_CRS = "EPSG:25831"
CT_COG_BASE = "https://datacloud.icgc.cat/datacloud"
CT_DTM_URL = f"{CT_COG_BASE}/model-elevacions-terreny/tif_unzip/model-elevacions-terreny-lidar-catalunya-1m-2021-2023.tif"
CT_DSM_URL = f"{CT_COG_BASE}/model-superficies/tif_unzip/model-superficies-lidar-catalunya-1m-2021-2023.tif"
CT_ELEVATION_PROBE = CT_DTM_URL

# Ortofoto PNOA máxima actualidad (IGN, CC BY 4.0 scne.es) – šablona a maska zpevněných ploch sprintu.
ES_PNOA_WMS = "https://www.ign.es/wms-inspire/pnoa-ma"
ES_PNOA_LAYER = "OI.OrthoimageCoverage"
ES_ORTHO_PROBE = (
    f"{ES_PNOA_WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS={ES_PNOA_LAYER}&STYLES=&SRS=EPSG:25830"
    "&BBOX=610000,4740000,610016,4740016&WIDTH=16&HEIGHT=16&FORMAT=image/jpeg"
)
