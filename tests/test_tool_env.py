from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import app.tool_env as tool_env
from app.tool_env import (
    OPTIONAL_GDAL_PLUGINS_NOTE,
    gis_subprocess_env,
    log_ignored_gdal_plugins,
    osgeo4w_root,
    osgeo_proj_dir,
    osgeo_scripts_dir,
    proj_data_dir,
    suppress_optional_gdal_plugins,
    tool_status,
    which_tool,
)


def test_tool_status_keys():
    status = tool_status()
    assert set(status) >= {"pdal", "ogr2ogr", "ogrinfo", "pullauta"}
    assert "gdal_calc" in status
    assert "gdal_polygonize" in status


def test_health_reports_tools(client):
    body = client.get("/api/health").json()
    assert "tools" in body
    assert "pipeline_ready" in body
    assert "pdal" in body["tools"]


def test_gis_env_prefers_osgeo_proj_over_pyproj():
    proj = proj_data_dir()
    assert proj is not None
    assert (proj / "proj.db").exists()
    env = gis_subprocess_env()
    assert Path(env["PROJ_DATA"]) == proj
    assert Path(env["PROJ_LIB"]) == proj
    assert (Path(env["PROJ_DATA"]) / "proj.db").exists()
    # On this Windows worker QGIS wins; never point subprocess at pip pyproj.
    assert "pyproj" not in str(Path(env["PROJ_DATA"])).lower() or osgeo4w_root() is None


def test_gis_subprocess_env_pins_qgis_proj_for_ogr2ogr():
    root = osgeo4w_root()
    if root is None:
        return
    ogr = which_tool("ogr2ogr")
    assert ogr
    env = gis_subprocess_env(ogr)
    expected = osgeo_proj_dir(root)
    assert expected is not None
    assert Path(env["PROJ_DATA"]) == expected
    assert Path(env["PROJ_LIB"]) == expected
    assert "pyproj" not in str(env["PROJ_DATA"]).lower()
    assert env.get("OSGEO4W_ROOT") == str(root)


def test_which_tool_finds_gdal_calc_in_qgis_scripts():
    root = osgeo4w_root()
    if root is None:
        return
    scripts = osgeo_scripts_dir(root)
    if scripts is None:
        return
    calc = which_tool("gdal_calc")
    assert calc is not None
    assert "gdal_calc" in Path(calc).name.lower()
    env = gis_subprocess_env(calc)
    assert env.get("OSGEO4W_ROOT") == str(root)


def test_ogr2ogr_assigns_s_jtsk(monkeypatch, tmp_path):
    captured: dict = {}

    class Result:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return Result()

    monkeypatch.setattr("app.pipeline.fetch_zabaged.subprocess.run", fake_run)
    from app.pipeline.fetch_zabaged import _ogr2ogr_shp

    gj = tmp_path / "a.geojson"
    gj.write_text("{}", encoding="utf-8")
    _ogr2ogr_shp("ogr2ogr", gj, tmp_path / "a.shp", (1.0, 2.0, 3.0, 4.0))
    assert captured["cmd"][captured["cmd"].index("-s_srs") + 1] == "EPSG:5514"
    assert captured["cmd"][captured["cmd"].index("-t_srs") + 1] == "EPSG:5514"
    assert captured["env"]["PROJ_DATA"]


def test_suppress_optional_plugins_disables_qgis_dir(tmp_path, monkeypatch):
    plugins = tmp_path / "gdalplugins"
    plugins.mkdir()
    (plugins / "gdal_ECW_JP2ECW.dll").write_bytes(b"")
    other = tmp_path / "custom-plugins"
    other.mkdir()
    monkeypatch.setattr(tool_env, "qgis_gdal_plugins_dir", lambda root=None: plugins)

    only = {"GDAL_DRIVER_PATH": str(plugins), "PROJ_DATA": r"C:\QGIS\share\proj"}
    assert suppress_optional_gdal_plugins(only) is True
    assert only["GDAL_DRIVER_PATH"] == "disable"
    assert only["PROJ_DATA"].endswith("proj")
    assert "GDAL_SKIP" not in only
    assert "CPL_LOG" not in only

    unset: dict[str, str] = {}
    assert suppress_optional_gdal_plugins(unset) is True
    assert unset["GDAL_DRIVER_PATH"] == "disable"

    mixed = {"GDAL_DRIVER_PATH": str(plugins) + ";" + str(other)}
    assert suppress_optional_gdal_plugins(mixed) is True
    assert mixed["GDAL_DRIVER_PATH"] == str(other)

    custom = {"GDAL_DRIVER_PATH": str(other)}
    assert suppress_optional_gdal_plugins(custom) is False
    assert custom["GDAL_DRIVER_PATH"] == str(other)


def test_log_ignored_gdal_plugins_once(caplog, tmp_path, monkeypatch):
    plugins = tmp_path / "gdalplugins"
    plugins.mkdir()
    monkeypatch.setattr(tool_env, "qgis_gdal_plugins_dir", lambda root=None: plugins)
    tool_env._optional_gdal_plugins_ignored = False
    tool_env._optional_gdal_plugins_logged = False
    assert suppress_optional_gdal_plugins({}) is True
    with caplog.at_level(logging.INFO, logger="podkladarna"):
        log_ignored_gdal_plugins()
        log_ignored_gdal_plugins()
    assert caplog.messages.count(OPTIONAL_GDAL_PLUGINS_NOTE) == 1


def test_gis_env_disables_optional_plugins_and_keeps_real_errors(tmp_path):
    root = osgeo4w_root()
    if root is None:
        return
    env = gis_subprocess_env()
    assert env.get("GDAL_DRIVER_PATH") == "disable"
    assert "GDAL_SKIP" not in env
    assert "CPL_LOG" not in env
    assert env.get("PROJ_DATA")
    gdalinfo = which_tool("gdalinfo")
    if not gdalinfo:
        return
    formats = subprocess.run(
        [gdalinfo, "--formats"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert formats.returncode == 0
    assert "GTiff" in formats.stdout
    assert "ECW" not in formats.stdout
    assert "MrSID" not in formats.stdout
    assert "Can't load requested DLL" not in (formats.stderr or "")
    missing = tmp_path / "no-such-raster.tif"
    failed = subprocess.run(
        [gdalinfo, str(missing)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert failed.returncode != 0
    assert "No such file or directory" in (failed.stderr or "")
    assert "gdal_ECW_JP2ECW" not in (failed.stderr or "")
    assert "Can't load requested DLL" not in (failed.stderr or "")

