"""Slovinsko: CLSS 2023–2025 (index z WFS, části listu, záloha ARSO) a mračno ARSO po listech 1 km, profil."""

import json

import numpy as np
import pytest

from app import countries, settings, source_status
from app.pipeline import elevation_raster, reference_si


def test_tiles_cover_window():
    assert elevation_raster.si_tiles((461500.0, 100200.0, 463000.0, 101000.0)) == [(461, 100), (462, 100)]


def test_block_lookup_prefers_neighbours_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    (tmp_path / "si_arso").mkdir()
    (tmp_path / "si_arso" / "blocks.json").write_text(json.dumps({"461_101": "b_35"}))
    asked = []

    def status(url):
        asked.append(url)
        return 200 if "/b_35/" in url else 404

    assert elevation_raster.si_tile_block(462, 101, status=status) == "b_35"
    assert asked == [reference_si.gkot_url("b_35", 462, 101)]  # soused napověděl blok, jediný dotaz
    assert elevation_raster.si_tile_block(462, 101, status=lambda u: pytest.fail("bez dotazu")) == "b_35"
    assert elevation_raster.si_tile_block(380, 50, status=lambda u: 404) is None  # mimo pokrytí, uloží se
    assert json.loads((tmp_path / "si_arso" / "blocks.json").read_text())["380_50"] == ""
    with pytest.raises(RuntimeError):  # výpadek serveru se neukládá jako „list neexistuje“
        elevation_raster.si_tile_block(381, 50, status=lambda u: None)


def test_laz_grids_ground_mean_and_surface_max(tmp_path):
    laspy = pytest.importorskip("laspy")
    header = laspy.LasHeader(point_format=1, version="1.2")
    header.scales = [0.01, 0.01, 0.01]
    header.offsets = [462000, 101000, 0]
    las = laspy.LasData(header)
    las.x = np.array([462000.2, 462000.7, 462000.5, 462000.5, 462000.5, 462001.5])
    las.y = np.array([101999.5, 101999.5, 101999.5, 101999.5, 101999.5, 101999.5])
    las.z = np.array([300.0, 302.0, 320.0, 340.0, 999.0, 305.0])
    las.classification = np.array([2, 2, 5, 6, 7, 1], dtype=np.uint8)
    path = tmp_path / "t.laz"
    las.write(str(path))
    dtm, dsm = elevation_raster.si_laz_grids(path, 462, 101)
    assert dtm.shape == (1000, 1000) and dtm[0, 0] == pytest.approx(301.0)  # průměr bodů země
    assert dsm[0, 0] == pytest.approx(340.0)  # nejvyšší bod tříd 2–6, šum 7 ne
    assert dtm[0, 1] == elevation_raster.NODATA and dsm[0, 1] == elevation_raster.NODATA  # nezařazený bod 1 ne


def test_slovenia_profile_and_probe():
    si = countries.load_countries()["SI"]
    assert si.elevation == "si_clss" and si.crs == "EPSG:3794" and not si.cz_sources and si.technical_map == "none"
    assert countries.elevation_ready(si) and "GURS" in si.citation and si.elevation_years == "2023–2025"
    assert si.data_notes == ()  # data 2023–2025 nejsou stará
    assert si.green == "leafoff"  # snímkování jaro / podzim: bez zrnitosti, nízký porost v lese OSM bílý
    probes = {p.key: p for p in source_status.PROBES if "SI" in p.countries}
    assert probes["si_elevation"].urls == (reference_si.SI_CLSS_PROBE,)
    assert probes["si_arso_elevation"].urls == (reference_si.SI_ELEVATION_PROBE,)
    assert not any(p.fatal for p in probes.values())  # jeden zdroj zálohuje druhý, job se kvůli výpadku neodmítá


def test_clss_index_from_wfs_and_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    window = (472100.0, 87100.0, 473900.0, 87900.0)  # listy 472_87 a 473_87
    part = {"ti_name": "473_87", "orig_ime": "05-ljubljana", "path_dmr": "clss/raw/05-ljubljana/zls/dmr/DMR_473_87.laz",
            "path_dmp": "clss/raw/05-ljubljana/zls/dmp/DMP_473_87.laz", "datum_za_1": "06.05.2023"}
    aneks = dict(part, orig_ime="13-ljubljana-aneks", path_dmr="clss/raw/13-ljubljana-aneks/zls/dmr/DMR_473_87.laz",
                 path_dmp="clss/raw/13-ljubljana-aneks/zls/dmp/DMP_473_87.laz")
    asked = []

    def fetch(url):
        asked.append(url)
        return {"features": [{"properties": part}, {"properties": aneks}, {"properties": dict(part, ti_name="999_9")}]}

    index = elevation_raster.si_clss_index(window, fetch=fetch)
    assert index["472_87"] == []  # mimo CLSS
    assert [p["dmr"] for p in index["473_87"]] == [part["path_dmr"], aneks["path_dmr"]]  # list ve dvou blocích
    assert "bbox=472100%2C87100%2C473900%2C87900%2CEPSG%3A3794" in asked[0]
    assert elevation_raster.si_clss_index(window, fetch=lambda u: pytest.fail("z cache")) == index


def test_clss_file_url_rejects_foreign_paths():
    assert reference_si.clss_file_url("clss/raw/09-celje/zls/dmr/DMR_484_104.laz") == (
        "https://assets.flycom.si/clss/raw/09-celje/zls/dmr/DMR_484_104.laz")
    for bad in ("../etc/passwd", "https://evil/x.laz", "clss/raw/../../x"):
        with pytest.raises(ValueError):
            reference_si.clss_file_url(bad)


def test_clss_merge_parts_mean_terrain_max_surface():
    nd = elevation_raster.NODATA
    a = np.array([[300.0, nd], [nd, nd]], dtype="float32")
    b = np.array([[302.0, 310.0], [nd, nd]], dtype="float32")
    dtm = elevation_raster.si_clss_merge([a, b], how="mean")
    dsm = elevation_raster.si_clss_merge([a, b], how="max")
    assert dtm[0, 0] == pytest.approx(301.0) and dtm[0, 1] == pytest.approx(310.0) and dtm[1, 1] == nd
    assert dsm[0, 0] == pytest.approx(302.0) and dsm[1, 0] == nd


def test_clss_outage_falls_back_to_arso(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)

    def down(window, **kw):
        raise OSError("connection refused")

    sentinel = object()
    monkeypatch.setattr(elevation_raster, "si_clss_index", down)
    monkeypatch.setattr(elevation_raster, "si_arso_provider", lambda bounds, out_dir, log=None: sentinel)
    lines = []
    got = elevation_raster.si_clss_provider((14.5, 46.05, 14.51, 46.06), tmp_path, log=lines.append)
    assert got is sentinel and "ARSO" in lines[0]


def test_clss_empty_index_not_cached_and_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    window = (472100.0, 87100.0, 472900.0, 87900.0)
    assert elevation_raster.si_clss_index(window, fetch=lambda u: {"features": []}) == {"472_87": []}
    assert not (tmp_path / "si_clss" / elevation_raster.SI_CLSS_INDEX_FILE).exists()  # prázdná odpověď se necachuje
    sentinel = object()
    monkeypatch.setattr(elevation_raster, "si_clss_index", lambda window, **kw: {})
    monkeypatch.setattr(elevation_raster, "si_arso_provider", lambda bounds, out_dir, log=None: sentinel)
    lines = []
    assert elevation_raster.si_clss_provider((14.5, 46.05, 14.51, 46.06), tmp_path, log=lines.append) is sentinel
    assert "ARSO" in lines[0]
