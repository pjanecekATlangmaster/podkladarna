from __future__ import annotations

from pathlib import Path

from app.tool_env import gis_subprocess_env, proj_data_dir, tool_status


def test_tool_status_keys():
    status = tool_status()
    assert set(status) == {"pdal", "ogr2ogr", "ogrinfo", "pullauta"}


def test_health_reports_tools(client):
    body = client.get("/api/health").json()
    assert "tools" in body
    assert "pipeline_ready" in body
    assert "pdal" in body["tools"]


def test_gis_env_uses_pyproj_data():
    proj = proj_data_dir()
    assert proj is not None
    assert (proj / "proj.db").exists()
    env = gis_subprocess_env()
    assert Path(env["PROJ_DATA"]) == proj
    assert Path(env["PROJ_LIB"]) == proj
    assert (Path(env["PROJ_DATA"]) / "proj.db").exists()


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

