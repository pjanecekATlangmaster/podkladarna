"""Služby swisstopo a kantonů pro Švýcarsko (zahraniční build): výšky, ortofoto, mapa, AV a stínování.

Bez klíče; swisstopo OGD (od 2021 volné použití včetně komerčního, „© swisstopo“), AV kantonů přes geodienste.ch
(většinou „Freie Nutzung, Quellenangabe ist Pflicht“; citace v ``configs/countries.yaml``).
Stav služeb hlídá ``app.source_status``, šablony píše ``online_templates``.
"""

from __future__ import annotations

CH_WMS = "https://wms.geo.admin.ch/"
CH_SWISSIMAGE_LAYER = "ch.swisstopo.swissimage"  # ortofoto SWISSIMAGE 10 cm
CH_PIXELKARTE_LAYER = "ch.swisstopo.pixelkarte-farbe"  # národní mapa (Landeskarte) barevně
CH_CADASTRAL_LAYER = "ch.kantone.cadastralwebmap-farbe"  # AV / katastr kantonů (Cadastral Web Map)
CH_RELIEF_LAYER = "ch.swisstopo.swissalti3d-reliefschattierung"  # stínování swissALTI3D
CH_STAC_ALTI = "https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.swissalti3d"
CH_AV_PROBE = (
    "https://geodienste.ch/db/av_0/deu?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature&TYPENAMES=ms:LCSF&COUNT=1"
    "&BBOX=2600200,1199300,2600300,1199400,urn:ogc:def:crs:EPSG::2056&OUTPUTFORMAT=geojson"
)
CH_WMS_PROBE = (
    f"{CH_WMS}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS={CH_SWISSIMAGE_LAYER}&STYLES=&CRS=EPSG:2056"
    "&BBOX=2600200,1199300,2600216,1199316&WIDTH=16&HEIGHT=16&FORMAT=image/jpeg"  # 1 × 1 px vrací 500
)
