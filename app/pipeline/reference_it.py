"""Služby pro Itálii – Trentino (zahraniční build): výšky LiDAR, ortofoto a technická mapa Provincia autonoma di Trento.

Bez klíče; CC BY 4.0 (uvést Provincia autonoma di Trento, citace v ``configs/countries.yaml``). Kresba je z OSM
(technická mapa v podrobnosti DTM v Trentinu není). Stav služeb hlídá ``app.source_status``.
"""

from __future__ import annotations

TN_WMS = "https://siat.provincia.tn.it/geoserver/stem/wms"
TN_ORTHO_LAYER = "ecw-rgb-2015"  # ortofoto 2015 (novější PAT veřejně nevydává)
TN_CTP_LAYER = "ctp2025_bn_00"  # Carta Tecnica Provinciale 1 : 10 000, 2025
TN_HILLSHADE_LAYER = "2014_agg_2018_dtm_315_10"  # stínování DTM 2014 doplněného 2018 (po vichřici Vaia)
# Sondy: index dlaždic DTM (1 objekt) a malý výřez ortofota v Trentu.
TN_ELEVATION_PROBE = (
    "https://siat.provincia.tn.it/geoserver/wfs?service=WFS&version=2.0.0&request=GetFeature"
    "&typeNames=stem:inqlid2014_dtm_asc&count=1&outputFormat=application/json"
)
TN_ORTHO_PROBE = (
    f"{TN_WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS={TN_ORTHO_LAYER}&STYLES=&SRS=EPSG:25832"
    "&BBOX=664200,5103200,664208,5103208&WIDTH=16&HEIGHT=16&FORMAT=image/jpeg"
)
