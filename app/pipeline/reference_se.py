"""Služby pro Švédsko (zahraniční build): výšky Lantmäteriet ze STAC ``stac-hojd`` (terén COG, povrch z mračna COPC).

Terén: Markhöjdmodell 1 m (kolekce ``dtm-cog``, CC BY 4.0), COG po listech 10 × 10 km. Povrch: Laserdata Nedladdning,
skog (``dsm-skoglig-copc``, CC BY 4.0 podle PDF podmínek z 20. 5. 2026), mračno COPC po listech 2,5 × 2,5 km, ~1 bod/m².
Obojí v SWEREF99 TM + RH 2000 (EPSG:5845 = 3006 + výšky RH 2000, nadmořské, nic se nepřevádí). Ythöjdmodell
(``dsm-cog``, obrazová korelace, jiné podmínky) se nepoužívá.

Katalog STAC je veřejný, soubory na ``dl1.lantmateriet.se`` chtějí Basic auth bezplatného účtu Geotorget. Jméno a heslo
se čtou jen z proměnných prostředí ``SE_LANTMATERIET_USER`` / ``SE_LANTMATERIET_PASSWORD`` a jdou jen do hlavičky
dotazu (``auth_headers``) nebo do dočasného souboru hlaviček GDAL (``gdal_header_file``), nikdy do URL, logu, chyb,
cache, šablon ani ZIPu. Bez nich se země nenabízí (``countries.elevation_ready``).

GDPR: podle podmínek je příjemce mračna správcem osobních údajů v něm – mračno se jen zpracuje v paměti na mřížku
povrchu a neukládá ani nepřibaluje.
"""

from __future__ import annotations

import base64
import contextlib
import os
import tempfile
import urllib.parse
from pathlib import Path

SE_CRS = "EPSG:3006"  # SWEREF99 TM (data jsou v EPSG:5845 = SWEREF99 TM + RH 2000)
SE_STAC = "https://api.lantmateriet.se/stac-hojd/v1"
SE_DATA_HOST = "dl1.lantmateriet.se"
SE_COLLECTIONS = {"dtm": "dtm-cog", "dsm": "dsm-skoglig-copc"}
SE_USER_ENV = "SE_LANTMATERIET_USER"
SE_PASSWORD_ENV = "SE_LANTMATERIET_PASSWORD"
# Sonda: HEAD listu terénu u Stockholmu (soubor se nestahuje; bez účtu 401).
SE_ELEVATION_PROBE = "https://dl1.lantmateriet.se/hojd/data/grid/mhm/65_6/m657_67.tif"


def credentials() -> tuple[str, str] | None:
    user = os.environ.get(SE_USER_ENV, "").strip()
    password = os.environ.get(SE_PASSWORD_ENV, "")
    if not user or not password:
        return None
    return user, password


def is_data_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and parts.netloc == SE_DATA_HOST


def _basic() -> str:
    cred = credentials()
    if cred is None:
        raise RuntimeError(f"chybí účet Lantmäteriet ({SE_USER_ENV}, {SE_PASSWORD_ENV})")
    return "Basic " + base64.b64encode(f"{cred[0]}:{cred[1]}".encode()).decode("ascii")


def auth_headers(url: str) -> dict[str, str]:
    """Hlavička Basic auth jen pro soubory Lantmäteriet (HTTPS na ``SE_DATA_HOST``); jinak nic."""
    if not is_data_url(url) or credentials() is None:
        return {}
    return {"Authorization": _basic()}


@contextlib.contextmanager
def gdal_header_file():
    """Dočasný soubor s hlavičkou Basic auth pro ``GDAL_HTTP_HEADER_FILE`` (/vsicurl); po použití se smaže.

    Heslo tak není v příkazové řádce (``run_cmd`` ji loguje) ani v proměnných prostředí podprocesu."""
    fd, name = tempfile.mkstemp(prefix=".se-auth-", suffix=".txt")
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(f"Authorization: {_basic()}\n")
        os.chmod(path, 0o600)
        yield path
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def redact(text: str) -> str:
    """Text bez hesla a hlavičky (chybové hlášky)."""
    cred = credentials()
    if cred is None:
        return text
    with contextlib.suppress(RuntimeError):
        text = text.replace(_basic(), "***")
    return text.replace(cred[1], "***")
