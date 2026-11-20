"""Služby pro Slovinsko (zahraniční build): klasifikované mračno LiDAR ARSO (GKOT, 2011–2015) po listech 1 km.

Bez klíče: ``gis.arso.gov.si/lidar/gkot/laz/<blok>/D96TM/TM_<E km>_<N km>.laz`` (D96/TM, EPSG:3794, výšky SVS2000).
List má ~50 MB a ~12 bodů/m² (země ~4,7/m², třídy 3–5 vegetace, 6 budovy, 7 šum). Blok (``b_35`` …) v adrese
nejde z čísla listu spočítat, zjistí se dotazem HEAD a uloží do cache. Licence: právní upozornění ARSO – opakované
užití veřejných informací ze stránek ARSO, i komerčně, s uvedením zdroje (ověřeno 2026-11-20, kazalci.arso.gov.si
„Legal note“; konkrétní licence u dat LiDAR uvedená není). Novější CLSS 2023–2025 (GURS, clss.si) má stahování
za ověřovací úlohou bez API – nepoužívá se. Stav služby hlídá ``app.source_status``.
"""

from __future__ import annotations

SI_CRS = "EPSG:3794"  # Slovenia 1996 / Slovene National Grid (D96/TM)
SI_GKOT_BASE = "https://gis.arso.gov.si/lidar/gkot/laz"
# Bloky snímkování; list leží v jednom z nich (ověřeno b_23, b_24, b_26, b_33, b_35, b_37).
SI_BLOCKS = tuple(f"b_{r}{c}" for r in range(1, 6) for c in range(1, 10))
# Sonda: HEAD listu u Ljubljany (blok b_35).
SI_ELEVATION_PROBE = f"{SI_GKOT_BASE}/b_35/D96TM/TM_462_101.laz"


def gkot_url(block: str, east_km: int, north_km: int) -> str:
    return f"{SI_GKOT_BASE}/{block}/D96TM/TM_{east_km}_{north_km}.laz"
