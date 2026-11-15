"""Služby pro Rakousko (zahraniční build, zatím jen Vídeň): výšky BEV, FMZK a ortofoto Stadt Wien.

Bez klíče; BEV ALS CC BY 4.0 („Datenquelle: BEV“), Stadt Wien OGD CC BY 4.0 („Datenquelle: Stadt Wien –
data.wien.gv.at“; citace v ``configs/countries.yaml``). Stav služeb hlídá ``app.source_status``.
"""

from __future__ import annotations

AT_WIEN_WMS = "https://data.wien.gv.at/daten/geo"
# FMZK jako WMS (GeoServer Stadt Wien, umí souřadnice jobu); styl „default“ GeoServer odmítá.
AT_FMZK_WMS_LAYERS = "FMZKGRUEN1OGD,FMZKGRUEN2OGD,FMZKVERKEHR1OGD,FMZKVERKEHR2OGD,FMZKGEBOGD,FMZKMAUOGD"
# Ortofoto Vídně (Luftbild, nejnovější) jen jako WMTS v EPSG:3857 → GDAL TMS + okno výřezu jako VRT.
AT_WIEN_LB_TILE_URL = "https://maps.wien.gv.at/wmts/lb/farbe/google3857/${z}/${y}/${x}.jpeg"
AT_WIEN_LB_ZOOM = 19  # ~0,3 m/px ve Vídni
# Sondy: hlavička dlaždice BEV (GET by stáhl 10 GB), 1 objekt FMZK, 1 dlaždice ortofota.
AT_BEV_PROBE = (
    "https://data.bev.gv.at/download/ALS/DTM/20250915/ALS_DTM_CRS3035RES50000mN2800000E4750000.tif"
)
AT_FMZK_PROBE = (
    f"{AT_WIEN_WMS}?service=WFS&version=1.1.0&request=GetFeature&typeName=ogdwien:FMZKGEBOGD&maxFeatures=1"
    "&outputFormat=json"
)
AT_LB_PROBE = "https://maps.wien.gv.at/wmts/lb/farbe/google3857/18/90898/142995.jpeg"
