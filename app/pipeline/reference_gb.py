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
