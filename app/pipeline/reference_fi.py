"""Služby pro Finsko (zahraniční build): výšky MML a Metsäkeskus, Helsinky YLRE, ortofoto a základní mapa.

Bez klíče, CC BY 4.0 (citace v ``configs/countries.yaml``):

- výškový model 2 m Maanmittauslaitos (MML) ze zrcadla Funet (CSC), listy TM35 1 : 10 000, GeoTIFF,
- model výšky porostu (latvusmalli) 1 m Suomen metsäkeskus po listech, GeoTIFF na S3,
- Helsinky: WFS / WMS kartta.hel.fi (avoindata, „Helsingin kaupunki“) – YLRE, budovy, ortofoto 5 cm, kantakartta,
- celé Finsko: WMS zrcadla kartat.kapsi.fi (data MML) – ortofoto a základní mapa (peruskartta).

Přímá rozhraní MML (avoin-karttakuva, tiedostopalvelu) chtějí klíč, proto zrcadla. Stav hlídá ``app.source_status``.
"""

from __future__ import annotations

FI_HEL_WMS = "https://kartta.hel.fi/ws/geoserver/avoindata/wms"
FI_HEL_ORTHO_LAYER = "avoindata:Ortoilmakuva"  # nejnovější ortofoto Helsinek (5 cm)
FI_HEL_BASEMAP_LAYER = "avoindata:Kantakartta"  # velkoměřítková mapa města
FI_KAPSI_ORTHO_WMS = "https://tiles.kartat.kapsi.fi/ortokuva"
FI_KAPSI_ORTHO_LAYER = "ortokuva"
FI_KAPSI_BASEMAP_WMS = "https://tiles.kartat.kapsi.fi/peruskartta"
FI_KAPSI_BASEMAP_LAYER = "peruskartta"
FI_KAPSI_CADASTRE_WMS = "https://tiles.kartat.kapsi.fi/kiinteistorajat"  # kiinteistörajat MML (CC BY 4.0)
FI_KAPSI_CADASTRE_LAYER = "kiinteistorajat"
# Sondy: jeden list výšek a porostu (Helsinky, L4133B) hlavičkou, 1 budova z WFS, malý výřez ortofota.
FI_DTM_PROBE = "https://mirrors.nic.funet.fi/index/geodata/mml/dem2m/2008_latest/L4/L41/L4133B.tif"
FI_CHM_PROBE = "https://avoin.metsakeskus.fi/aineistot/Latvusmalli/Karttalehti/uusin/CHM_L4133B_uusin.tif"
FI_HEL_WFS_PROBE = (
    "https://kartta.hel.fi/ws/geoserver/avoindata/wfs?service=WFS&version=2.0.0&request=GetFeature"
    "&typeNames=avoindata:Rakennukset_alue&count=1&outputFormat=application/json"
)
FI_ORTHO_PROBE = (
    f"{FI_KAPSI_ORTHO_WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS={FI_KAPSI_ORTHO_LAYER}&STYLES=&SRS=EPSG:3067"
    "&BBOX=385600,6672100,385616,6672116&WIDTH=16&HEIGHT=16&FORMAT=image/jpeg"
)
