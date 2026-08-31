from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from app.settings import CONFIG_DIR


def load_presets() -> dict:
    return yaml.safe_load((CONFIG_DIR / "presets.yaml").read_text(encoding="utf-8"))


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

    overrides: dict[str, str | int | float] = {
        "vectorconf": preset.get("vectorconf", "zabaged.txt"),
        "contour_interval": opts.get("contour_interval", preset["contour_interval"]),
        "basemapinterval": opts.get("basemapinterval", preset["basemapinterval"]),
        "scalefactor": opts.get("scalefactor", preset["scalefactor"]),
        "formline": opts.get("formline", preset["formline"]),
        "smoothing": opts.get("smoothing", preset.get("smoothing", 0.7)),
        "processes": opts.get("processes", preset.get("processes", 2)),
        "output_dxf": 1 if opts.get("output_dxf", True) else 0,
        "savetempfolders": 1 if opts.get("savetempfolders", False) else 0,
        "savetempfiles": 1 if opts.get("savetempfolders", False) else 0,
        "batch": 0,
        "vegeonly": 0,
        "contoursonly": 0,
        "cliffsonly": 0,
    }

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

    shutil.copy2(CONFIG_DIR / "zabaged.txt", work_dir / "zabaged.txt")
    return ini_path
