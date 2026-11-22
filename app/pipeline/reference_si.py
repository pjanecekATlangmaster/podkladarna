"""Služby pro Slovinsko (zahraniční build): klasifikované mračno LiDAR ARSO (GKOT, 2011–2015) po listech 1 km.

Bez klíče: ``gis.arso.gov.si/lidar/gkot/laz/<blok>/D96TM/TM_<E km>_<N km>.laz`` (D96/TM, EPSG:3794, výšky SVS2000).
List má ~50 MB a ~12 bodů/m² (země ~4,7/m², třídy 3–5 vegetace, 6 budovy, 7 šum). Blok (``b_35`` …) v adrese
nejde z čísla listu spočítat, zjistí se dotazem HEAD a uloží do cache. Licence: právní upozornění ARSO – opakované
užití veřejných informací ze stránek ARSO, i komerčně, s uvedením zdroje (ověřeno 2026-11-20, kazalci.arso.gov.si
„Legal note“; konkrétní licence u dat LiDAR uvedená není). Od 4.16.0 jen záloha za CLSS.

CLSS 2023–2025 (Ciklično lasersko skeniranje Slovenije, GURS, clss.si): celé Slovinsko po listech 1 km, DMR (terén)
a DMP (povrch z prvního odrazu) jako LAZ v mřížce 0,5 m (~4 + 5 MB na list), D96-17/TM, výšky SVS2010. Index listů
je veřejná WFS vrstva prohlížeče (``tileindex_clss_zdruzeno``: list → blok a cesty souborů), soubory leží staticky
na ``assets.flycom.si``. List na hranici bloků bývá ve dvou blocích (každý pokrývá část) – berou se všechny.
Licence GURS CC BY 4.0 i komerčně, citace „Geodetska uprava Republike Slovenije, druh dat a datum“ (e-prostor.gov.si
„Pogoji uporabe“, ověřeno 2026-11-22). Limit 20 stažení denně z návodu prohlížeče se na nás podle bývalého
zaměstnance GURS nevztahuje (Petr 2026-11-22: stahovat bez omezení, jinak je výsledek nahodilý).
Stav služby hlídá ``app.source_status``.
"""

from __future__ import annotations

SI_CRS = "EPSG:3794"  # Slovenia 1996 / Slovene National Grid (D96/TM)
SI_GKOT_BASE = "https://gis.arso.gov.si/lidar/gkot/laz"
# Bloky snímkování; list leží v jednom z nich (ověřeno b_23, b_24, b_26, b_33, b_35, b_37).
SI_BLOCKS = tuple(f"b_{r}{c}" for r in range(1, 6) for c in range(1, 10))
# Sonda: HEAD listu u Ljubljany (blok b_35).
SI_ELEVATION_PROBE = f"{SI_GKOT_BASE}/b_35/D96TM/TM_462_101.laz"


# CLSS 2023–2025 (GURS): index listů (WFS GeoServeru prohlížeče) a statické soubory DMR / DMP.
SI_CLSS_WFS = "https://lift.clss.si/geoserver/fmp_clss/wfs"
SI_CLSS_LAYER = "fmp_clss:tileindex_clss_zdruzeno"
SI_CLSS_ASSETS = "https://assets.flycom.si/"
# Sonda: HEAD DMR listu v Ljubljani (~4 MB se nestahuje).
SI_CLSS_PROBE = f"{SI_CLSS_ASSETS}clss/raw/05-ljubljana/zls/dmr/DMR_473_88.laz"


def clss_index_url(window: tuple[float, float, float, float]) -> str:
    """WFS GetFeature listů CLSS, které zasahují do okna (D96/TM), jen potřebné atributy jako GeoJSON."""
    import urllib.parse

    xmin, ymin, xmax, ymax = window
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": SI_CLSS_LAYER,
        "outputFormat": "application/json",
        "propertyName": "ti_name,orig_ime,path_dmr,path_dmp,datum_za_1",
        "srsName": SI_CRS,
        "bbox": f"{xmin:.0f},{ymin:.0f},{xmax:.0f},{ymax:.0f},{SI_CRS}",
    }
    return f"{SI_CLSS_WFS}?{urllib.parse.urlencode(params)}"


def clss_file_url(path: str) -> str:
    """Úplná adresa souboru z atributu ``path_dmr`` / ``path_dmp`` indexu (``clss/raw/<blok>/zls/…``)."""
    path = str(path or "").lstrip("/")
    if not path.startswith("clss/raw/") or ".." in path:
        raise ValueError(f"neočekávaná cesta CLSS: {path!r}")
    return SI_CLSS_ASSETS + path


def gkot_url(block: str, east_km: int, north_km: int) -> str:
    return f"{SI_GKOT_BASE}/{block}/D96TM/TM_{east_km}_{north_km}.laz"
