"""Presety map a čtení voleb jobu z formuláře / API."""

from __future__ import annotations

import yaml

from app.settings import CONFIG_DIR

# greenhigh (m) – body nad touto výškou jdou do „high“ vegetace.
# Volba ve formuláři / API: kp_vege_height (prefix je historický, ne runtime KP).
KP_VEGE_HEIGHT_CHOICES = (1.5, 2.0, 2.5, 3.0)
KP_VEGE_HEIGHT_DEFAULT = 2.0

# cliff1/cliff2 – min. výškový skok; nižší = citlivější (více srázů).
# Výchozí „low“: ještě vyšší práh proti falešným zemním srázům (kartografie).
# „normal“ = dřívější low (1,8 / 3,4). Citlivější „high“ = 1,4 / 2,8.
KP_CLIFF_SENSITIVITY: dict[str, tuple[float, float]] = {
    "low": (2.2, 4.0),
    "normal": (1.8, 3.4),
    "high": (1.4, 2.8),
    "very_high": (1.15, 2.0),
}
KP_CLIFF_SENSITIVITY_DEFAULT = "low"


def load_presets() -> dict:
    return yaml.safe_load((CONFIG_DIR / "presets.yaml").read_text(encoding="utf-8"))


def resolve_vege_height(options: dict | None) -> float:
    raw = (options or {}).get("kp_vege_height", KP_VEGE_HEIGHT_DEFAULT)
    try:
        h = float(raw)
    except (TypeError, ValueError):
        return KP_VEGE_HEIGHT_DEFAULT
    for choice in KP_VEGE_HEIGHT_CHOICES:
        if abs(h - choice) < 1e-6:
            return choice
    return KP_VEGE_HEIGHT_DEFAULT


def resolve_cliff_sensitivity(options: dict | None) -> str:
    raw = str((options or {}).get("kp_cliff_sensitivity") or KP_CLIFF_SENSITIVITY_DEFAULT)
    key = raw.strip().lower()
    if key in KP_CLIFF_SENSITIVITY:
        return key
    return KP_CLIFF_SENSITIVITY_DEFAULT
