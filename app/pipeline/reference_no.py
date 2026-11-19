"""Služby pro Norsko (zahraniční build): výšky Nasjonal detaljert høydemodell (Kartverket) z WCS Geonorge.

Bez klíče; volné produkty Kartverket jsou CC BY 4.0 (uvést „©Kartverket“, citace v ``configs/countries.yaml``,
ověřeno 2026-11-19 na kartverket.no/en/api-and-data/terms-of-use a v metadatech DOM1 „Åpne data“).
FKB (budovy, silnice) je „Norge digitalt begrenset“ a ortofoto Norge i bilder jen se smlouvou – nepoužívá se,
kresba je z OSM. Stav služeb hlídá ``app.source_status``.
"""

from __future__ import annotations

NO_CRS = "EPSG:25833"  # ETRS89 / UTM 33N, nativní systém celostátního výškového modelu (i pro západ a Finnmark)
NO_WCS = {
    "dtm": "https://wcs.geonorge.no/skwms1/wcs.hoyde-dtm-nhm-25833",
    "dsm": "https://wcs.geonorge.no/skwms1/wcs.hoyde-dom-nhm-25833",
}
NO_COVERAGES = {"dtm": "nhm_dtm_topo_25833", "dsm": "nhm_dom_topo_25833"}
# Sonda: okno 8 × 8 m terénu (Nordmarka u Osla).
NO_ELEVATION_PROBE = (
    f"{NO_WCS['dtm']}?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage&COVERAGE={NO_COVERAGES['dtm']}"
    f"&CRS={NO_CRS}&BBOX=261000,6656000,261008,6656008&WIDTH=8&HEIGHT=8&FORMAT=GeoTIFF"
)
