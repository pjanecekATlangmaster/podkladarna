"""Služby pro Lucembursko (zahraniční build): výšky LiDAR 2024, ortofoto, topografická mapa a katastr ACT.

Bez klíče, vše CC0 1.0 (Administration du cadastre et de la topographie, data.public.lu; citace v
``configs/countries.yaml`` jen ze zdvořilosti):

- MNT a MNS 0,5 m z LiDARu 2024 (únor–duben, > 30 bodů/m²): jeden COG na celou zemi (EPSG:2169, Float64, bloky
  128 px) – čte se jen okno výřezu přes /vsicurl, nic se nestahuje celé (MNT 40 GB),
- WMS geoportail.lu „opendata“: ortofoto (nejnovější ročník), topografická mapa 1 : 5 000 a parcely PCN.

EPSG:2169 (LUREF) má v EPSG osy sever–východ: WMS 1.3.0 chce BBOX v pořadí N, E (``NORTHING_FIRST_CRS``),
sondy proto používají WMS 1.1.1 (vždy E, N). Stav hlídá ``app.source_status``.
"""

from __future__ import annotations

LU_LIDAR_BASE = (
    "https://download.data.public.lu/resources/bd-l-lidar2024-releve-3d-du-territoire-luxembourgeois/20241223-093912"
)
LU_DTM_URL = f"{LU_LIDAR_BASE}/MNT_Lidar2024.tif"
LU_DSM_URL = f"{LU_LIDAR_BASE}/MNS_Lidar2024.tif"
LU_WMS = "https://wms.geoportail.lu/opendata/service"
LU_ORTHO_LAYER = "ortho_latest"  # nejnovější letní ortofoto (10 cm)
LU_TOPO_LAYER = "topo_5k"  # topografická mapa 1 : 5 000
LU_CADASTRE_LAYER = "parcels"  # parcely PCN (průhledné)
# Sondy: hlavička COG terénu (HEAD na S3), malý výřez ortofota (Lucemburk, WMS 1.1.1 = pořadí E, N).
LU_ELEVATION_PROBE = LU_DTM_URL
LU_ORTHO_PROBE = (
    f"{LU_WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS={LU_ORTHO_LAYER}&STYLES=&SRS=EPSG:2169"
    "&BBOX=77000,75000,77016,75016&WIDTH=16&HEIGHT=16&FORMAT=image/jpeg"
)
