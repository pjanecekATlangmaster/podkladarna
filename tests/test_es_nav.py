"""Navarra: listy mračna LiDAR 2024 (jméno = levý horní roh), seznam z výpisu adresáře, převod LAZ a profil země."""

import numpy as np
import pytest

from app import countries, settings, source_status
from app.pipeline import elevation_raster as er
from app.pipeline import online_templates, reference_es


def _listing(names):
    return "".join(f'<A HREF="/5_LIDAR/x/{n}">{n}</A><br>' for n in names)


def test_index_parses_listing_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    body = _listing(["las_cam_610-4741_2024_NAV_EPSG25830.laz", "las_cam_611-4741_2024_NAV_EPSG25830.laz", "x.pdf"])
    assert er.es_index("es_nav", fetch=lambda u: body) == {(610, 4741), (611, 4741)}
    assert er.es_index("es_nav", fetch=lambda u: pytest.fail("bez cache")) == {(610, 4741), (611, 4741)}


def test_index_empty_listing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    with pytest.raises(er.ElevationUnavailable):
        er.es_index("es_nav", fetch=lambda u: "<html></html>")
    assert not (tmp_path / "es_nav" / "index.json").exists()


def test_index_outage_uses_stale_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    er.es_index("es_nav", fetch=lambda u: _listing(["las_cam_610-4741_2024_NAV_EPSG25830.laz"]))

    def down(u):
        raise OSError("503")

    assert er.es_index("es_nav", fetch=down, max_age_days=0) == {(610, 4741)}


@pytest.mark.parametrize(
    "window,expected",
    [
        ((610100.0, 4740100.0, 610900.0, 4740900.0), [(610, 4741)]),  # uvnitř listu 610-4741 (y 4740–4741 km)
        ((609900.0, 4740100.0, 610100.0, 4740900.0), [(609, 4741), (610, 4741)]),
        ((610100.0, 4740900.0, 610200.0, 4741100.0), [(610, 4741), (610, 4742)]),
        ((700000.0, 4740100.0, 700100.0, 4740200.0), []),  # mimo index
    ],
)
def test_tiles_use_top_corner(window, expected):
    index = {(609, 4741), (610, 4741), (610, 4742)}
    assert er.es_tiles("es_nav", window, index) == expected


def test_laz_grids_tile_corner_and_classes(tmp_path):
    laspy = pytest.importorskip("laspy")
    header = laspy.LasHeader(point_format=6, version="1.4")
    header.offsets = [610000.0, 4740000.0, 0.0]
    header.scales = [0.01, 0.01, 0.01]
    las = laspy.LasData(header)
    # země (2) a vozovka (11) v buňce (0, 0) levého horního rohu, strom (5) nad ní, přesah sousedního listu (x < 610 km)
    las.x = np.array([610000.2, 610000.6, 610000.5, 609990.0])
    las.y = np.array([4740999.5, 4740999.5, 4740999.5, 4740999.5])
    las.z = np.array([500.0, 502.0, 520.0, 900.0])
    las.classification = np.array([2, 11, 5, 2], dtype=np.uint8)
    path = tmp_path / "t.laz"
    las.write(str(path))
    region = reference_es.ES_LAZ_REGIONS["es_nav"]
    dtm, dsm = er.si_laz_grids(path, 610, 4741, side_m=1000, left=610000.0, bottom=4740000.0,
                               ground=region.ground, surface=region.surface)
    assert dtm.shape == (1000, 1000)
    assert dtm[0, 0] == pytest.approx(501.0) and dsm[0, 0] == pytest.approx(520.0)
    assert (dtm[1:, :] == er.NODATA).all() and (dtm[0, 1:] == er.NODATA).all()  # přesah 609,99 km se nepočítá


def test_provider_builds_vrt_and_geoid_offset(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(er, "es_index", lambda key: {(610, 4741)})
    monkeypatch.setattr(er, "_es_tile", lambda key, e, n, log=None: tmp_path / f"{e}_{n}")
    monkeypatch.setattr(er, "run_cmd", lambda cmd, log=None: calls.append(cmd))
    monkeypatch.setattr(er, "_es_geoid_grid", lambda log=None: tmp_path / "geoid.tif")
    monkeypatch.setattr(er, "geoid_offsets", lambda grid, pts: [50.0])
    monkeypatch.setattr(er, "_buffered", lambda b, crs, m: (610100.0, 4740100.0, 610900.0, 4740900.0))
    res = er.PROVIDERS["es_nav"]((0, 0, 1, 1), tmp_path)
    assert res.dtm.name == "es_nav_dtm.vrt" and res.dsm.name == "es_nav_dsm.vrt" and len(calls) == 2
    assert res.vertical_offset(-1.64, 42.8) == 50.0  # elipsoidické výšky → geoid


def test_provider_outside_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(er, "es_index", lambda key: set())
    monkeypatch.setattr(er, "_buffered", lambda b, crs, m: (610100.0, 4740100.0, 610900.0, 4740900.0))
    with pytest.raises(er.ElevationUnavailable):
        er.PROVIDERS["es_nav"]((0, 0, 1, 1), tmp_path)


def test_navarra_profile_and_probes():
    nc = countries.load_countries()["ES-NC"]
    assert nc.elevation == "es_nav" and nc.crs == "EPSG:25830" and not nc.cz_sources and nc.status == "beta"
    assert "Gobierno de Navarra" in nc.citation
    pamplona = (-1.66, 42.80, -1.62, 42.83)
    assert countries.bbox_share(pamplona, nc) == pytest.approx(1.0)
    assert countries.bbox_share((-3.72, 40.40, -3.68, 40.43), nc) == 0.0  # Madrid
    keys = {p.key: p for p in source_status.PROBES}
    assert keys["nav_elevation"].countries == ("ES-NC",) and keys["nav_elevation"].fatal
    assert keys["es_ortho"].countries == ("ES-NC",) and not keys["es_ortho"].fatal
    assert [w[0] for w in online_templates.wms_layers_for("ES-NC")] == ["es_ortho"]
