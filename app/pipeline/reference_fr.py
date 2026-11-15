"""Služby pro Francii (zahraniční build, zatím jen Paříž): výšky LiDAR HD, Plan de voirie, ortofoto, plán, katastr.

IGN (Géoplateforme, data.geopf.fr) bez klíče, Licence Ouverte Etalab 2.0; Plan de voirie Ville de Paris (ODbL).
Citace v ``configs/countries.yaml``, stav služeb hlídá ``app.source_status``.
"""

from __future__ import annotations

FR_WMS_R = "https://data.geopf.fr/wms-r/wms"
# WMS-R Géoplateforme (Lambert-93 i další CRS); styl „default“ odmítá (400), prázdný styl ano.
FR_ORTHO_LAYER = "ORTHOIMAGERY.ORTHOPHOTOS"
FR_PLAN_LAYER = "GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2"
FR_CADASTRE_LAYER = "CADASTRALPARCELS.PARCELLAIRE_EXPRESS"
# Sondy: malý výřez MNT LiDAR HD a ortofota (Hôtel de Ville), 1 záznam Plan de voirie.
_PROBE_BBOX = "&CRS=EPSG:2154&BBOX=652400,6862200,652408,6862208&WIDTH=16&HEIGHT=16"
FR_LIDAR_PROBE = (
    f"{FR_WMS_R}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"
    f"&STYLES={_PROBE_BBOX}&FORMAT=image/geotiff"
)
FR_ORTHO_PROBE = f"{FR_WMS_R}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS={FR_ORTHO_LAYER}&STYLES={_PROBE_BBOX}&FORMAT=image/jpeg"
FR_PDV_PROBE = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/plan-de-voirie-chaussees/records?limit=1"
