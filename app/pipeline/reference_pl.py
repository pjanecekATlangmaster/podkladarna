"""Služby pro Polsko (zahraniční build): výšky NMT a NMPT, ortofoto a topografická mapa GUGiK.

Vše bez klíče. Data GUGiK jsou od 2020 bezplatná k libovolnému využití (art. 40a ust. 2 Prawo geodezyjne i kartograficzne,
podmínky GUGiK: uvést zdroj „Główny Urząd Geodezji i Kartografii“ a že jde o zpracovaná data; ``configs/countries.yaml``).
Souřadnice EPSG:2180 (PL-1992; v EPSG osy sever–východ → WMS 1.3.0 chce BBOX N, E, ``NORTHING_FIRST_CRS``).

- NMT (terén) 1 m: WCS ``NMT/GRID1/WCS/DigitalTerrainModelFormatTIFF``, coverage ``DTM_PL-KRON86-NH_TIFF`` vrací
  Float32 GeoTIFF (2 × 2 km ~16 MB za ~30 s). Výšky Kronštadt 86 (od EVRF2007 se liší o ~0,2 m, na mapu nic).
- NMPT (povrch): GeoTIFF služba vrací jen obarvený obrázek (3 × Byte), TIFF varianta služby není. Výšky jen jako
  ARC/INFO ASCII (``image/x-aaigrid``, multipart) – ~90 s a ~20 MB na km² po 1 m (``SCALEFACTOR=0.5``); souběžné dotazy
  server odmítá (prázdná odpověď), proto po jedné dlaždici 1 km. Aktuálnost listů 2022–2025.
- Záloha při výpadku WCS (2026-11-23: GetCoverage NMT i NMPT končí po ~10 s prázdnou odpovědí, GetCapabilities a WMS
  běží, z cloudu i z domácí sítě stejně – výpadek, ne blokace): celé listy 1 : 5000 (~2,3 × 2,3 km) ARC/INFO ASCII
  z ``opendata.geoportal.gov.pl/NumDaneWys``, odkaz na list vrací WFS skorowidzů (vrstva za rok, atribut
  ``url_do_pobrania``). Výšky EVRF2007, NMT 1 m (~37 MB list), NMPT od 2025 po 0,5 m (~150 MB list). Městské listy
  v PL-2000 (godło ``7.125.11.12``) se neberou, jen listy PL-1992 (godło ``M-34-64-D-d-1-4``).
- Mračno LAZ (list 1 : 1250 ~155 MB) se nepoužívá.

Stav hlídá ``app.source_status``.
"""

from __future__ import annotations

import re

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


# Záloha: skorowidze (indexy listů) přes WFS 2.0 – osy EPSG:2180 v BBOX sever, východ; vrstvy po letech.
PL_SHEET_WFS = {
    "nmt": f"{PL_GUGIK}/NumerycznyModelTerenuEVRF2007/WFS/Skorowidze",
    "nmpt": f"{PL_GUGIK}/NumerycznyModelPokryciaTerenuEVRF2007/WFS/Skorowidze",
}
PL_SHEET_LAYER = {"nmt": "gugik:SkorowidzNMT{year}", "nmpt": "gugik:SkorowidzNMPT{year}"}
PL_SHEET_YEARS = {"nmt": tuple(range(2026, 2017, -1)), "nmpt": tuple(range(2026, 2018, -1))}  # NMPT od 2019
PL_SHEET_HOST = "https://opendata.geoportal.gov.pl/"
_PL1992_GODLO = re.compile(r"^[MN]-\d{2}-")


def pl_sheet_index_url(kind: str, year: int, box: tuple[float, float, float, float]) -> str:
    x0, y0, x1, y1 = box
    return (
        f"{PL_SHEET_WFS[kind]}?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature&TYPENAMES={PL_SHEET_LAYER[kind].format(year=year)}"
        f"&COUNT=500&BBOX={y0:.0f},{x0:.0f},{y1:.0f},{x1:.0f},urn:ogc:def:crs:EPSG::2180"
    )


def parse_sheet_index(xml: bytes, year: int) -> list[dict]:
    """Listy z odpovědi WFS: godło, rok, datum, odkaz, laser; jen ASCII listy PL-1992 na opendata GUGiK."""
    import xml.etree.ElementTree as ET

    out: list[dict] = []
    root = ET.fromstring(xml)
    for member in root:
        if not member.tag.endswith("member"):
            continue
        for feat in member:
            attrs = {el.tag.rsplit("}", 1)[-1]: (el.text or "").strip() for el in feat}
            url, godlo = attrs.get("url_do_pobrania", ""), attrs.get("godlo", "")
            if not (url.startswith(PL_SHEET_HOST) and url.lower().endswith(".asc") and _PL1992_GODLO.match(godlo)):
                continue
            out.append({
                "godlo": godlo, "year": year, "date": attrs.get("akt_data", ""), "url": url,
                "laser": "laser" in attrs.get("zrodlo_danych", "").lower(),
            })
    return out


def choose_sheets(records: list[dict]) -> list[dict]:
    """Na každé godło jeden list: laser před snímky, pak nejnovější."""
    best: dict[str, dict] = {}
    for rec in records:
        key = (rec["laser"], rec["year"], rec["date"])
        cur = best.get(rec["godlo"])
        if cur is None or key > (cur["laser"], cur["year"], cur["date"]):
            best[rec["godlo"]] = rec
    return sorted(best.values(), key=lambda r: r["godlo"])


# Sondy: malý výřez NMT (Varšava), malý výřez ortofota (WMS 1.1.1 = pořadí E, N).
PL_ELEVATION_PROBE = pl_dtm_url((638000, 486000, 638016, 486016))
# Druhá adresa sondy: záložní listy (index NMT); zdroj běží, když odpoví aspoň jedna.
PL_SHEET_PROBE = pl_sheet_index_url("nmt", 2025, (638000, 486000, 638016, 486016))
PL_ORTHO_PROBE = (
    f"{PL_ORTHO_WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS={PL_ORTHO_LAYER}&STYLES=&SRS=EPSG:2180"
    "&BBOX=638000,486000,638016,486016&WIDTH=16&HEIGHT=16&FORMAT=image/jpeg"
)
