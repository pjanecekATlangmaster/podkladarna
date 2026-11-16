"""Služby pro Bavorsko (zahraniční build): výšky DGM1 a DOM20, ortofoto DOP40, ALKIS Parzellarkarte a DOK 1 : 10 000.

Vše od LDBV (Landesamt für Digitalisierung, Breitband und Vermessung) jako OpenData, CC BY 4.0, bez klíče
(uvést „Bayerische Vermessungsverwaltung – www.geodaten.bayern.de“; ``configs/countries.yaml``). Souřadnice EPSG:25832.

- DGM1 (terén z ALS) 1 m: dlaždice 1 km GeoTIFF ``{e}_{n}.tif`` (~2–4 MB), čte se okno přes /vsicurl.
- DOM20 (povrch ze **snímků** Bayernbefliegung, 20 cm, Float32 LZW bloky 256 px, ~45 MB na km²): letní nálet
  (metadata ``BILDFLUG_DATUM``, např. 24. 8. 2024, 7. 9. 2025) → listnáče jsou v něm, obilí a kukuřice také.
  Čte se jen okno výřezu a hned se převzorkuje na 1 m (maximum).
- Mračno ``laser`` (LAZ po 1 km, až 1 GB) se nepoužívá.

Stav hlídá ``app.source_status``.
"""

from __future__ import annotations

BY_CRS = "EPSG:25832"
BY_DOWNLOAD = "https://download1.bayernwolke.de/a"
BY_DGM1_URL = BY_DOWNLOAD + "/dgm/dgm1/{e}_{n}.tif"
BY_DOM20_URL = BY_DOWNLOAD + "/dom20/DOM/32{e}_{n}_20_DOM.tif"
BY_WMS = "https://geoservices.bayern.de/od/wms"
BY_DOP_WMS = f"{BY_WMS}/dop/v1/dop40"
BY_DOP_LAYER = "by_dop40c"
BY_ALKIS_WMS = f"{BY_WMS}/alkis/v1/parzellarkarte"
BY_ALKIS_LAYER = "by_alkis_parzellarkarte_farbe"
BY_DOK_WMS = f"{BY_WMS}/dtk/v1/dok"
BY_DOK_LAYER = "by_dok"

# Sondy: HEAD na dlaždici DGM1 (Mnichov) a malý výřez ortofota (WMS 1.1.1 = pořadí E, N).
BY_ELEVATION_PROBE = BY_DGM1_URL.format(e=691, n=5334)
BY_ORTHO_PROBE = (
    f"{BY_DOP_WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS={BY_DOP_LAYER}&STYLES=&SRS=EPSG:25832"
    "&BBOX=691000,5334000,691016,5334016&WIDTH=16&HEIGHT=16&FORMAT=image/jpeg"
)
