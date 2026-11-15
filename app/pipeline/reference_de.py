"""Služby pro Německo (zahraniční build, zatím jen Berlín): výšky DGM1 a DOM1, Straßenbefahrung, ALKIS, ortofoto.

Vše z GDI Berlin bez klíče, Datenlizenz Deutschland – Zero 2.0 (citace není povinná, uvádíme ji; ``configs/countries.yaml``).
Stav služeb hlídá ``app.source_status``.
"""

from __future__ import annotations

DE_BE_WMS = "https://gdi.berlin.de/services/wms"
# WMS GDI Berlin (GeoServer, EPSG:25833 nativně); styl „default“ odmítá.
DE_BE_TRUEDOP_WMS = f"{DE_BE_WMS}/truedop_2024"
DE_BE_TRUEDOP_LAYER = "truedop_2024"
DE_BE_K5_WMS = f"{DE_BE_WMS}/k5_farbe"
DE_BE_K5_LAYER = "k5_farbe"
DE_BE_ALKIS_WMS = f"{DE_BE_WMS}/alkis_flurstuecke"
DE_BE_ALKIS_LAYER = "flurstuecke"
# Sondy: hlavička dlaždice DGM1, 1 objekt Straßenbefahrung, malý výřez ortofota.
DE_BE_DGM_PROBE = "https://gdi.berlin.de/data/dgm1/atom/DGM1_390_5818.zip"
DE_BE_SB_PROBE = (
    "https://gdi.berlin.de/services/wfs/strassenbefahrung?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
    "&TYPENAMES=strassenbefahrung:cm_fahrbahn&COUNT=1&OUTPUTFORMAT=application/json"
)
DE_BE_DOP_PROBE = (
    f"{DE_BE_TRUEDOP_WMS}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS={DE_BE_TRUEDOP_LAYER}&STYLES="
    "&CRS=EPSG:25833&BBOX=391400,5820600,391416,5820616&WIDTH=16&HEIGHT=16&FORMAT=image/png"
)
