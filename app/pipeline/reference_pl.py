"""Služby pro Polsko (zahraniční build): výšky NMT a NMPT, ortofoto a topografická mapa GUGiK.

Vše bez klíče. Data GUGiK jsou od 2020 bezplatná k libovolnému využití (art. 40a ust. 2 Prawo geodezyjne i kartograficzne,
podmínky GUGiK: uvést zdroj „Główny Urząd Geodezji i Kartografii“ a že jde o zpracovaná data; ``configs/countries.yaml``).
Souřadnice EPSG:2180 (PL-1992; v EPSG osy sever–východ → WMS 1.3.0 chce BBOX N, E, ``NORTHING_FIRST_CRS``).

- NMT (terén) 1 m: WCS ``NMT/GRID1/WCS/DigitalTerrainModelFormatTIFF``, coverage ``DTM_PL-KRON86-NH_TIFF`` vrací
  Float32 GeoTIFF (2 × 2 km ~16 MB za ~30 s). Výšky Kronštadt 86 (od EVRF2007 se liší o ~0,2 m, na mapu nic).
- NMPT (povrch): GeoTIFF služba vrací jen obarvený obrázek (3 × Byte), TIFF varianta služby není. Výšky jen jako
  ARC/INFO ASCII (``image/x-aaigrid``, multipart) – ~90 s a ~20 MB na km² po 1 m (``SCALEFACTOR=0.5``); souběžné dotazy
  server odmítá (prázdná odpověď), proto po jedné dlaždici 1 km. Aktuálnost listů 2022–2025.
- Mračno LAZ (list 1 : 1250 ~155 MB) se nepoužívá.

Stav hlídá ``app.source_status``.
"""

from __future__ import annotations

PL_CRS = "EPSG:2180"
PL_GUGIK = "https://mapy.geoportal.gov.pl/wss/service/PZGIK"
PL_DTM_WCS = f"{PL_GUGIK}/NMT/GRID1/WCS/DigitalTerrainModelFormatTIFF"
PL_DTM_COVERAGE = "DTM_PL-KRON86-NH_TIFF"
PL_DSM_WCS = f"{PL_GUGIK}/NMPT/GRID1/WCS/DigitalSurfaceModel"
PL_DSM_COVERAGE = "DSM_PL-KRON86-NH"  # stejný výškový systém jako NMT
PL_ORTHO_WMS = f"{PL_GUGIK}/ORTO/WMS/StandardResolution"  # HighResolution 30–60 s na snímek, občas 400
PL_ORTHO_LAYER = "Raster"
PL_TOPO_WMS = "https://mapy.geoportal.gov.pl/wss/service/img/guest/TOPO/MapServer/WMSServer"
PL_TOPO_LAYER = "Raster"
# Katastr (KIEG integracja.gugik.gov.pl) je jen rozcestník na servery okresů (Varšava vrací reset spojení) – nepoužito.


def pl_dtm_url(box: tuple[float, float, float, float]) -> str:
    """GetCoverage NMT (Float32 GeoTIFF, 1 m) jedné dlaždice; ``x`` je ve službě východ, ``y`` sever."""
    x0, y0, x1, y1 = box
    return (
        f"{PL_DTM_WCS}?SERVICE=WCS&REQUEST=GetCoverage&VERSION=2.0.1&COVERAGEID={PL_DTM_COVERAGE}&FORMAT=image/tiff"
        f"&SUBSET=x({x0:.0f},{x1:.0f})&SUBSET=y({y0:.0f},{y1:.0f})"
    )


def pl_dsm_url(box: tuple[float, float, float, float]) -> str:
    """GetCoverage NMPT jako ARC/INFO ASCII po 1 m (0,5 m se ``SCALEFACTOR=0.5``)."""
    x0, y0, x1, y1 = box
    return (
        f"{PL_DSM_WCS}?SERVICE=WCS&REQUEST=GetCoverage&VERSION=2.0.1&COVERAGEID={PL_DSM_COVERAGE}"
        f"&FORMAT=image/x-aaigrid&SUBSET=x({x0:.0f},{x1:.0f})&SUBSET=y({y0:.0f},{y1:.0f})&SCALEFACTOR=0.5"
    )


# Sondy: malý výřez NMT (Varšava), malý výřez ortofota (WMS 1.1.1 = pořadí E, N).
PL_ELEVATION_PROBE = pl_dtm_url((638000, 486000, 638016, 486016))
PL_ORTHO_PROBE = (
    f"{PL_ORTHO_WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS={PL_ORTHO_LAYER}&STYLES=&SRS=EPSG:2180"
    "&BBOX=638000,486000,638016,486016&WIDTH=16&HEIGHT=16&FORMAT=image/jpeg"
)
