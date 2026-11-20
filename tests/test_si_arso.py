"""Slovinsko: mračno LiDAR ARSO po listech 1 km – listy okna, blok z HEAD s cache, převod na terén a povrch, profil."""

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
    assert si.elevation == "si_arso" and si.crs == "EPSG:3794" and not si.cz_sources and si.technical_map == "none"
    assert countries.elevation_ready(si) and "ARSO" in si.citation and si.data_notes == ("old",)
    assert si.green == "leafoff"  # zimní mračno: bez zrnitosti, nízký porost v lese OSM bílý
    probe = next(p for p in source_status.PROBES if p.key == "si_elevation")
    assert probe.countries == ("SI",) and probe.fatal and probe.method == "HEAD"
    assert probe.urls == (reference_si.SI_ELEVATION_PROBE,)
