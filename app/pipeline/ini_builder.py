from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from app.settings import CONFIG_DIR

# greenhigh (m) – body nad touto výškou jdou do „high“ vegetace.
KP_VEGE_HEIGHT_CHOICES = (1.5, 2.0, 2.5, 3.0)
KP_VEGE_HEIGHT_DEFAULT = 2.0

# cliff1/cliff2 – min. výškový skok; nižší = citlivější (více srázů).
# „normal“ = současný pullauta.base.ini (1.4 / 2.8).
KP_CLIFF_SENSITIVITY: dict[str, tuple[float, float]] = {
    "low": (1.8, 3.4),
    "normal": (1.4, 2.8),
    "high": (1.15, 2.0),
    "very_high": (0.95, 1.7),
}
KP_CLIFF_SENSITIVITY_DEFAULT = "normal"


def load_presets() -> dict:
    return yaml.safe_load((CONFIG_DIR / "presets.yaml").read_text(encoding="utf-8"))


def kp_contour_interval(ground_m: float, scalefactor: float, formline: float) -> float:
    """Convert metres-on-ground contour interval to Karttapullautin's INI value.

    KP always generates contours at ``contour_interval / 2 * scalefactor``.
    With ``formline > 0`` every other line is a formline, so main contours
    land at ``contour_interval * scalefactor``. With ``formline = 0`` every
    half-interval line is drawn as a full contour.
    """
    sf = float(scalefactor)
    if sf <= 0:
        sf = 1.0
    ground = float(ground_m)
    if float(formline) > 0:
        return ground / sf
    return 2.0 * ground / sf


def _ini_number(value: float) -> int | float:
    rounded = round(float(value), 6)
    as_int = round(rounded)
    if abs(rounded - as_int) < 1e-9:
        return int(as_int)
    return rounded


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


def write_pullauta_ini(
    work_dir: Path,
    preset_id: str,
    options: dict | None = None,
) -> Path:
    presets = load_presets()
    if preset_id not in presets:
        raise KeyError(f"Neznamy preset: {preset_id}")
    preset = presets[preset_id]
    opts = options or {}

    base_src = CONFIG_DIR / "pullauta.base.ini"
    if base_src.exists():
        lines = base_src.read_text(encoding="utf-8", errors="ignore").splitlines()
    else:
        lines = []

    scalefactor = opts.get("scalefactor", preset["scalefactor"])
    formline = opts.get("formline", preset["formline"])
    ground_interval = opts.get("contour_interval", preset["contour_interval"])
    contour_interval = _ini_number(
        kp_contour_interval(ground_interval, scalefactor, formline)
    )
    vectorconf = Path(str(preset.get("vectorconf", "zabaged.txt"))).name
    src_conf = CONFIG_DIR / vectorconf
    if not src_conf.is_file():
        raise FileNotFoundError(f"Chybí vectorconf: {src_conf}")

    vege_h = resolve_vege_height(opts)
    cliff_key = resolve_cliff_sensitivity(opts)
    cliff1, cliff2 = KP_CLIFF_SENSITIVITY[cliff_key]

    overrides: dict[str, str | int | float] = {
        "vectorconf": vectorconf,
        "contour_interval": contour_interval,
        "basemapinterval": opts.get("basemapinterval", preset["basemapinterval"]),
        "scalefactor": scalefactor,
        "formline": formline,
        "smoothing": opts.get("smoothing", preset.get("smoothing", 0.7)),
        "processes": opts.get("processes", preset.get("processes", 2)),
        "output_dxf": 0,
        "savetempfolders": 1 if opts.get("savetempfolders", False) else 0,
        "savetempfiles": 1 if opts.get("savetempfolders", False) else 0,
        "batch": 0,
        "vegeonly": 0,
        "contoursonly": 0,
        "cliffsonly": 0,
        "buildingcolor": opts.get(
            "buildingcolor", preset.get("buildingcolor", "0,0,0")
        ),
        "greenhigh": _ini_number(vege_h),
        "cliff1": _ini_number(cliff1),
        "cliff2": _ini_number(cliff2),
    }
    indexcontours = opts.get("indexcontours", preset.get("indexcontours"))
    if indexcontours is not None:
        overrides["indexcontours"] = indexcontours

    disabled_keys = {"waterelevation", "buildingsclass"}
    out: list[str] = []
    seen: set[str] = set()

    for line in lines:
        stripped = line.strip()
        key = None
        if stripped.startswith("#"):
            body = stripped.lstrip("#").strip()
            if "=" in body:
                key = body.split("=", 1)[0].strip()
            if key in disabled_keys:
                out.append(line if stripped.startswith("#") else f"# {stripped}")
                continue
            if key in overrides and key not in seen:
                out.append(f"{key}={overrides[key]}")
                seen.add(key)
                continue
            out.append(line)
            continue
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
        if key in disabled_keys:
            out.append(f"# {stripped}")
            continue
        if key in overrides:
            if key not in seen:
                out.append(f"{key}={overrides[key]}")
                seen.add(key)
            continue
        out.append(line)

    for key, val in overrides.items():
        if key not in seen:
            out.append(f"{key}={val}")

    work_dir.mkdir(parents=True, exist_ok=True)
    ini_path = work_dir / "pullauta.ini"
    ini_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    shutil.copy2(src_conf, work_dir / vectorconf)
    return ini_path
