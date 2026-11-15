"""Služby pro Flandry (zahraniční build): výšky DHMV II, GRB, ortofoto a katastr Digitaal Vlaanderen.

Bez klíče; Modellicentie Gratis Hergebruik (uvést „Digitaal Vlaanderen“, citace v ``configs/countries.yaml``).
Stav služeb hlídá ``app.source_status``.
"""

from __future__ import annotations

BE_OMW_WMS = "https://geo.api.vlaanderen.be/OMW/wms"
BE_ORTHO_LAYER = "OMWRGB25VL"  # ortofoto zima 2025 (bez listí)
BE_GRB_WMS = "https://geo.api.vlaanderen.be/GRB/wms"
BE_GRB_LAYER = "GRB_BSK"  # GRB-basiskaart
BE_ADP_WMS = "https://geo.api.vlaanderen.be/Adpf/wms"
BE_ADP_LAYER = "GrAdpf"  # hranice katastrálních parcel (nejnovější fiskální stav)
# Sondy: malé okno DTM z WCS (Gent), 1 budova GRB, malý výřez ortofota.
BE_ELEVATION_PROBE = (
    "https://geo.api.vlaanderen.be/DHMV/wcs?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage&COVERAGE=DHMVII_DTM_1m"
    "&CRS=EPSG:31370&BBOX=104200,193200,104208,193208&WIDTH=8&HEIGHT=8&FORMAT=GeoTIFF"
)
BE_GRB_PROBE = (
    "https://geo.api.vlaanderen.be/GRB/wfs?service=WFS&version=2.0.0&request=GetFeature"
    "&typeNames=GRB:GBG&count=1&outputFormat=application/json"
)
BE_ORTHO_PROBE = (
    f"{BE_OMW_WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS={BE_ORTHO_LAYER}&STYLES=&SRS=EPSG:31370"
    "&BBOX=104200,193200,104208,193208&WIDTH=16&HEIGHT=16&FORMAT=image/jpeg"
)
