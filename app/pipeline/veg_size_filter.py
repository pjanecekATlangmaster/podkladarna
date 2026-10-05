"""Vegetační size/tvar filtr — **vypnutý** (≥2.2.17).

Petr 2026-10-05: min-size cleanup zrušen kompletně (veg, skály, path stubs);
úklid drobných fleků necháno na uživateli. Jediná výjimka v pipeline je
min. délka srázů **104** (~50 m @ 1:10k).

API (`keep_veg_polygon`, prahy, ``veg_size_profile``) zůstává kvůli
zpětné kompatibilitě testů / A/B skriptu, ale ``keep_veg_polygon`` vždy
nechá polygon (kromě None / empty).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

VegSizeProfile = Literal["default", "strict"]

_GREEN_CODES = frozenset({"406", "408", "410"})
_VEG_CODES = frozenset({"401"}) | _GREEN_CODES

# Důvody pro uzitecne/vyhozene (SHP pole duvod, ASCII) — legacy, filtr je off.
REASON_SMALL_AREA = "mala_plocha"
REASON_COMPACT = "kompaktni_flek"

# Globální vypínač min-size / tvarového cutu vegetace.
VEG_SIZE_FILTER_ENABLED = False


@dataclass(frozen=True)
class VegSizeThresholds:
    hard_floor_m2: float
    soft_min_m2: float
    band_length_min_m: float
    band_aspect_min: float


# Legacy prahy (dokumentace / A/B skript) — pipeline je neaplikuje.
_DEFAULT_401 = VegSizeThresholds(6.0, 12.0, 20.0, 4.0)
_DEFAULT_GREEN = VegSizeThresholds(12.0, 25.0, 25.0, 5.0)
_STRICT_401 = VegSizeThresholds(8.0, 20.0, 25.0, 4.0)
_STRICT_GREEN = VegSizeThresholds(15.0, 40.0, 30.0, 5.0)

_PROFILES: dict[str, dict[str, VegSizeThresholds]] = {
    "default": {"401": _DEFAULT_401, "green": _DEFAULT_GREEN},
    "strict": {"401": _STRICT_401, "green": _STRICT_GREEN},
}


def is_sprint_preset(
    *,
    preset_id: str | None = None,
    map_scale: int | float | None = None,
) -> bool:
    try:
        if map_scale is not None and int(map_scale) == 4000:
            return True
    except (TypeError, ValueError):
        pass
    pid = (preset_id or "").strip().lower()
    return pid.startswith("sprint")


def resolve_veg_size_profile(
    options: dict[str, Any] | None = None,
    *,
    preset_id: str | None = None,
    map_scale: int | float | None = None,
) -> VegSizeProfile:
    """Sprint vždy ``default``; jinak ``veg_size_profile`` z options (default/strict).

    Profil se při ``VEG_SIZE_FILTER_ENABLED=False`` stejně neaplikuje.
    """
    opts = options or {}
    if is_sprint_preset(preset_id=preset_id, map_scale=map_scale):
        return "default"
    if preset_id is None:
        preset_id = str(opts.get("preset_id") or opts.get("preset") or "") or None
    if map_scale is None and opts.get("map_scale") is not None:
        map_scale = opts.get("map_scale")
    if is_sprint_preset(preset_id=preset_id, map_scale=map_scale):
        return "default"
    raw = str(opts.get("veg_size_profile") or "default").strip().lower()
    if raw in {"strict", "prisnejsi", "přísnější", "aggressive"}:
        return "strict"
    return "default"


def thresholds_for(code: str, profile: VegSizeProfile | str = "default") -> VegSizeThresholds:
    prof = _PROFILES.get(str(profile), _PROFILES["default"])
    if code in _GREEN_CODES:
        return prof["green"]
    return prof["401"]


def soft_min_for_code(code: str, profile: VegSizeProfile | str = "default") -> float:
    """Zpětná kompatibilita pro testy / starší „min area“ čtení (= soft_min)."""
    return thresholds_for(code, profile).soft_min_m2


def mrr_width_length(poly) -> tuple[float, float]:
    """Šířka (kratší) a délka (delší) minimum rotated rectangle — shapely Polygon."""
    try:
        mrr = poly.minimum_rotated_rectangle
        coords = list(mrr.exterior.coords)
    except Exception:
        minx, miny, maxx, maxy = poly.bounds
        w, h = maxx - minx, maxy - miny
        return min(w, h), max(w, h)
    sides = [
        math.hypot(coords[i][0] - coords[i + 1][0], coords[i][1] - coords[i + 1][1])
        for i in range(4)
    ]
    return min(sides), max(sides)


def _as_shapely(geom):
    """Shapely geom, nebo OGR → WKB → shapely; jinak None."""
    if geom is None:
        return None
    if hasattr(geom, "area") and hasattr(geom, "is_empty") and hasattr(geom, "bounds"):
        if getattr(geom, "geom_type", None) or hasattr(geom, "minimum_rotated_rectangle"):
            return geom
    try:
        from shapely import from_wkb

        wkb = geom.ExportToWkb()
        return from_wkb(bytes(wkb))
    except Exception:
        return None


def _geom_area(geom) -> float:
    if hasattr(geom, "GetArea"):
        return float(geom.GetArea())
    return float(geom.area)


def keep_veg_polygon(
    code: str,
    geom,
    *,
    profile: VegSizeProfile | str = "default",
) -> tuple[bool, str | None]:
    """Vrátí ``(keep, duvod|None)`` pro jeden vegetační polygon.

    Od ≥2.2.17 vždy ``(True, None)`` pro neprázdný geom — min-size/tvar cut vypnutý.
    """
    if geom is None:
        return False, REASON_SMALL_AREA
    try:
        empty = geom.IsEmpty() if hasattr(geom, "IsEmpty") else geom.is_empty
    except Exception:
        empty = False
    if empty:
        return False, REASON_SMALL_AREA

    if not VEG_SIZE_FILTER_ENABLED:
        return True, None

    if code not in _VEG_CODES:
        return True, None

    thr = thresholds_for(code, profile)
    area = _geom_area(geom)
    if area < thr.hard_floor_m2:
        return False, REASON_SMALL_AREA
    if area >= thr.soft_min_m2:
        return True, None

    poly = _as_shapely(geom)
    if poly is None or poly.is_empty:
        return False, REASON_COMPACT
    if getattr(poly, "geom_type", None) == "MultiPolygon":
        parts = [g for g in poly.geoms if not g.is_empty]
        if not parts:
            return False, REASON_SMALL_AREA
        poly = max(parts, key=lambda g: float(g.area))

    width, length = mrr_width_length(poly)
    aspect = length / max(width, 1e-9)
    if length >= thr.band_length_min_m and aspect >= thr.band_aspect_min:
        return True, None
    return False, REASON_COMPACT
