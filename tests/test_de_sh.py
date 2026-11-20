"""Šlesvicko-Holštýnsko: index dlaždic z GeoJSON hromadného stahování, DGM1 z XYZ, bDOM20 → 1 m, profil země."""

import json

import pytest

from app import countries, source_status
from app.pipeline import elevation_raster as er
from app.pipeline.reference_de_states import elevation_probe

DL = "https://geodaten.schleswig-holstein.de/gaialight-sh/_apps/dladownload/massen.php"


def _index(*names):
    return json.dumps({"features": [{"properties": {"link_data": f"{DL}?file={n}&id=2"}} for n in names]}).encode()


def test_parse_index_keeps_only_valid_tiles():
    data = json.loads(_index("dgm1_32_580_5980_1_sh_2024.xyz", "bdom20nc_32_581_5980_1_sh_2023.tif", "x.pdf"))
    data["features"].append({"properties": {"link_data": "https://evil.example/massen.php?file=dgm1_32_1_1_1_sh_2024.xyz"}})
    assert er.parse_sh_index(data) == {"580_5980": f"{DL}?file=dgm1_32_580_5980_1_sh_2024.xyz&id=2",
                                        "581_5980": f"{DL}?file=bdom20nc_32_581_5980_1_sh_2023.tif&id=2"}


def test_index_cache_and_outage(tmp_path, monkeypatch):
    monkeypatch.setattr(er.settings, "CACHE_DIR", tmp_path)
    assert set(er.sh_index("de_sh", "dtm", fetch=lambda u, m: _index("dgm1_32_580_5980_1_sh_2024.xyz"))) == {"580_5980"}
    assert set(er.sh_index("de_sh", "dtm", fetch=lambda u, m: pytest.fail("bez cache"))) == {"580_5980"}

    def down(u, m):
        raise OSError("503")

    monkeypatch.setattr(er, "SH_INDEX_MAX_AGE_S", 0)
    assert set(er.sh_index("de_sh", "dtm", fetch=down)) == {"580_5980"}  # prošlý index při výpadku
    with pytest.raises(er.ElevationUnavailable):
        er.sh_index("de_sh", "dsm", fetch=down)


def test_tile_converts_xyz_and_warps_bdom_to_tile(tmp_path, monkeypatch):
    gdal = pytest.importorskip("osgeo.gdal")
    monkeypatch.setattr(er.settings, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(er, "sh_index", lambda key, kind, log=None: {"580_5980": f"u-{kind}"})
    monkeypatch.setattr(er, "_sh_fetch", lambda url, m: b"580000.50 5980999.50 39.39\r\n580001.50 5980999.50 39.40\r\n"
                                                        b"580000.50 5980998.50 39.30\r\n580001.50 5980998.50 39.31\r\n")
    monkeypatch.setattr(er, "_download_retry", lambda url, dest, log=None: dest.write_bytes(b"bdom") or dest)
    cmds = []

    def warp(cmd, log=None):
        cmds.append(cmd)
        open(cmd[-1], "wb").write(b"dsm")

    monkeypatch.setattr(er, "run_cmd", warp)
    monkeypatch.setattr(er, "_gdal_tool", lambda name: name)  # CI bez nástrojů GDAL
    folder = er._sh_tile("de_sh", 580, 5980)
    assert (folder / ".complete").is_file() and (folder / "dsm.tif").read_bytes() == b"dsm"
    ds = gdal.Open(str(folder / "dtm.tif"))
    assert ds.GetGeoTransform()[0] == 580000.0 and ds.GetGeoTransform()[3] == 5981000.0
    cmd = cmds[0]
    assert cmd[cmd.index("-r") + 1] == "max" and cmd[cmd.index("-te") + 1:cmd.index("-te") + 5] == [
        "580000", "5980000", "581000", "5981000"]
    assert [p.name for p in folder.iterdir() if p.name.startswith(".") and p.name not in (".complete", ".last_used")] == []
    assert er._sh_tile("de_sh", 580, 5980) == folder  # z cache bez stahování


def test_tile_without_bdom_after_failure_stays_incomplete(tmp_path, monkeypatch):
    pytest.importorskip("osgeo.gdal")
    monkeypatch.setattr(er.settings, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(er, "sh_index", lambda key, kind, log=None: {"580_5980": f"u-{kind}"})
    monkeypatch.setattr(er, "_sh_fetch", lambda url, m: b"580000.50 5980999.50 39.39\n580001.50 5980999.50 39.40\n")

    def fail(url, dest, log=None):
        raise OSError("503")

    monkeypatch.setattr(er, "_download_retry", fail)
    issues = []
    monkeypatch.setattr(er, "log_issue", lambda log, msg: issues.append(msg))
    folder = er._sh_tile("de_sh", 580, 5980)
    assert (folder / "dtm.tif").is_file() and not (folder / "dsm.tif").exists()
    assert not (folder / ".complete").exists() and "bDOM" in issues[0]  # příští job povrch zkusí znovu


def test_tile_outside_index_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(er.settings, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(er, "sh_index", lambda key, kind, log=None: {})
    assert er._sh_tile("de_sh", 1, 1) is None


def test_schleswig_holstein_profile_and_probes():
    sh = countries.load_countries()["DE-SH"]
    assert sh.elevation == "de_sh" and sh.crs == "EPSG:25832" and not sh.cz_sources and sh.status == "beta"
    assert "LVermGeo SH" in sh.citation
    assert countries.bbox_share((9.95, 54.05, 10.05, 54.10), sh) == pytest.approx(1.0)  # Neumünster
    assert countries.bbox_share((9.95, 53.53, 10.05, 53.58), sh) < 0.2  # Hamburk
    keys = {p.key: p for p in source_status.PROBES}
    assert keys["de_sh_elevation"].countries == ("DE-SH",) and keys["de_sh_elevation"].method == "HEAD"
    assert "DGM1_SH__Massendownload.geojson" in elevation_probe("de_sh")
