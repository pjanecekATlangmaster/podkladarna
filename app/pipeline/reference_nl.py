"""Služby PDOK pro Nizozemsko (zahraniční build): výšky, ortofoto, BGT, katastr a TOPO.

Vše bez klíče a s licencí, která dovoluje komerční použití (citace v ``configs/countries.yaml``):
AHN CC0 (novější CC BY 4.0), Luchtfoto Beeldmateriaal CC BY 4.0, BGT CC0, Kadaster (katastr, TOP10NL) CC BY 4.0.
Stav služeb hlídá ``app.source_status`` (HEAD na GetCapabilities), šablony píše ``online_templates``.
"""

from __future__ import annotations

NL_AHN_WCS = "https://service.pdok.nl/rws/ahn/wcs/v1_0"
NL_LUCHTFOTO_WMS = "https://service.pdok.nl/hwh/luchtfotorgb/wms/v1_0"
NL_LUCHTFOTO_LAYER = "Actueel_orthoHR"  # nejnovější snímkování 7,5–8 cm
NL_KADASTER_WMS = "https://service.pdok.nl/kadaster/kadastralekaart/wms/v5_0"
NL_KADASTER_LAYER = "Kadastralekaart"
NL_TOP10NL_WMS = "https://service.pdok.nl/brt/top10nl/wms/v1_0"
NL_TOP10NL_LAYER = "top10nl"
# BGT jako WMS není veřejně (401), jen WMTS v mřížce EPSG:28992 (dlaždice 256 px, úroveň 0 = 3440,64 m/px).
NL_BGT_WMTS = "https://service.pdok.nl/lv/bgt/wmts/v1_0"
NL_BGT_TILE_URL = "https://service.pdok.nl/kadaster/bgt/wmts/v1_0/standaardvisualisatie/EPSG:28992/${z}/${x}/${y}.png"
RD_TILE_ORIGIN = (-285401.92, 903401.92)
RD_TILE_RES0 = 3440.64
RD_TILE_MAX_LEVEL = 16  # 0,0525 m/px
