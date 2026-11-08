"""Zadání jobu pro nový formulář („Načíst zadání do nové mapy“).

Z uloženého jobu vrátí jen to, co formulář umí poslat znovu: název, výřez a volby
v podobě polí formuláře. Nikdy ne IP klienta, e-mail ani interní volby pipeline.
Starší joby bez některých klíčů dostanou výchozí hodnoty (stejné jako při vytvoření).
"""

from __future__ import annotations

import math
from typing import Any

from app.pipeline.job_options import (
    resolve_cliff_sensitivity,
    resolve_contour_smooth,
    resolve_formline_mode,
    resolve_map_types,
    resolve_osm_objects_mode,
    resolve_vege_height,
    resolve_veg_smooth,
)
from app.pipeline.package_oom import resolve_omap_job

# Stavy, po kterých je LAZ v jobu kompletní a jde převzít (iterace bez stahování).
_REUSE_STATUSES = {"done", "done_with_errors"}
_CLIFF_SYMBOLS = {"auto", "earth_bank", "symbol_206", "off"}
_RESIDUAL_SIZES = {"small", "medium", "large"}
NAME_MAX_LEN = 120


def _bool(raw: Any, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _bbox(raw: Any) -> list[float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        vals = [float(v) for v in raw]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in vals):
        return None
    west, south, east, north = vals
    if not (west < east and south < north):
        return None
    return vals


def _dtm_choice(opts: dict) -> str:
    """Jedna volba GUI `dtm` ze dvou uložených (`dtm_sprint` i `dtm_forest` výchozí ano od 3.18.0)."""
    sprint = _bool(opts.get("dtm_sprint"), True)
    forest = _bool(opts.get("dtm_forest"), True)
    if sprint and forest:
        return "1"
    if not sprint and not forest:
        return "0"
    return "auto"


def job_form_template(job: dict[str, Any], presets: dict | None = None) -> dict[str, Any]:
    """Zadání jobu jako hodnoty formuláře nové mapy."""
    opts = job.get("options") or {}
    notes: list[str] = []

    try:
        resolved = resolve_omap_job(
            opts.get("map_scale"),
            opts.get("contour_interval"),
            presets=presets,
            preset_id_fallback=str(job.get("preset_id") or "") or None,
        )
        map_scale = int(resolved["map_scale"])
        contour = resolved["contour_interval"]
    except ValueError:
        # Ekvidistance mezitím pro měřítko zakázaná: měřítko nech, ekvidistance výchozí.
        try:
            resolved = resolve_omap_job(
                opts.get("map_scale"),
                None,
                presets=presets,
                preset_id_fallback=str(job.get("preset_id") or "") or None,
            )
            map_scale = int(resolved["map_scale"])
            contour = resolved["contour_interval"]
            notes.append("Ekvidistance původní mapy už není k dispozici, je nastavená výchozí.")
        except ValueError:
            map_scale, contour = 10000, None
            notes.append("Měřítko původní mapy se nepodařilo načíst, je nastavené 1:10 000.")

    if map_scale == 4000:
        map_types: list[str] = []
    else:
        # Starší joby bez klíče stavěly les i MTBO (resolve_map_types → None).
        map_types = resolve_map_types(opts) or ["les", "mtbo"]

    cliff = str(opts.get("kp_cliff_symbol") or "auto").strip().lower()
    if cliff not in _CLIFF_SYMBOLS:
        cliff = "auto"  # i dřívější „rock_face“
    residual_size = str(opts.get("sprint_residual_size") or "small").strip().lower()
    if residual_size not in _RESIDUAL_SIZES:
        residual_size = "small"

    vege = resolve_vege_height(opts)
    form = {
        "map_scale": str(map_scale),
        "contour_interval": None if contour is None else (
            str(int(contour)) if float(contour).is_integer() else str(contour)
        ),
        "map_types": map_types,
        "contour_formlines": resolve_formline_mode(opts),
        "contour_smooth": str(resolve_contour_smooth(opts)),
        "veg_smooth": str(resolve_veg_smooth(opts)),
        "kp_vege_height": str(int(vege)) if float(vege).is_integer() else str(vege),
        "kp_cliff_symbol": cliff,
        "kp_cliff_sensitivity": resolve_cliff_sensitivity(opts),
        "kp_osm_objects": resolve_osm_objects_mode(opts),
        "dtm": _dtm_choice(opts),
        "sprint_residual_paved": _bool(opts.get("sprint_residual_paved"), False),
        "sprint_residual_size": residual_size,
        "output_georef": _bool(opts.get("output_georef"), False),
        # Zaškrtnuto = stáhnout šablony předem, jinak online šablony.
        "output_references": _bool(opts.get("output_references"), False),
    }

    bbox = _bbox(opts.get("bbox_wgs84"))
    if bbox is None:
        notes.append("Původní mapa nemá uložený výřez – vyznačte ho na mapě.")

    name = str(job.get("name") or "").strip()[:NAME_MAX_LEN]
    reuse = (
        job.get("id")
        if bbox is not None
        and job.get("status") in _REUSE_STATUSES
        and job.get("has_reusable_lidar")
        else None
    )
    return {
        "source_job_id": job.get("id"),
        "source_status": job.get("status"),
        "source_private": bool(job.get("private")),
        "name": name,
        "bbox_wgs84": bbox,
        "reuse_job_id": reuse,
        "form": form,
        "notes": notes,
    }
