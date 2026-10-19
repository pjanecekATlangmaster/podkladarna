"""Vyhledání balíků DTM kraje (ZPS_OPL / ZPS_KR) pro výřez – bez pevných odkazů.

DMVS (ČÚZK) vydává veřejné datové sady jako ``VS_<kraj>_<OBEC|MOMC>_<kód>_<datum>_<sada>.jvf.zip``
na serverech krajů. Odkazy se mění (nové datum, nové UUID), proto se vždy ptáme
živého rozhraní portálu DMVS (stejné, které používá „Stáhnout seznam odkazů“):

* ``GET  /api/rest/open-data/datove-sady/is-dtmk/info`` – kraje a stav sad,
* ``POST /api/rest/open-data/datove-sady/stavove/seznam-url`` – seznam URL (text/plain).

Území výřezu (obec / městská část) určuje RÚIAN (ArcGIS ČÚZK). Tento modul jen
**vyhledá** balíky; stahování a čtení JVF je samostatný krok.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from app import settings
from app.pipeline.fetch_openzu import USER_AGENT

DMVS_API = "https://dmvs.cuzk.gov.cz/api/rest/open-data/datove-sady"
HTTP_TIMEOUT_S = 90
# Seznam odkazů se mění nejvýš měsíčně (stavová data k poslednímu dni měsíce).
URL_LIST_CACHE_MAX_AGE_H = 24.0
# Stavová data jsou k poslednímu dni měsíce; okno musí pokrýt poslední vydání.
DATE_FROM_WINDOW_DAYS = 100

# RÚIAN vrstvy (ArcGIS ČÚZK): městská část / obvod, obec, kraj.
RUIAN_LAYER_MOMC = 8
RUIAN_LAYER_OBEC = 12
RUIAN_LAYER_VUSC = 17

SET_PB = "ZPS_PB"
SET_OPL = "ZPS_OPL"
SET_KR = "ZPS_KR"
DEFAULT_SETS = (SET_OPL, SET_KR)

_NAME_RX = re.compile(
    r"/(?P<name>V[SZ]_(?P<kraj>CZ\d{3})_(?P<kind>OBEC|MOMC)_(?P<code>\d+)_(?P<date>\d{8})_"
    r"(?P<set>[A-Z][A-Z_]*))\.(?P<fmt>[a-z]+)\.zip(?:\?.*)?$"
)


@dataclass(frozen=True)
class DtmPackage:
    url: str
    kraj: str  # NUTS, např. CZ010
    unit_kind: str  # OBEC | MOMC
    unit_code: int  # RÚIAN kód obce / městské části
    date: str  # YYYYMMDD (k poslednímu dni měsíce)
    set_name: str  # ZPS_OPL, ZPS_KR, ZPS_PB, ZPS_DOP, DI
    fmt: str  # jvf (shp/gpkg jen u vybraných regionálních portálů)

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1].split("?", 1)[0]


@dataclass(frozen=True)
class DtmUnit:
    kind: str  # OBEC | MOMC
    code: int
    name: str
    kraj_name: str = ""


# --------------------------------------------------------------------------- seznam odkazů


def parse_url_list(text: str) -> list[DtmPackage]:
    """Řádky ``https://…/VS_CZ010_MOMC_500143_20260930_ZPS_KR.jvf.zip`` → balíky."""
    out: list[DtmPackage] = []
    for raw in text.splitlines():
        url = raw.strip()
        if not url:
            continue
        m = _NAME_RX.search(url)
        if not m:
            continue
        out.append(
            DtmPackage(
                url=url,
                kraj=m["kraj"],
                unit_kind=m["kind"],
                unit_code=int(m["code"]),
                date=m["date"],
                set_name=m["set"],
                fmt=m["fmt"],
            )
        )
    return out


def _request(url: str, *, data: bytes | None = None, accept: str = "*/*") -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return resp.read()


def fetch_regions() -> list[dict]:
    """Kraje, pro které jsou sady připravené: ``[{"kod": "CZ010", "nazev": …, "stav": …}]``."""
    body = _request(f"{DMVS_API}/is-dtmk/info", accept="application/json")
    return list(json.loads(body.decode("utf-8")).get("items") or [])


def _normalize(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "").casefold()
    return re.sub(r"[^a-z0-9]+", "", "".join(c for c in text if not unicodedata.combining(c)))


def region_code_for_name(kraj_name: str, regions: list[dict]) -> str | None:
    """RÚIAN ``Hlavní město Praha`` / ``Středočeský kraj`` → NUTS kód z přehledu DMVS."""
    want = _normalize(kraj_name)
    for reg in regions:
        if _normalize(str(reg.get("nazev") or "")) == want:
            return str(reg.get("kod"))
    return None


def _list_cache_path(kraj: str) -> Path:
    return settings.DOWNLOADS_DIR / "dtm" / f"urls_{kraj}.txt"


def fetch_url_list(
    kraj: str,
    *,
    date_from: str | None = None,
    max_age_h: float = URL_LIST_CACHE_MAX_AGE_H,
    force: bool = False,
    log=None,
) -> list[DtmPackage]:
    """Aktuální odkazy pro jeden kraj (cache ``max_age_h``; při výpadku starší cache)."""
    cache = _list_cache_path(kraj)
    if not force and cache.is_file() and (time.time() - cache.stat().st_mtime) / 3600.0 <= max_age_h:
        return parse_url_list(cache.read_text(encoding="utf-8"))
    if date_from is None:
        date_from = (date.today() - timedelta(days=DATE_FROM_WINDOW_DAYS)).isoformat()
    payload = json.dumps({"isDtmk": [kraj], "datumOd": date_from}).encode("utf-8")
    try:
        text = _request(
            f"{DMVS_API}/stavove/seznam-url", data=payload, accept="text/plain"
        ).decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if cache.is_file():
            if log:
                log(f"DTM: seznam odkazů ({kraj}) nelze obnovit ({exc}), beru starší cache")
            return parse_url_list(cache.read_text(encoding="utf-8"))
        raise
    packages = parse_url_list(text)
    if packages:  # prázdnou odpověď necachujeme
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
    return packages


# --------------------------------------------------------------------------- území výřezu


def units_for_bbox(bbox_wgs84: tuple[float, float, float, float]) -> list[DtmUnit]:
    """Městské části a obce (RÚIAN), do kterých výřez zasahuje, vč. názvu kraje.

    U statutárních měst s městskými částmi vrací MOMC (DMVS je vydává po nich),
    jinak OBEC. Pořadí: MOMC první.
    """
    from app.pipeline.fetch_ruian import RUIAN_SERVICE
    from app.pipeline.fetch_zabaged import query_layer_geojson

    west, south, east, north = bbox_wgs84

    def _layer(layer_id: int) -> list[dict]:
        gj = query_layer_geojson(RUIAN_SERVICE, layer_id, west, south, east, north)
        return [f.get("properties") or {} for f in (gj.get("features") or [])]

    kraje = [str(p.get("nazev") or "") for p in _layer(RUIAN_LAYER_VUSC)]
    kraj_name = kraje[0] if kraje else ""
    units: list[DtmUnit] = []
    for props in _layer(RUIAN_LAYER_MOMC):
        if props.get("kod") is not None:
            units.append(DtmUnit("MOMC", int(props["kod"]), str(props.get("nazev") or ""), kraj_name))
    for props in _layer(RUIAN_LAYER_OBEC):
        if props.get("kod") is not None:
            units.append(DtmUnit("OBEC", int(props["kod"]), str(props.get("nazev") or ""), kraj_name))
    return units


# --------------------------------------------------------------------------- výběr balíků


def latest_packages(
    packages: list[DtmPackage],
    units: list[DtmUnit],
    *,
    sets: tuple[str, ...] = DEFAULT_SETS,
    fmt: str = "jvf",
) -> list[DtmPackage]:
    """Pro každou jednotku a sadu nejnovější balík. MOMC má přednost před OBEC.

    Obec, jejíž městské části v seznamu jsou (Praha, Brno…), se přeskočí – data jsou
    jen po městských částech. Jednotka bez balíku se vynechá.
    """
    by_key: dict[tuple[str, int, str], DtmPackage] = {}
    for pkg in packages:
        if pkg.fmt != fmt or pkg.set_name not in sets:
            continue
        key = (pkg.unit_kind, pkg.unit_code, pkg.set_name)
        if key not in by_key or pkg.date > by_key[key].date:
            by_key[key] = pkg
    have_momc = any(u.kind == "MOMC" and any(k[0] == "MOMC" and k[1] == u.code for k in by_key) for u in units)
    out: list[DtmPackage] = []
    for unit in units:
        if unit.kind == "OBEC" and have_momc:
            continue
        for set_name in sets:
            pkg = by_key.get((unit.kind, unit.code, set_name))
            if pkg:
                out.append(pkg)
    return out


def find_packages_for_bbox(
    bbox_wgs84: tuple[float, float, float, float],
    *,
    sets: tuple[str, ...] = DEFAULT_SETS,
    fmt: str = "jvf",
    force: bool = False,
    log=None,
) -> list[DtmPackage]:
    """Aktuální balíky DTM (ZPS_OPL + ZPS_KR) pro výřez; prázdné, když data nejsou."""
    units = units_for_bbox(bbox_wgs84)
    if not units:
        return []
    regions = fetch_regions()
    kraj_name = next((u.kraj_name for u in units if u.kraj_name), "")
    kraj = region_code_for_name(kraj_name, regions)
    if not kraj:
        if log:
            log(f"DTM: pro kraj „{kraj_name}“ nejsou datové sady")
        return []
    packages = fetch_url_list(kraj, force=force, log=log)
    return latest_packages(packages, units, sets=sets, fmt=fmt)
