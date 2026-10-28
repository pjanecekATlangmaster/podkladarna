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

# Pomocné vrstevnice 103 (půl ekvidistance): sparse/more jen kde ukážou tvar
# navíc, all = všude jako vodítko (jako OCAD z LAZ).
FORMLINE_MODES = ("off", "sparse", "more", "all")
FORMLINE_MODE_DEFAULT = "off"


# Stupeň zaoblení plošné vegetace (louky 401, hustý porost 410):
# 0 = kostičky z rastru, 1 = zjednodušit + křivky, 2 = navíc vyhladit drobné
# výběžky a zátoky (obrysy korun stromů), 3 = silné vyhlazení ve větším okolí.
# Plocha se nikdy nepřelije přes cesty, budovy a vodu z OSM (viz veg_smooth).
VEG_SMOOTH_LEVELS = (0, 1, 2, 3)
VEG_SMOOTH_DEFAULT = 2
# Starý dvouhodnotový přepínač (off / round).
_VEG_SMOOTH_LEGACY = {"off": 0, "round": 1}


def resolve_veg_smooth(options: dict | None) -> int:
    raw = str((options or {}).get("veg_smooth", "")).strip().lower()
    if raw in _VEG_SMOOTH_LEGACY:
        return _VEG_SMOOTH_LEGACY[raw]
    try:
        level = int(raw)
    except ValueError:
        return VEG_SMOOTH_DEFAULT
    return level if level in VEG_SMOOTH_LEVELS else VEG_SMOOTH_DEFAULT


def _as_bool(raw, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on", "dtm"}:
        return True
    if text in {"0", "false", "no", "off", "osm"}:
        return False
    return default


def resolve_use_dtm(options: dict | None, preset_id: str) -> bool:
    """Kreslit preset z DTM? Sprint: `dtm_sprint` (výchozí ano); les a MTBO: `dtm_forest` (výchozí ne od 2.43.0,
    Petr 2026-10-27; GUI má jednu volbu `dtm`, která nastaví obě)."""
    opts = options or {}
    if str(preset_id).startswith("sprint"):
        return _as_bool(opts.get("dtm_sprint"), True)
    return _as_bool(opts.get("dtm_forest"), False)


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


def resolve_formline_mode(options: dict | None) -> str:
    raw = str((options or {}).get("contour_formlines") or "").strip().lower()
    return raw if raw in FORMLINE_MODES else FORMLINE_MODE_DEFAULT
