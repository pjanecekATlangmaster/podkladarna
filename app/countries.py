"""Podporované země a povolené území výřezu (kontrola na serveru i podklad pro mapu v GUI).

Konfigurace ``configs/countries.yaml`` + polygony hranic ``configs/countries/<kód>.geojson``.
Povolené území požadavku = země domény (``SITES``, ``*`` = všechny) ∩ země se ``status: live``; bez ``SITES`` jen CZ.

Výřez patří zemi, ve které leží aspoň ``MIN_SHARE`` jeho plochy (Petr 2026-11-14, karta „většina v zemi, zbytek
z OSM“). Přesah za hranici projde, ale kreslí se jen z OSM (LiDAR, ZABAGED ani DTM tam nejsou) a web i log
na to upozorní. Podíl se počítá v rovině lon × cos(φ) × lat – na výřez do desítek km stačí.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Country:
    code: str
    names: dict[str, str]
    status: str
    crs: str
    rings: tuple[tuple[tuple[float, float], ...], ...]  # vnější obrysy (lon, lat), bez děr
    view: tuple[float, float, float, float]  # jih, západ, sever, východ

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
        polys = [[[list(p) for p in ring]] for ring in self.rings]
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
        )
        if len(view) != 4:
            w, s, e, n = country.envelope
            country = Country(country.code, country.names, country.status, country.crs, rings, (s, w, n, e))
        out[code] = country
    return out


def allowed_countries(site: Any = None) -> list[Country]:
    """Země, které smí požadavek použít: země domény (``Site.countries``) ∩ ``status: live``."""
    countries = load_countries()
    codes = tuple(getattr(site, "countries", None) or (DEFAULT_COUNTRY,))
    live = [c for c in countries.values() if c.status == "live"]
    if "*" in codes:
        return live
    wanted = {c.upper() for c in codes}
    return [c for c in live if c.code in wanted]


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
    total = (east - west) * (north - south)
    if total <= 0:
        return 0.0
    cw, cs, ce, cn = country.envelope
    if east <= cw or west >= ce or north <= cs or south >= cn:
        return 0.0
    # Obdélník je v lon/lat; cos(φ) je pro výřez konstanta, podíl ploch na ní nezávisí.
    inside = sum(_area(_clip_polygon(r, west, south, east, north)) for r in country.rings)
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
                else i18n.Msg("api.country.outside_many", countries=", ".join(names))
            )
        raise CountryError(i18n.Msg("api.country.minority", pct=round(share * 100), country=best.msg_name()))
    return best, share


def outside_note(country: Country, share: float) -> str | None:
    """Věta o přesahu za hranici (log jobu, česky); None, když výřez leží celý v zemi."""
    if share >= FULL_SHARE:
        return None
    pct = max(1, round((1.0 - share) * 100))
    return (
        f"{pct} % výřezu leží mimo {country.name()}: tam se kreslí jen z OSM "
        "(bez terénu z LiDARu, ZABAGED a DTM)."
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
            }
        )
    return {"countries": items, "min_share": MIN_SHARE}
