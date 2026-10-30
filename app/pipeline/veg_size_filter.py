"""Vegetační size/tvar filtr — zrušen (≥2.2.17, Petr 2026-10-05).

Min-size cleanup vegetace byl zrušen kompletně; úklid drobných fleků
necháno na uživateli. Zůstaly jen ``is_sprint_preset`` (používá
``vegetation_gdal``) a tenký ``resolve_veg_size_profile`` pro ``run_job``.
"""

from __future__ import annotations

from typing import Any


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
) -> str:
    """Vždy ``default``; odstraní se po úpravě ``run_job`` (volba je bez účinku)."""
    return "default"
