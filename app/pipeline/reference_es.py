"""Služby pro španělské regiony (zahraniční build): klasifikované mračno LiDAR regionálních vlád po listech.

- **Navarra** (Gobierno de Navarra, CC BY 4.0 – „Términos de uso“ na filescartografia.navarra.es): let 2024, sensor
  Optech Galaxy T2000, ~5 bodů/m² (země ~1,7/m²), LAZ 1.4 bodový formát 8 po listech 1 × 1 km (+25 m přesah, ~80 MB):
  ``5_LIDAR/5_6_2024_NAV_cam_EPSG25830/las_cam_<E km>-<N km>_2024_NAV_EPSG25830.laz``, kde E/N je **levý horní** roh.
  EPSG:25830, výšky **elipsoidické** (ověřeno 2026-11-20 proti MDT 2 m: rozdíl 49,4–49,9 m ≈ EGM08-REDNAP 50,0 m)
  → odečte se geoid IGN z PROJ-data. Třídy ASPRS: 2 země, 3–5 vegetace, 6 budovy, 9 voda, 11 vozovka, 17 mosty,
  7 šum; vlastní 64+ se neberou. Seznam listů z výpisu adresáře (HTTPS, Range podporuje).

- Ortofoto PNOA (IGN, CC BY 4.0 „scne.es“, bez klíče): WMS ``pnoa-ma`` v EPSG:25830 pro celé Španělsko.

Stav služby hlídá ``app.source_status``.
"""

from __future__ import annotations

from dataclasses import dataclass

ES_CRS = "EPSG:25830"  # ETRS89 / UTM zone 30N
# Geoid EGM08-REDNAP (IGN, PROJ-data, CC BY 4.0), elipsoidické výšky ETRS89 → nadmořské (Alicante), ~0,8 MB.
ES_GEOID_URL = "https://cdn.proj.org/es_ign_egm08-rednap.tif"


@dataclass(frozen=True)
class EsLazRegion:
    key: str  # provider výšek a složka cache
    label: str  # do logu
    listing: str  # adresář s listy (HTML výpis)
    name_re: str  # regulární výraz jména listu ve výpisu se skupinami E a N (km)
    url: str  # šablona URL listu s {e} a {n}
    tile_m: int  # strana listu v metrech
    top_corner: bool  # N ve jménu je horní okraj listu (jinak dolní)
    ground: tuple[int, ...]  # třídy terénu
    surface: tuple[int, ...]  # třídy povrchu (maximum v buňce)
    ellipsoidal: bool  # výšky elipsoidické → odečíst geoid
    probe: str  # malý list pro HEAD sondu


NAV_BASE = "https://filescartografia.navarra.es/5_LIDAR/5_6_2024_NAV_cam_EPSG25830"

ES_LAZ_REGIONS: dict[str, EsLazRegion] = {
    "es_nav": EsLazRegion(
        key="es_nav",
        label="Navarra LiDAR 2024",
        listing=f"{NAV_BASE}/",
        name_re=r"las_cam_(\d{3})-(\d{4})_2024_NAV_EPSG25830\.laz",
        url=NAV_BASE + "/las_cam_{e}-{n}_2024_NAV_EPSG25830.laz",
        tile_m=1000,
        top_corner=True,
        ground=(2, 11),
        surface=(2, 3, 4, 5, 6, 9, 11, 17),
        ellipsoidal=True,
        probe=f"{NAV_BASE}/las_cam_610-4741_2024_NAV_EPSG25830.laz",
    ),
}

NAV_ELEVATION_PROBE = ES_LAZ_REGIONS["es_nav"].probe

# Ortofoto PNOA máxima actualidad (IGN, CC BY 4.0 scne.es) – šablona a maska zpevněných ploch sprintu.
ES_PNOA_WMS = "https://www.ign.es/wms-inspire/pnoa-ma"
ES_PNOA_LAYER = "OI.OrthoimageCoverage"
ES_ORTHO_PROBE = (
    f"{ES_PNOA_WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS={ES_PNOA_LAYER}&STYLES=&SRS=EPSG:25830"
    "&BBOX=610000,4740000,610016,4740016&WIDTH=16&HEIGHT=16&FORMAT=image/jpeg"
)
