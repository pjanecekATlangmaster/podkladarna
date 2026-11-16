"""Služby pro Severní Porýní-Vestfálsko (zahraniční build, „DE-NW“): výšky, ortofoto, ABK 1 : 5 000 a ALKIS.

Vše od Geobasis NRW (Bezirksregierung Köln) bez klíče, Datenlizenz Deutschland – Zero 2.0 (citace není povinná,
uvádíme ji; ``configs/countries.yaml``). Souřadnice EPSG:25832 (ETRS89 / UTM 32N), osy východ–sever.

- DGM1: terén 1 m z ALS (WCS ``nw_dgm``),
- nDOM50: výška objektů nad terénem z **letního** obrazového DOM (bDOM50, snímky květen–říjen, „belaubt“) minus DGM
  (WCS ``nw_ndom``, 0,5 m; čte se se ``SCALEFACTOR=0.5`` po 1 m),
- DOM1 z ALS (WCS ``nw_dom``) ze **zimních** náletů (listopad–březen), o 1–4 roky starší než bDOM.
  Výška porostu jobu = menší z obou (``elevation_raster.de_nrw_provider``): ALS ukazuje vytěžené kůrovcové smrčiny
  (Egge, Sauerland) jako les, letní snímky vidí obilí a kukuřici jako porost a rozmazávají okraje budov.

WCS vrací nejvýš 2000 × 2000 px (2 × 2 km po 1 m). Mračno (LAZ 1.2, ne COPC, 70–100 MB / km², bez tříd vegetace)
se nepoužívá. Stav hlídá ``app.source_status``.
"""

from __future__ import annotations

DE_NW_CRS = "EPSG:25832"
DE_NW_WCS = "https://www.wcs.nrw.de/geobasis"
DE_NW_COVERAGES = {"dtm": "nw_dgm", "ndom": "nw_ndom", "dsm": "nw_dom"}
DE_NW_SCALE = {"dtm": 1.0, "ndom": 0.5, "dsm": 1.0}  # nDOM má 0,5 m → po 1 m (4 MB / km² místo 16)
DE_NW_WMS = "https://www.wms.nrw.de/geobasis"
DE_NW_DOP_WMS = f"{DE_NW_WMS}/wms_nw_dop"
DE_NW_DOP_LAYER = "nw_dop_rgb"
DE_NW_ABK_WMS = f"{DE_NW_WMS}/wms_nw_abk"
DE_NW_ABK_LAYER = "nw_abk_col"  # Amtliche Basiskarte 1 : 5 000 (barevná)
DE_NW_ALKIS_WMS = f"{DE_NW_WMS}/wms_nw_alkis"
DE_NW_ALKIS_LAYER = "adv_alkis_flurstuecke"  # se STYLES=default vrací chybu (prázdný styl ano)


def de_nw_coverage_url(kind: str, box: tuple[float, float, float, float]) -> str:
    """GetCoverage (WCS 2.0.1) jedné dlaždice v EPSG:25832; pixel 1 m."""
    x0, y0, x1, y1 = box
    name = DE_NW_COVERAGES[kind]
    return (
        f"{DE_NW_WCS}/wcs_{name}?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage"
        f"&COVERAGEID={name}&FORMAT=image/tiff&SUBSET=x({x0:.0f},{x1:.0f})&SUBSET=y({y0:.0f},{y1:.0f})"
        f"&SCALEFACTOR={DE_NW_SCALE[kind]:g}"
    )


# Sondy: malý výřez DGM z WCS (Bielefeld), malý výřez ortofota.
DE_NW_ELEVATION_PROBE = de_nw_coverage_url("dtm", (459000, 5768500, 459016, 5768516))
DE_NW_DOP_PROBE = (
    f"{DE_NW_DOP_WMS}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS={DE_NW_DOP_LAYER}&STYLES="
    "&CRS=EPSG:25832&BBOX=459000,5768500,459016,5768516&WIDTH=16&HEIGHT=16&FORMAT=image/jpeg"
)
