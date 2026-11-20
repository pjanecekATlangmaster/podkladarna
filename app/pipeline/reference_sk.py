"""Šablony podkladů pro Slovensko (zahraniční build): ortofoto ZBGIS, katastrální mapa ESKN a mapa ZBGIS.

Bez klíče, služby ÚGKK SR v S-JTSK (EPSG:5514 = souřadnice slovenského jobu, WMS 1.3.0 i 1.1.1).

- Ortofotomozaika SR (GKÚ Bratislava, 3. cyklus 2023–2025, 15 cm): služba CC BY 4.0, ale podmínky zdrojových dat
  ortofota (GKÚ 2018) zakazují šířit data „v původní (dodané) podobě“ – obrázek se proto do ZIPu nedává, v .omap
  zůstává online šablona (``image_in_zip: forbidden`` v ``configs/data_sources.yaml``). Citace „GKÚ Bratislava, NLC“.
- Katastrální mapa ESKN (ÚGKK SR, CC BY 4.0): jen hranice a čísla parcel, průhledná (``kn_wms_norm``); vlastníci ne.
- ZBGIS (GKÚ Bratislava, CC BY 4.0): topografická mapa ze ZBGIS – obdoba výtahu z technické mapy (budovy,
  komunikace, vodstvo, vegetace). Vrstvy služby nemají společnou skupinu, seznam je převzatý z katalogu Freemap.sk.

Služby z cloudu Anthropicu odmítají spojení (reset), proto nemají sondu v ``app.source_status``; Mapper je načítá
z počítače mapaře. Vrstvy ověřené z katalogů QGIS pluginu czech_slovak_freegeodata a Freemap.sk (2026-11-20).
"""

from __future__ import annotations

SK_ORTHO_WMS = "https://zbgisws.skgeodesy.sk/zbgis_ortofoto_wms/service.svc/get"
SK_ORTHO_LAYERS = "3,2,1"  # 1 = nejnovější a nejostřejší, kreslí se navrch (Petr 2026-11-20 v QGIS); 2, 3 doplní mezery
SK_CADASTRE_WMS = "https://kataster.skgeodesy.sk/eskn/services/NR/kn_wms_norm/MapServer/WmsServer"
SK_CADASTRE_LAYERS = "1,2,3,4,5,6,7,8,10,11,12,13,14,15"
SK_ZBGIS_WMS = "https://zbgisws.skgeodesy.sk/zbgis_wms_featureinfo/service.svc/get"
SK_ZBGIS_LAYERS = ",".join(
    str(n)
    for n in (
        139, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 159, 161, 163,
        *range(2, 99),
        101, 102, 103, 104, 106, 107, 108, 109, 111, 113, 114, 115, 116, 118, 119, 120, 121, 122, 123, 125, 126, 127,
        128, 129, 130, 132, 133, 134, 135, 136, 137,
    )
)
