"""Nový Zéland (beta): výšky LINZ z veřejného bucketu nz-elevation na AWS.

- **Výšky** (Toitū Te Whenua Land Information New Zealand, CC BY 4.0, bez klíče): celostátní mozaiky
  „New Zealand LiDAR 1m DEM“ a „… 1m DSM“ (nálety 2008–2026, nejnovější má přednost – mozaiku skládá LINZ)
  v ``s3://nz-elevation/new-zealand/new-zealand/{dem_1m,dsm_1m}/2193/``. Jeden COG (Float32, LERC, dlaždice 512,
  NoData −9999) na mapový list Topo50 24 × 36 km, jméno listu = řádek (dvě písmena bez I a O) + sloupec (dvě číslice).
  Souřadnice NZTM2000 (EPSG:2193), výšky NZVD2016 – nic se nepřevádí. Čte se jen okno výřezu přes /vsicurl.
  Listy bez náletu v bucketu nejsou (404), seznam listů je v ``collection.json`` (odkazy ``item``) a drží se v cache.
  Hranice země v ``configs/countries/nz.geojson`` je obrys náletů (``capture-area.geojson``), díry uvnitř listu
  hlídá kontrola pokrytí z přehledů COG.

Stav služby hlídá ``app.source_status`` (HEAD ``collection.json``).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

from app import settings

NZ_CRS = "EPSG:2193"
NZ_BUCKET = "https://nz-elevation.s3.ap-southeast-2.amazonaws.com"
NZ_MOSAIC = f"{NZ_BUCKET}/new-zealand/new-zealand"
NZ_KINDS = {"dtm": "dem_1m", "dsm": "dsm_1m"}
NZ_ELEVATION_PROBE = f"{NZ_MOSAIC}/dem_1m/2193/collection.json"
NZ_INDEX_MAX_AGE_DAYS = 30

# Mřížka listů Topo50 v NZTM2000 (ověřeno na listech AS21, BQ31, BX12 z hlaviček COG 2026-11-22).
SHEET_W = 24_000
SHEET_H = 36_000
SHEET_LEFT = 988_000  # levý okraj sloupce 00
SHEET_TOP = 6_810_000  # horní okraj řádku „AA“
SHEET_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_SHEET_RE = re.compile(r"^([A-HJ-NP-Z]{2})(\d{2})$")
_INDEX_LOCK = threading.Lock()


def sheet_bounds(name: str) -> tuple[float, float, float, float] | None:
    """Obdélník listu Topo50 (xmin, ymin, xmax, ymax) v EPSG:2193; None = nečitelné jméno."""
    m = _SHEET_RE.match(name.upper())
    if not m:
        return None
    a, b = m.group(1)
    row = SHEET_LETTERS.index(a) * len(SHEET_LETTERS) + SHEET_LETTERS.index(b)
    left = SHEET_LEFT + int(m.group(2)) * SHEET_W
    top = SHEET_TOP - row * SHEET_H
    return float(left), float(top - SHEET_H), float(left + SHEET_W), float(top)


def sheets_in(window: tuple[float, float, float, float]) -> list[str]:
    """Jména listů Topo50 zasahujících do okna v EPSG:2193 (bez ohledu na to, zda list v bucketu je)."""
    xmin, ymin, xmax, ymax = window
    n = len(SHEET_LETTERS)
    c0 = int((xmin - SHEET_LEFT) // SHEET_W)
    c1 = int((xmax - SHEET_LEFT) // SHEET_W)
    r0 = int((SHEET_TOP - ymax) // SHEET_H)
    r1 = int((SHEET_TOP - ymin) // SHEET_H)
    out = []
    for r in range(max(r0, 0), min(r1, n * n - 1) + 1):
        for c in range(max(c0, 0), min(c1, 99) + 1):
            out.append(f"{SHEET_LETTERS[r // n]}{SHEET_LETTERS[r % n]}{c:02d}")
    return out


def _default_fetch(url: str) -> str:
    import urllib.request

    from app.pipeline.fetch_openzu import USER_AGENT

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read(20_000_000).decode("utf-8")


def nz_index(*, fetch=None, max_age_days: float = NZ_INDEX_MAX_AGE_DAYS) -> dict[str, list[str]]:
    """Listy v bucketu po druhu: ``{"dtm": [jméno…], "dsm": [jméno…]}`` z cache, jinak z ``collection.json``."""
    path = settings.CACHE_DIR / "nz_linz" / "index.json"
    with _INDEX_LOCK:
        try:
            if time.time() - path.stat().st_mtime < max_age_days * 86400:
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        fetch = fetch or _default_fetch
        index: dict[str, list[str]] = {}
        for kind, folder in NZ_KINDS.items():
            coll = json.loads(fetch(f"{NZ_MOSAIC}/{folder}/2193/collection.json"))
            names = {
                link["href"].rsplit("/", 1)[-1].removesuffix(".json")
                for link in coll.get("links", [])
                if link.get("rel") == "item"
            }
            index[kind] = sorted(n for n in names if _SHEET_RE.match(n))
        if not index.get("dtm"):
            raise RuntimeError("Nový Zéland: seznam listů výšek LINZ je prázdný")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(index), encoding="utf-8")
        tmp.replace(path)
        return index


def sheet_urls(kind: str, window: tuple[float, float, float, float], index: dict[str, list[str]]) -> list[str]:
    """Adresy COG listů druhu ``kind`` (``dtm`` / ``dsm``), které zasahují do okna a v bucketu jsou."""
    have = set(index.get(kind, []))
    folder = NZ_KINDS[kind]
    return [f"{NZ_MOSAIC}/{folder}/2193/{name}.tiff" for name in sheets_in(window) if name in have]
