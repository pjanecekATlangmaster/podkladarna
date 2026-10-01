from __future__ import annotations

from pathlib import Path

from app.tool_env import (
    gis_subprocess_env,
    osgeo4w_root,
    osgeo_proj_dir,
    osgeo_scripts_dir,
    proj_data_dir,
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

