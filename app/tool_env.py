from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent


_OSGEO_CANDIDATES = (
    os.environ.get("OSGEO4W_ROOT") or "",
    r"C:\QGIS",
    r"C:\OSGeo4W",
    r"C:\OSGeo4W64",
)

def osgeo4w_root() -> Path | None:
    for raw in _OSGEO_CANDIDATES:
        if not raw:
            continue
        root = Path(raw)
        if (root / "bin" / "pdal.exe").exists():
            return root
    return None


def osgeo_proj_dir(root: Path | None = None) -> Path | None:
    """``share/proj`` next to the QGIS/OSGeo4W root used for ogr2ogr/pdal."""
    root = root or osgeo4w_root()
    if not root:
        return None
    path = root / "share" / "proj"
    if (path / "proj.db").exists():
        return path
    return None


def proj_data_dir() -> Path | None:
    """PROJ data matching local QGIS/OSGeo GDAL; pyproj only as fallback.

    QGIS GDAL (libproj) expects a matching ``proj.db``. Pip pyproj often ships an
    older layout (e.g. MINOR=4) while QGIS 3.4x needs MINOR>=6 — pointing
    ``PROJ_DATA`` at pyproj then breaks ``ogr2ogr`` EPSG:5514.
    """
    osgeo = osgeo_proj_dir()
    if osgeo is not None:
        return osgeo
    try:
        from pyproj.datadir import get_data_dir

        path = Path(get_data_dir())
        if (path / "proj.db").exists():
            return path
    except Exception:
        pass
    return None


def osgeo_scripts_dir(root: Path | None = None) -> Path | None:
    """``apps/Python3xx/Scripts`` s gdal_calc / gdal_polygonize (QGIS/OSGeo4W)."""
    root = root or osgeo4w_root()
    if not root:
        return None
    apps = root / "apps"
    if not apps.is_dir():
        return None
    # Nejnovější Python*Scripts, které mají gdal_calc.
    for scripts in sorted(apps.glob("Python*/Scripts"), reverse=True):
        if (scripts / "gdal_calc.exe").exists() or (scripts / "gdal_calc.bat").exists():
            return scripts
    return None


# QGIS ships these as separate DLLs. They are unused for GeoTIFF / LAZ / SHP /
# EPSG:5514 and on Windows often fail to load ("Can't load requested DLL" / 127).
_OPTIONAL_QGIS_PLUGIN_DLLS = (
    "gdal_ECW_JP2ECW.dll",
    "gdal_GEOR.dll",
    "gdal_HDF5.dll",
    "gdal_MrSID.dll",
    "ogr_MSSQLSpatial.dll",
    "ogr_OCI.dll",
    "ogr_SOSI.dll",
)

OPTIONAL_GDAL_PLUGINS_NOTE = (
    "Ignoruji volitelné GDAL pluginy QGIS (ECW/MrSID/…) — pro Podkladárnu nejsou potřeba."
)

_optional_gdal_plugins_ignored = False
_optional_gdal_plugins_logged = False


def qgis_gdal_plugins_dir(root: Path | None = None) -> Path | None:
    """``apps/gdal/lib/gdalplugins`` — optional ECW/MrSID/HDF5/OCI plugins."""
    root = root or osgeo4w_root()
    if not root:
        return None
    path = root / "apps" / "gdal" / "lib" / "gdalplugins"
    if path.is_dir() and any((path / name).is_file() for name in _OPTIONAL_QGIS_PLUGIN_DLLS):
        return path
    return None


def _same_dir(left: str, right: Path) -> bool:
    try:
        return Path(left).expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return os.path.normcase(os.path.normpath(left)) == os.path.normcase(
            os.path.normpath(str(right))
        )


def suppress_optional_gdal_plugins(env: dict[str, str]) -> bool:
    """Stop GDAL from probing broken optional QGIS plugins.

    Sets ``GDAL_DRIVER_PATH=disable`` when that variable is missing or points
    only at the QGIS ``gdalplugins`` directory. Another directory listed next
    to it is kept. ``GDAL_SKIP`` is not set: with plugins disabled, GDAL warns
    ``Unable to find driver … to unload`` for every name. ``CPL_LOG`` is not
    set, so a missing file or a PROJ/SRS error still reaches stderr.
    """
    global _optional_gdal_plugins_ignored
    plugins = qgis_gdal_plugins_dir()
    if plugins is None:
        return False
    raw = env.get("GDAL_DRIVER_PATH")
    if raw is not None and raw.strip().lower() == "disable":
        _optional_gdal_plugins_ignored = True
        return True
    parts = [part.strip() for part in (raw or "").split(os.pathsep) if part.strip()]
    kept = [part for part in parts if not _same_dir(part, plugins)]
    if kept and len(kept) == len(parts):
        return False
    if kept:
        env["GDAL_DRIVER_PATH"] = os.pathsep.join(kept)
    else:
        env["GDAL_DRIVER_PATH"] = "disable"
    _optional_gdal_plugins_ignored = True
    return True


def log_ignored_gdal_plugins() -> None:
    """One info line after logging is configured. No-op until a handler exists."""
    global _optional_gdal_plugins_logged
    if _optional_gdal_plugins_logged or not _optional_gdal_plugins_ignored:
        return
    logger = logging.getLogger("podkladarna")
    if not logger.hasHandlers() and not logging.getLogger().hasHandlers():
        return
    _optional_gdal_plugins_logged = True
    logger.info(OPTIONAL_GDAL_PLUGINS_NOTE)


def apply_local_gis_env() -> Path | None:
    """Na Windows doplní OSGeo4W/QGIS do PATH a GDAL/PDAL/PROJ data."""
    if os.name != "nt":
        return None
    root = osgeo4w_root()
    if not root:
        return None
    os.environ["OSGEO4W_ROOT"] = str(root)
    bin_dir = str(root / "bin")
    path = os.environ.get("PATH", "")
    if bin_dir.lower() not in path.lower():
        # Append, ať OSGeo4W python nepřebije systémový interpreter.
        os.environ["PATH"] = path + os.pathsep + bin_dir
    scripts = osgeo_scripts_dir(root)
    if scripts is not None:
        scripts_s = str(scripts)
        if scripts_s.lower() not in os.environ.get("PATH", "").lower():
            os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + scripts_s

    mapping = {
        "PDAL_DRIVER_PATH": root / "apps" / "pdal" / "plugins",
        "GDAL_DATA": root / "apps" / "gdal" / "share" / "gdal",
    }
    for key, folder in mapping.items():
        if folder.is_dir():
            os.environ.setdefault(key, str(folder))
    # Do not point GDAL_DRIVER_PATH at gdalplugins (ECW/MrSID/… fail to load).
    suppress_optional_gdal_plugins(os.environ)

    # Always pin PROJ to QGIS/OSGeo share/proj when present (not pip pyproj).
    compatible = proj_data_dir()
    if compatible:
        os.environ["PROJ_DATA"] = str(compatible)
        os.environ["PROJ_LIB"] = str(compatible)
    return root


def gis_subprocess_env(exe: str | None = None) -> dict[str, str]:
    """Env for pdal/ogr2ogr/gdal_calc: PROJ_DATA must match the GDAL binary's libproj.

    Always sets ``OSGEO4W_ROOT`` when QGIS/OSGeo root exists — ``gdal_calc.exe``
    and sibling Scripts tools need it even when not invoked via ``*.bat``.
    """
    env = os.environ.copy()
    root = osgeo4w_root()
    proj = osgeo_proj_dir(root) or proj_data_dir()
    if root:
        env["OSGEO4W_ROOT"] = str(root)
        osgeo_proj = osgeo_proj_dir(root)
        if osgeo_proj is not None:
            proj = osgeo_proj
        gdal_data = root / "apps" / "gdal" / "share" / "gdal"
        if gdal_data.is_dir():
            env["GDAL_DATA"] = str(gdal_data)
        pdal_plug = root / "apps" / "pdal" / "plugins"
        if pdal_plug.is_dir():
            env["PDAL_DRIVER_PATH"] = str(pdal_plug)
    if proj:
        env["PROJ_DATA"] = str(proj)
        env["PROJ_LIB"] = str(proj)
    suppress_optional_gdal_plugins(env)
    return env


def _tool_stem(name: str) -> str:
    stem = name
    lower = stem.lower()
    if lower.endswith(".exe") or lower.endswith(".bat"):
        stem = stem[:-4]
    elif lower.endswith(".py"):
        stem = stem[:-3]
    return stem


def which_tool(name: str) -> str | None:
    """Najde GDAL/PDAL nástroj: ``bin/*.exe``, pak ``apps/Python*/Scripts`` (gdal_calc)."""
    stem = _tool_stem(name)
    if os.name == "nt":
        root = osgeo4w_root()
        if root:
            exe = root / "bin" / f"{stem}.exe"
            if exe.exists():
                return str(exe)
            scripts = osgeo_scripts_dir(root)
            if scripts is not None:
                for cand in (f"{stem}.exe", f"{stem}.bat", f"{stem}.py"):
                    path = scripts / cand
                    if path.exists():
                        return str(path)
    found = shutil.which(stem) or shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        found = shutil.which(f"{stem}.exe")
        if found:
            return found
    return None


def tool_status() -> dict[str, str | None]:
    return {
        "pdal": which_tool("pdal"),
        "ogr2ogr": which_tool("ogr2ogr"),
        "ogrinfo": which_tool("ogrinfo"),
        "gdal_translate": which_tool("gdal_translate"),
        "gdal_calc": which_tool("gdal_calc"),
        "gdal_polygonize": which_tool("gdal_polygonize"),
    }
