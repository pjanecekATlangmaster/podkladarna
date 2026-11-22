"""Služby pro Portugalsko (zahraniční build): LiDAR DGT 2024 z Centro de Dados (cdd.dgterritorio.gov.pt).

MDT (terén) a MDS (povrch) 0,5 m pro pevninu jako GeoTIFF dlaždice ~1 km v ETRS89 / PT-TM06 (EPSG:3763), výšky Cascais;
licence CC BY 4.0 (uvést „Direção-Geral do Território“, citace v ``configs/countries.yaml``). Katalog STAC
``/dgt-be/v1/collections`` je veřejný, hledání podle výřezu (``POST /dgt-be/v1/search``, CQL2) a stažení
(``/dgt-be/v1/download/<hash>`` → 302 na podepsanou adresu úložiště na portu 9000) jen po přihlášení.
Účet Centro de Dados je bezplatný a zakládá se jen e-mailem (portál PoInT s Cartão de Cidadão na data potřeba není,
ověřeno 2026-11-22). Přihlašovací údaje jen z proměnných prostředí ``PT_DGT_USER`` / ``PT_DGT_PASSWORD``; bez nich
se Portugalsko nenabízí (``countries.ELEVATION_KEY_ENV``). Do logu ani chyb se nedostanou.
Odkaz na stažení platí jen pro jedno stažení po hledání (pak „já foi transferido“), proto se hledá v každém jobu
a dlaždice se drží v cache. MDS zatím chybí na severozápadě (popis kolekce), tam se výška porostu nespočítá.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

PT_CRS = "EPSG:3763"  # ETRS89 / Portugal TM06
PT_CDD = "https://cdd.dgterritorio.gov.pt"
PT_API = f"{PT_CDD}/dgt-be/v1"
PT_LOGIN = f"{PT_CDD}/auth/login"
PT_COLLECTIONS = {"dtm": "MDT-50cm", "dsm": "MDS-50cm"}
PT_USER_ENV = "PT_DGT_USER"
PT_PASSWORD_ENV = "PT_DGT_PASSWORD"
# Sonda: veřejný popis kolekce terénu (bez přihlášení; hledání a stažení chce účet).
PT_ELEVATION_PROBE = f"{PT_API}/collections/{PT_COLLECTIONS['dtm']}"

SEARCH_LIMIT = 100
SEARCH_MAX_PAGES = 20  # 2 000 dlaždic ~1 km² na kolekci: víc je chyba stránkování, ne výřez
_FORM_RE = re.compile(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', re.I)
# MDT-50cm-111194-07-2024: číslo dlaždice, lot, rok nasnímání
_ITEM_RE = re.compile(r"^(?:MDT|MDS)-50cm-(\d+)-(\d+)-(\d{4})$")


class PtAuthError(RuntimeError):
    """Přihlášení do Centro de Dados se nepovedlo (chybí nebo neplatí údaje, změnil se formulář)."""


def credentials() -> tuple[str, str] | None:
    user = os.environ.get(PT_USER_ENV, "").strip()
    password = os.environ.get(PT_PASSWORD_ENV, "")
    return (user, password) if user and password else None


def tile_key(item_id: str) -> str | None:
    """Klíč dlaždice v cache (číslo, lot, rok) společný pro terén i povrch; None u neznámého tvaru id."""
    m = _ITEM_RE.match(item_id)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def search_body(collection: str, bbox_wgs84: tuple[float, float, float, float]) -> dict:
    """Tělo hledání STAC (CQL2 JSON) jako ve webu Centro de Dados: průnik s obdélníkem výřezu a kolekce."""
    w, s, e, n = bbox_wgs84
    poly = {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}
    return {
        "filter-lang": "cql2-json",
        "filter": {"op": "and", "args": [
            {"op": "intersects", "args": [{"property": "geometry"}, poly]},
            {"op": "eq", "args": [{"property": "collection"}, collection]},
        ]},
        "limit": SEARCH_LIMIT,
    }


class CddSession:
    """Přihlášená relace Centro de Dados (cookie ``connect.sid`` po OIDC přihlášení přes Keycloak)."""

    def __init__(self, user_agent: str, timeout: float = 60.0):
        self.user_agent = user_agent
        self.timeout = timeout
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def _open(self, url: str, data: bytes | None = None, headers: dict | None = None, timeout: float | None = None):
        req = urllib.request.Request(url, data=data, headers={"User-Agent": self.user_agent, **(headers or {})})
        return self.opener.open(req, timeout=timeout or self.timeout)

    def login(self) -> None:
        creds = credentials()
        if creds is None:
            raise PtAuthError(f"chybí {PT_USER_ENV} / {PT_PASSWORD_ENV}")
        with self._open(PT_LOGIN) as resp:
            page = resp.read(2_000_000).decode("utf-8", "replace")
        m = _FORM_RE.search(page)
        if not m:
            raise PtAuthError("přihlašovací formulář Centro de Dados nenalezen")
        action = html.unescape(m.group(1))
        if urllib.parse.urlsplit(action).scheme != "https":
            raise PtAuthError("neočekávaná adresa přihlášení")
        form = urllib.parse.urlencode({"username": creds[0], "password": creds[1], "credentialId": ""}).encode()
        with self._open(action, data=form, headers={"Content-Type": "application/x-www-form-urlencoded"}) as resp:
            final = resp.geturl()
            resp.read(2_000_000)
        if not final.startswith(PT_CDD + "/") or not any(c.name == "connect.sid" for c in self.jar):
            raise PtAuthError("Centro de Dados přihlášení odmítlo (neplatné údaje?)")

    def search(self, collection: str, bbox_wgs84: tuple[float, float, float, float]) -> list[dict]:
        """Položky kolekce zasahující do obdélníku (WGS84), všechny stránky."""
        body: dict | None = search_body(collection, bbox_wgs84)
        out: list[dict] = []
        for _page in range(SEARCH_MAX_PAGES):
            with self._open(f"{PT_API}/search", data=json.dumps(body).encode(),
                            headers={"Content-Type": "application/json", "Accept": "application/geo+json"}) as resp:
                final = resp.geturl()
                raw = resp.read(50_000_000)
            if not final.startswith(f"{PT_API}/search"):
                raise PtAuthError("hledání Centro de Dados chce přihlášení")
            data = json.loads(raw)
            data = data.get("data", data)
            out.extend(data.get("features") or [])
            nxt = next((link for link in data.get("links") or [] if link.get("rel") == "next"), None)
            if not nxt or not isinstance(nxt.get("body"), dict) or not data.get("features"):
                return out
            body = nxt["body"]
        raise RuntimeError("Centro de Dados: hledání vrátilo příliš mnoho stránek")

    def open_download(self, href: str, timeout: float = 300.0):
        """Otevře stažení dlaždice (odkaz z hledání → přesměrování na úložiště); cookie se mimo CDD neposílá."""
        if not href.startswith(f"{PT_API}/download/"):
            raise RuntimeError("Centro de Dados: neočekávaný odkaz ke stažení")
        return self._open(href, timeout=timeout)
