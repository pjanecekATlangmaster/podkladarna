"""Služby pro Estonsko (zahraniční build): výšky a ETAK Maa- ja Ruumiamet, ortofoto, základní mapa, katastr.

Bez klíče; otevřená licence Maa- ja Ruumiamet (uvést zdroj a datum dat, citace v ``configs/countries.yaml``).
Stav služeb hlídá ``app.source_status``.
"""

from __future__ import annotations

EE_FOTO_WMS = "https://kaart.maaamet.ee/wms/fotokaart"
EE_ORTHO_LAYER = "EESTIFOTO"
EE_ALUS_WMS = "https://kaart.maaamet.ee/wms/alus"
EE_BASEMAP_LAYER = "pohi_vr2"  # Eesti põhikaart 1 : 10 000
EE_CADASTRE_WMS = "https://gsavalik.envir.ee/geoserver/kataster/ows"
EE_CADASTRE_LAYER = "ky_kehtiv"  # platné katastrální jednotky
# Sondy: hlavička listu DSM 1 km v centru Tallinnu, 1 objekt ETAK, malý výřez ortofota.
EE_ELEVATION_PROBE = (
    "https://geoportaal.maaruum.ee/index.php?lang_id=2&plugin_act=otsing&kaardiruut=589542"
    "&andmetyyp=ndsm_abs_1m_geotiff&dl=1&f=589542_dsm_1m.tif&page_id=664"
)
EE_ETAK_PROBE = (
    "https://gsavalik.envir.ee/geoserver/etak/ows?service=WFS&version=2.0.0&request=GetFeature"
    "&typeNames=etak:e_401_hoone_ka&count=1&outputFormat=application/json"
)
EE_ORTHO_PROBE = (
    f"{EE_FOTO_WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS={EE_ORTHO_LAYER}&STYLES=&SRS=EPSG:3301"
    "&BBOX=542400,6589000,542408,6589008&WIDTH=16&HEIGHT=16&FORMAT=image/jpeg"
)
