from __future__ import annotations

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

_PULLAUTA_CANDIDATES = (
    APP_ROOT / "bin" / "pullauta.exe",
    APP_ROOT / "bin" / "pullauta",
    APP_ROOT.parent / "karttapullautin-x86_64-win" / "pullauta.exe",
    Path(r"D:\Downloads\karttapullautin-x86_64-win\pullauta.exe"),
    Path(r"C:\Users\PetrJanecek\Downloads\karttapullautin-x86_64-win\pullauta.exe"),
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

    mapping = {
        "PDAL_DRIVER_PATH": root / "apps" / "pdal" / "plugins",
        "GDAL_DATA": root / "apps" / "gdal" / "share" / "gdal",
        "GDAL_DRIVER_PATH": root / "apps" / "gdal" / "lib" / "gdalplugins",
    }
    for key, folder in mapping.items():
        if folder.is_dir():
            os.environ.setdefault(key, str(folder))

    # Always pin PROJ to QGIS/OSGeo share/proj when present (not pip pyproj).
    compatible = proj_data_dir()
    if compatible:
        os.environ["PROJ_DATA"] = str(compatible)
        os.environ["PROJ_LIB"] = str(compatible)
    return root


def gis_subprocess_env(exe: str | None = None) -> dict[str, str]:
    """Env for pdal/ogr2ogr/ogrinfo: PROJ_DATA must match the GDAL binary's libproj."""
    env = os.environ.copy()
    root = osgeo4w_root()
    proj = osgeo_proj_dir(root) or proj_data_dir()
    if root and exe:
        try:
            under = Path(exe).resolve().is_relative_to(root.resolve())
        except (OSError, ValueError):
            under = False
        if under:
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
    return env


def resolve_pullauta() -> str:
    explicit = os.environ.get("PULLAUTA_BIN")
    if explicit and Path(explicit).exists():
        return explicit
    which = shutil.which("pullauta") or shutil.which("pullauta.exe")
    if which:
        return which
    for candidate in _PULLAUTA_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    return explicit or "/usr/local/bin/pullauta"


def which_tool(name: str) -> str | None:
    if os.name == "nt":
        root = osgeo4w_root()
        if root:
            exe = root / "bin" / f"{name}.exe"
            if exe.exists():
                return str(exe)
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        found = shutil.which(f"{name}.exe")
        if found:
            return found
    return None


def tool_status() -> dict[str, str | None]:
    return {
        "pdal": which_tool("pdal"),
        "ogr2ogr": which_tool("ogr2ogr"),
        "ogrinfo": which_tool("ogrinfo"),
        "pullauta": resolve_pullauta() if Path(resolve_pullauta()).exists() else None,
    }
