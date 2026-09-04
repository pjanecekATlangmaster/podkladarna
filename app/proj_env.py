"""Nastavení PROJ_DATA pro GDAL/osr v Dockeru (conda share/proj často chybí/rozbitý)."""

from __future__ import annotations

import os
from pathlib import Path


def ensure_proj_data() -> str | None:
    """Nastaví PROJ_DATA/PROJ_LIB na adresář s proj.db. Vrací cestu nebo None."""
    candidates: list[Path] = []
    for key in ("PROJ_DATA", "PROJ_LIB"):
        raw = os.environ.get(key)
        if raw:
            candidates.append(Path(raw))
    try:
        from pyproj.datadir import get_data_dir

        candidates.append(Path(get_data_dir()))
    except Exception:
        pass
    for prefix in (
        Path("/opt/conda/share/proj"),
        Path("/usr/share/proj"),
        Path("/usr/local/share/proj"),
    ):
        candidates.append(prefix)

    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_dir() and (path / "proj.db").is_file():
            os.environ["PROJ_DATA"] = str(path)
            os.environ["PROJ_LIB"] = str(path)
            try:
                from osgeo import gdal, osr

                gdal.SetConfigOption("PROJ_DATA", str(path))
                gdal.SetConfigOption("PROJ_LIB", str(path))
                # GDAL 3.9+: SearchPaths před prvním použitím OSR.
                if hasattr(osr, "SetPROJSearchPaths"):
                    osr.SetPROJSearchPaths([str(path)])
            except Exception:
                pass
            return str(path)
    return None
