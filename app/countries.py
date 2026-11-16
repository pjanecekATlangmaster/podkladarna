"""Podporované země a povolené území výřezu (kontrola na serveru i podklad pro mapu v GUI).

Konfigurace ``configs/countries.yaml`` + polygony hranic ``configs/countries/<kód>.geojson``.
Povolené území požadavku = země domény (``SITES``, ``*`` = všechny) ∩ země se ``status: live``; bez ``SITES`` jen CZ.
Země se ``status: beta`` (zahraniční build) přidá ``COUNTRIES_BETA`` (seznam kódů nebo ``*``; na testu B výchozí ``*``),
ale jen doméně, která tu zemi nabízí (``*`` nebo kód v ``SITES``): česká doména (``…:free:CZ``) ani požadavek
bez domény ze ``SITES`` zahraničí nikdy nedostane (Petr 2026-11-15: zahraniční mapy jen na mezinárodní doméně).
Na ostré se beta bez proměnné nenabízí.

Výřez patří zemi, ve které leží aspoň ``MIN_SHARE`` jeho plochy (Petr 2026-11-14, karta „většina v zemi, zbytek
z OSM“). Přesah za hranici projde, ale kreslí se jen z OSM (LiDAR, ZABAGED ani DTM tam nejsou) a web i log
na to upozorní. Podíl se počítá v rovině lon × cos(φ) × lat – na výřez do desítek km stačí.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app import i18n

CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"
COUNTRIES_FILE = CONFIG_DIR / "countries.yaml"
DEFAULT_COUNTRY = "CZ"
# Aspoň polovina výřezu ve vybrané zemi; od FULL_SHARE výše se přesah neohlašuje (tolerance polygonu).
MIN_SHARE = 0.5
FULL_SHARE = 0.99


class CountryError(ValueError):
    """Výřez nebo země mimo povolené území; ``args[0]`` je ``i18n.Msg`` pro uživatele."""


class CountryName(i18n.Msg):
    """Název země z ``countries.yaml`` jako parametr hlášky (přeloží se s ní)."""

    def __new__(cls, names: dict[str, str], code: str) -> "CountryName":
        obj = str.__new__(cls, names.get(i18n.SOURCE_LANG) or names.get("en") or code)
        obj.key = ""
        obj.params = {}
        obj.names = names
        obj.code = code
        return obj

    def render(self, lang: str) -> str:
        return self.names.get(lang) or self.names.get("en") or self.code


class CountryNameList(i18n.Msg):
    """Výčet názvů zemí jako parametr hlášky; každý název se přeloží do jazyka hlášky."""

    def __new__(cls, names: list[CountryName], sep: str = ", ") -> "CountryNameList":
        obj = str.__new__(cls, sep.join(str(n) for n in names))
        obj.key = ""
        obj.params = {}
        obj.names = list(names)
        obj.sep = sep
        return obj

    def render(self, lang: str) -> str:
        return self.sep.join(n.render(lang) for n in self.names)


@dataclass(frozen=True)
class Country:
    code: str
    names: dict[str, str]
    status: str
    crs: str
    rings: tuple[tuple[tuple[float, float], ...], ...]  # vnější obrysy (lon, lat), bez děr
    view: tuple[float, float, float, float]  # jih, západ, sever, východ
    elevation: str = "cz_openzu"  # zdroj výšek (``app.pipeline.country_profile``)
    cz_sources: bool = True  # ZABAGED, RÚIAN, DTM, katastr, AOPK, ČÚZK ortofoto a stínování
    # Obdoba české DTM (velkoměřítková technická mapa) pro kresbu: ``cz_dtm`` | ``nl_bgt`` | ``ch_av`` | ``at_fmzk`` | ``none``.
    technical_map: str = "cz_dtm"
    # Citace zdrojů, které vyžadují licence místních dat (README, CO_JE_PODKLADARNA, poznámka .omap).
    attribution: tuple[str, ...] = ()
    citation: str = ""
    # Části země bez technické mapy (např. kantony CH bez volné AV): kreslí se tam jen z OSM; mapa v GUI je odliší.
    technical_map_gaps: tuple[tuple[tuple[float, float], ...], ...] = ()
    # Naopak jediné území s technickou mapou (FR: Plan de voirie jen v Paříži); prázdné = celá země mimo ``gaps``.
    technical_map_area: tuple[tuple[tuple[float, float], ...], ...] = ()
    # Zeleň v lese: ``points`` hustota bodů z mračna (Česko), ``laser`` zrnitost z laserového povrchu (hranice
    # porostů jako z mračna, ``veg_texture``), ``photo`` povrch ze snímků – jen základní zeleň z výšky porostu.
    green: str = "laser"
    # Stáří a vlastnosti výškových dat pro upozornění před buildem (``app.data_notes``): roky snímkování
    # (text, např. „2013–2015“) a klíče důsledků ``old`` | ``coarse`` (``photo`` plyne z ``green``).
    elevation_years: str = ""
    data_notes: tuple[str, ...] = ()

    def name(self, lang: str = "cs") -> str:
        return self.names.get(lang) or self.names.get("en") or self.code

    def msg_name(self) -> CountryName:
        return CountryName(self.names, self.code)

    @property
    def envelope(self) -> tuple[float, float, float, float]:
        """west, south, east, north obrysu."""
        xs = [x for ring in self.rings for x, _ in ring]
        ys = [y for ring in self.rings for _, y in ring]
        return min(xs), min(ys), max(xs), max(ys)

    def geojson(self) -> dict[str, Any]:
        return _rings_geojson(self.rings)

    @property
    def has_technical_map(self) -> bool:
        return self.technical_map not in ("", "none")

    def technical_map_covers(self, bbox) -> bool:
        """Zasahuje výřez do území technické mapy? Bez ``technical_map_area`` (celá země) vždy ano."""
        if not self.technical_map_area or not bbox:
            return True
        return rings_share(tuple(float(v) for v in bbox), self.technical_map_area) > 0.0


def _rings_geojson(rings) -> dict[str, Any]:
    polys = [[[list(p) for p in ring]] for ring in rings]
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


def _rings_from_geojson(path: Path) -> tuple[tuple[tuple[float, float], ...], ...]:
    data = json.loads(path.read_text(encoding="utf-8"))
    geoms = [f.get("geometry") or {} for f in data.get("features") or []] if "features" in data else [data]
    rings: list[tuple[tuple[float, float], ...]] = []
    for g in geoms:
        if g.get("type") == "Polygon":
            polys = [g["coordinates"]]
        elif g.get("type") == "MultiPolygon":
            polys = g["coordinates"]
        else:
            continue
        for poly in polys:
            if poly:
                rings.append(tuple((float(x), float(y)) for x, y, *_ in poly[0]))
    return tuple(rings)


@lru_cache(maxsize=1)
def load_countries() -> dict[str, Country]:
    raw = yaml.safe_load(COUNTRIES_FILE.read_text(encoding="utf-8")) or {}
    out: dict[str, Country] = {}
    for code, spec in raw.items():
        code = str(code).upper()
        rings = _rings_from_geojson(CONFIG_DIR / spec["boundary"])
        if not rings:
            continue
        view = tuple(float(v) for v in spec.get("view") or ())
        country = Country(
            code=code,
            names={str(k): str(v) for k, v in (spec.get("name") or {}).items()},
            status=str(spec.get("status") or "off"),
            crs=str(spec.get("crs") or ""),
            rings=rings,
            view=view if len(view) == 4 else (0.0, 0.0, 0.0, 0.0),
            elevation=str(spec.get("elevation") or ("cz_openzu" if code == DEFAULT_COUNTRY else "none")),
            cz_sources=bool(spec.get("cz_sources", code == DEFAULT_COUNTRY)),
            technical_map=str(spec.get("technical_map") or ("cz_dtm" if code == DEFAULT_COUNTRY else "none")),
            attribution=tuple(str(line) for line in (spec.get("attribution") or [])),
            citation=str(spec.get("citation") or ""),
            technical_map_gaps=(
                _rings_from_geojson(CONFIG_DIR / spec["technical_map_gaps"]) if spec.get("technical_map_gaps") else ()
            ),
            technical_map_area=(
                _rings_from_geojson(CONFIG_DIR / spec["technical_map_area"]) if spec.get("technical_map_area") else ()
            ),
            green=str(spec.get("green") or ("points" if code == DEFAULT_COUNTRY else "laser")),
            elevation_years=str(spec.get("elevation_years") or ""),
            data_notes=tuple(str(k) for k in (spec.get("data_notes") or [])),
        )
        if len(view) != 4:
            w, s, e, n = country.envelope
            country = replace(country, view=(s, w, n, e))
        out[code] = country
    return out


# Zdroje výšek z ručně stažených celostátních dat na disku (``DATA_ROOT/<složka>/dtm`` a ``…/dsm``, GeoTIFF).
# Země s takovým zdrojem se nabízí, jen když data na instanci jsou (Slovensko: jen ostrá A, Petr 2026-11-16).
LOCAL_ELEVATION_DIRS = {"sk_ugkk_local": "cache/static/sk_ugkk"}


def local_elevation_dir(elevation: str) -> Path | None:
    """Složka dat zdroje výšek z disku (``local:<složka>`` nebo klíč z ``LOCAL_ELEVATION_DIRS``), jinak None."""
    from app import settings

    raw = LOCAL_ELEVATION_DIRS.get(elevation)
    if raw is None and elevation.startswith("local:"):
        raw = elevation.split(":", 1)[1].strip()
    if raw is None:
        return None
    path = Path(raw)
    return path if path.is_absolute() else Path(settings.DATA_ROOT) / path


def elevation_ready(country: Country) -> bool:
    """Má instance výšky země? U zdroje z disku musí být GeoTIFF terénu (``dtm``) i povrchu (``dsm``)."""
    base = local_elevation_dir(country.elevation)
    if base is None:
        return True
    try:
        return all(any((base / sub).glob("*.tif")) for sub in ("dtm", "dsm"))
    except OSError:
        return False


def beta_codes() -> frozenset[str]:
    """Kódy zemí ``status: beta`` povolené proměnnou ``COUNTRIES_BETA`` (``*`` = všechny beta)."""
    raw = os.environ.get("COUNTRIES_BETA")
    if raw is None:
        from app import settings

        raw = "*" if settings.INSTANCE_CHANNEL == "test" else ""
    return frozenset(c.strip().upper() for c in raw.replace(";", ",").split(",") if c.strip())


def _site_codes(site: Any) -> frozenset[str]:
    """Země domény (``Site.countries``); bez domény ze ``SITES`` jen CZ."""
    return frozenset(c.upper() for c in (getattr(site, "countries", None) or (DEFAULT_COUNTRY,)))


def _beta_for_site(code: str, site_codes: frozenset[str], beta: frozenset[str]) -> bool:
    """Beta země jen s ``COUNTRIES_BETA`` a jen doméně, která ji nabízí (``*`` nebo její kód)."""
    return ("*" in beta or code in beta) and ("*" in site_codes or code in site_codes)


def offered_codes() -> frozenset[str]:
    """Země, které instance nějaké doméně nabízí (``live`` a povolené ``beta``); podle nich stav zdrojů."""
    beta = beta_codes()
    site_sets = [_site_codes(s) for s in i18n.sites().values()] or [_site_codes(None)]
    return frozenset(
        c.code
        for c in load_countries().values()
        if (c.status == "live"
            or (c.status == "beta" and any(_beta_for_site(c.code, codes, beta) for codes in site_sets)))
        and elevation_ready(c)
    )


def allowed_countries(site: Any = None) -> list[Country]:
    """Země, které smí požadavek použít: země domény (``Site.countries``) ∩ ``status: live``, plus beta země
    z ``COUNTRIES_BETA``, které doména nabízí (česká doména ``:CZ`` zahraničí nedostane)."""
    countries = load_countries()
    codes = _site_codes(site)
    live = [c for c in countries.values() if c.status == "live"]
    out = live if "*" in codes else [c for c in live if c.code in codes]
    beta = beta_codes()
    if beta:
        out = out + [c for c in countries.values() if c.status == "beta" and _beta_for_site(c.code, codes, beta)]
    return [c for c in out if elevation_ready(c)]


# --------------------------------------------------------------------------- geometrie (bez shapely)


def _clip_polygon(ring, west, south, east, north):
    """Sutherland–Hodgman: obrys oříznutý obdélníkem (výsledek může být prázdný)."""
    pts = list(ring)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]

    def clip(points, inside, cross):
        out = []
        for i, cur in enumerate(points):
            prev = points[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(cross(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cross(prev, cur))
        return out

    def x_cross(x0):
        return lambda a, b: (x0, a[1] + (b[1] - a[1]) * (x0 - a[0]) / (b[0] - a[0]))

    def y_cross(y0):
        return lambda a, b: (a[0] + (b[0] - a[0]) * (y0 - a[1]) / (b[1] - a[1]), y0)

    for inside, cross in (
        (lambda p: p[0] >= west, x_cross(west)),
        (lambda p: p[0] <= east, x_cross(east)),
        (lambda p: p[1] >= south, y_cross(south)),
        (lambda p: p[1] <= north, y_cross(north)),
    ):
        if not pts:
            break
        pts = clip(pts, inside, cross)
    return pts


def _area(points) -> float:
    s = 0.0
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def bbox_share(bbox: tuple[float, float, float, float], country: Country) -> float:
    """Podíl plochy výřezu (west, south, east, north) uvnitř obrysu země, 0–1."""
    west, south, east, north = bbox
    cw, cs, ce, cn = country.envelope
    if east <= cw or west >= ce or north <= cs or south >= cn:
        return 0.0
    return rings_share(bbox, country.rings)


def rings_share(bbox: tuple[float, float, float, float], rings) -> float:
    """Podíl plochy výřezu uvnitř obrysů ``rings`` (lon, lat), 0–1."""
    west, south, east, north = bbox
    total = (east - west) * (north - south)
    if total <= 0:
        return 0.0
    # Obdélník je v lon/lat; cos(φ) je pro výřez konstanta, podíl ploch na ní nezávisí.
    inside = sum(_area(_clip_polygon(r, west, south, east, north)) for r in rings)
    return max(0.0, min(1.0, inside / total))


# --------------------------------------------------------------------------- kontrola výřezu


def check_bbox(
    bbox: tuple[float, float, float, float],
    site: Any = None,
    country_code: str | None = None,
) -> tuple[Country, float]:
    """Ověří výřez proti povolenému území; vrátí (zemi, podíl výřezu v ní) nebo vyhodí ``CountryError``.

    Bez ``country_code`` se vezme povolená země s největším podílem výřezu.
    """
    allowed = allowed_countries(site)
    if not allowed:
        raise CountryError(i18n.Msg("api.country.none_allowed"))
    if country_code:
        code = country_code.strip().upper()
        match = [c for c in allowed if c.code == code]
        if not match:
            raise CountryError(i18n.Msg("api.country.not_allowed", code=code))
        candidates = match
    else:
        candidates = allowed
    best, share = max(((c, bbox_share(bbox, c)) for c in candidates), key=lambda cs: cs[1])
    if share < MIN_SHARE:
        if share <= 0.0:
            names = [c.msg_name() for c in candidates]
            raise CountryError(
                i18n.Msg("api.country.outside", country=names[0])
                if len(names) == 1
                else i18n.Msg("api.country.outside_many", countries=CountryNameList(names))
            )
        raise CountryError(i18n.Msg("api.country.minority", pct=round(share * 100), country=best.msg_name()))
    return best, share


def outside_note(country: Country, share: float) -> str | None:
    """Věta o přesahu za hranici (log jobu, česky); None, když výřez leží celý v zemi."""
    if share >= FULL_SHARE:
        return None
    pct = max(1, round((1.0 - share) * 100))
    if country.cz_sources:
        return (
            f"{pct} % výřezu leží mimo {country.name()}: tam se kreslí jen z OSM "
            "(bez terénu z LiDARu, ZABAGED a DTM)."
        )
    # Mimo Česko: národní výšky končí na hranici (polygon země je s malou rezervou), za ní nemusí být terén ani porost.
    return (
        f"{pct} % výřezu leží mimo {country.name()}: tam se kreslí jen z OSM, bez místní velkoměřítkové mapy; "
        "terén a porost tam nemusí být (národní výšková data končí na hranici)."
    )


def outside_share(share: float) -> float:
    """Podíl mimo zemi pro API (0 pod tolerancí polygonu)."""
    return 0.0 if share >= FULL_SHARE else round(1.0 - share, 3)


def coverage_payload(site: Any = None, lang: str = "cs") -> dict[str, Any]:
    """Povolené země s obrysem pro mapu v GUI (``GET /api/coverage``)."""
    items = []
    for c in allowed_countries(site):
        w, s, e, n = c.envelope
        items.append(
            {
                "code": c.code,
                "name": c.name(lang),
                "bounds": [round(s, 4), round(w, 4), round(n, 4), round(e, 4)],
                "view": list(c.view),
                "geometry": c.geojson(),
                # Zvýraznění v mapě výběru: kde je výška + technická mapa (obdoba DTM), kde jen výška + OSM.
                "technical_map": c.has_technical_map,
                "technical_map_gaps": _rings_geojson(c.technical_map_gaps) if c.technical_map_gaps else None,
                "technical_map_area": _rings_geojson(c.technical_map_area) if c.technical_map_area else None,
                # Mapa v GUI šrafuje země, kde zeleň vychází jen základní (povrch ze snímků).
                "green": c.green,
            }
        )
    return {"countries": items, "min_share": MIN_SHARE}
