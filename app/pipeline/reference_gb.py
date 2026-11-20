"""Služby pro Anglii (zahraniční build): výšky LIDAR Composite Environment Agency.

Bez klíče, Open Government Licence v3 (uvést „© Environment Agency copyright and/or database right 2022“;
``configs/countries.yaml``). Souřadnice EPSG:27700 (British National Grid, osy východ–sever).

- Terén LIDAR Composite DTM 2022 a povrch First Return DSM 2022, 1 m: jen celé ZIPy dlaždic 5 × 5 km mřížky OS
  (``/tiles/collections/survey/<produkt>/<rok>/<rozlišení>/<dlaždice>``, Float32 GeoTIFF LZW ~70–75 MB, ~5 s).
  WCS ``environment.data.gov.uk/geoservices/datasets/…/wcs`` na výřez nestíhá (504 i na 500 m).
- Ortofoto ani topografická mapa nejsou otevřeně jako služba (OS Data Hub chce klíč) → šablony jen OSM a Sentinel-2.
- Mračno National LiDAR Programme (LAZ po 5 km, stovky MB) se nepoužívá.

Stav hlídá ``app.source_status``.
"""

from __future__ import annotations

GB_EA_TILES = "https://environment.data.gov.uk/tiles/collections/survey"
GB_EA_PRODUCTS = {"dtm": "lidar_composite_dtm/2022/1", "dsm": "lidar_composite_first_return_dsm/2022/1"}

# Sonda: HEAD na dlaždici terénu (Leeds); server HEAD nepodporuje a odpoví 405, výpadek = timeout / 5xx.
GB_EA_PROBE = f"{GB_EA_TILES}/{GB_EA_PRODUCTS['dtm']}/SE2530"

# Katastr: INSPIRE Index Polygons HM Land Registry (WMS, EPSG:27700). Data HMLR pod OGL, geometrie © Crown copyright
# a database rights Ordnance Survey (INSPIRE end user licence, obrázek s vodoznakem podmínek) → jen online šablona.
GB_HMLR_WMS = "https://inspire.landregistry.gov.uk/inspire/ows"
GB_HMLR_LAYER = "inspire:CP.CadastralParcel"

# Skotsko (Scottish Remote Sensing Portal, OGL v3): sonda stavu = HEAD malé dlaždice terénu (1 km, 0,25 MB).
SCT_ELEVATION_PROBE = (
    "https://srsp-open-data.s3.eu-west-2.amazonaws.com/lidar/national-lidar-programme/dtm/27700/gridded/"
    "NG2106_50cm_DTM_ScotlandNationalLiDAR.tif"
)
