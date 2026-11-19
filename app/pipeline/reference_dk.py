"""Služby pro Dánsko (zahraniční build): výšky DHM z Datafordeleru (Klimadatastyrelsen).

DHM/Terræn (terén) a DHM/Overflade (povrch) z lidaru, mřížka 0,4 m, WCS ``dhm_wcs`` v EPSG:25832, výšky DVR90
(nadmořské, nic se nepřevádí). Licence CC BY 4.0 (Klimadatastyrelsen, volná geodata od 16. 5. 2024, i komerčně).

Datafordeler chce API klíč (IT systém v Datafordeler Administration, obnova po dvou letech). Klíč se čte jen
z proměnné prostředí ``DK_DATAFORDELER_API_KEY`` a do URL se dosadí až těsně před dotazem (``with_key``): nikdy
se nezapisuje do logu, chyb, cache, šablon ani ZIPu (proto Dánsko nemá online šablony – WMS s klíčem by klíč
v .omap prozradil). Bez klíče se země nenabízí (``countries.elevation_ready``).
Odpovědi Datafordeleru samy klíč ozvěnou vracejí (GetCapabilities v odkazech), proto se jejich text nikde
neukládá a chybové hlášky prochází ``redact``.
"""

from __future__ import annotations

import os

DK_API_KEY_ENV = "DK_DATAFORDELER_API_KEY"
# Zástupný text v URL; skutečný klíč dosadí ``with_key`` až při dotazu (sonda, stahování).
KEY_PLACEHOLDER = "{dk_apikey}"

DK_CRS = "EPSG:25832"
DK_WCS = "https://wcs.datafordeler.dk/DHMNedboer/dhm_wcs/1.0.0/WCS"
DK_COVERAGES = {"dtm": "dhm_terraen", "dsm": "dhm_overflade"}
# Sonda: okno 8 × 8 m terénu (Dyrehaven u Kodaně).
DK_PROBE_BOX = (723400, 6185400, 723408, 6185408)
DK_ELEVATION_PROBE = (
    f"{DK_WCS}?apikey={KEY_PLACEHOLDER}&SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage&COVERAGE={DK_COVERAGES['dtm']}"
    f"&CRS={DK_CRS}&RESPONSE_CRS={DK_CRS}&BBOX={','.join(map(str, DK_PROBE_BOX))}&WIDTH=8&HEIGHT=8&FORMAT=GTiff"
)


def api_key() -> str:
    return os.environ.get(DK_API_KEY_ENV, "").strip()


def with_key(url: str) -> str:
    """URL se skutečným klíčem (jen pro samotný dotaz; výsledek nikam neukládat ani nelogovat)."""
    if KEY_PLACEHOLDER not in url:
        return url
    key = api_key()
    if not key:
        raise RuntimeError(f"chybí API klíč Datafordeleru ({DK_API_KEY_ENV})")
    return url.replace(KEY_PLACEHOLDER, key)


def redact(text: str) -> str:
    """Text bez klíče (chybové hlášky, odpovědi služby)."""
    key = api_key()
    return text.replace(key, "***") if key else text
