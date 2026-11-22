"""Španělské regiony z mračna LiDAR (Navarra 2024 – levý horní roh, km; Baskicko 2017 – levý dolní roh, hm; Kastilie a León 2025 – podadresáře MTN50, relativní jména): seznam listů z výpisu adresářů, převod LAZ a profily zemí."""

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
    base = reference_es.ES_LAZ_REGIONS["es_nav"].listing
    expected = {(610, 4741): base + "las_cam_610-4741_2024_NAV_EPSG25830.laz",
                (611, 4741): base + "las_cam_611-4741_2024_NAV_EPSG25830.laz"}
    assert er.es_index("es_nav", fetch=lambda u: body) == expected
    assert er.es_index("es_nav", fetch=lambda u: pytest.fail("bez cache")) == expected


def test_index_empty_listing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    with pytest.raises(er.ElevationUnavailable):
        er.es_index("es_nav", fetch=lambda u: "<html></html>")
    assert not (tmp_path / "es_nav" / "index2.json").exists()


def test_index_outage_uses_stale_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    er.es_index("es_nav", fetch=lambda u: _listing(["las_cam_610-4741_2024_NAV_EPSG25830.laz"]))

    def down(u):
        raise OSError("503")

    assert set(er.es_index("es_nav", fetch=down, max_age_days=0)) == {(610, 4741)}


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


def test_basque_index_crawls_sheets_and_tiles_use_bottom_corner_in_hm(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    root = reference_es.ES_LAZ_REGIONS["es_pv"].listing
    pages = {root: '<A HREF="/lidar/DatosDescarga/LIDAR/LIDAR_2017_ETRS89/060/">060</A>'
                   '<A HREF="/lidar/DatosDescarga/LIDAR/">up</A>',
             root + "060/": '<A HREF="/lidar/DatosDescarga/LIDAR/LIDAR_2017_ETRS89/060/4655-47870.laz">a</A>'
                            '<A HREF="/lidar/DatosDescarga/LIDAR/LIDAR_2017_ETRS89/060/4660-47870.laz">b</A>'}
    index = er.es_index("es_pv", fetch=pages.__getitem__)
    assert index == {(4655, 47870): root + "060/4655-47870.laz", (4660, 47870): root + "060/4660-47870.laz"}
    # list 4655-47870 = x 465 500–466 000, y 4 787 000–4 787 500
    assert er.es_tiles("es_pv", (465600.0, 4787100.0, 466100.0, 4787400.0), index) == [(4655, 47870), (4660, 47870)]
    assert er.es_tiles("es_pv", (465600.0, 4787600.0, 465700.0, 4787700.0), index) == []


@pytest.mark.parametrize("code,calls,ok", [(503, 3, True), (404, 1, False)])
def test_laz_download_retries_transient_errors(tmp_path, monkeypatch, code, calls, ok):
    import io
    import urllib.error
    import urllib.request

    seen = []

    def urlopen(req, timeout=None):
        seen.append(1)
        if len(seen) < 3:
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
        return io.BytesIO(b"laz")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    dest = tmp_path / "t.laz"
    if ok:
        assert er._es_fetch_laz("https://example.invalid/t.laz", dest, "t", wait_s=0) == 3
    else:
        with pytest.raises(urllib.error.HTTPError):
            er._es_fetch_laz("https://example.invalid/t.laz", dest, "t", wait_s=0)
    assert len(seen) == calls


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
    monkeypatch.setattr(er, "es_index", lambda key: {(610, 4741): "u"})
    monkeypatch.setattr(er, "_es_tile", lambda key, e, n, url, log=None: tmp_path / f"{e}_{n}")
    monkeypatch.setattr(er, "run_cmd", lambda cmd, log=None: calls.append(cmd))
    monkeypatch.setattr(er, "_gdal_tool", lambda name: name)  # CI bez nástrojů GDAL
    monkeypatch.setattr(er, "_es_geoid_grid", lambda log=None: tmp_path / "geoid.tif")
    monkeypatch.setattr(er, "geoid_offsets", lambda grid, pts: [50.0])
    monkeypatch.setattr(er, "_buffered", lambda b, crs, m: (610100.0, 4740100.0, 610900.0, 4740900.0))
    res = er.PROVIDERS["es_nav"]((0, 0, 1, 1), tmp_path)
    assert res.dtm.name == "es_nav_dtm.vrt" and res.dsm.name == "es_nav_dsm.vrt" and len(calls) == 2
    assert res.vertical_offset(-1.64, 42.8) == 50.0  # elipsoidické výšky → geoid


def test_provider_outside_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(er, "es_index", lambda key: {})
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
    assert "ES-NC" in keys["es_ortho"].countries and not keys["es_ortho"].fatal
    assert [w[0] for w in online_templates.wms_layers_for("ES-NC")] == ["es_ortho"]


def test_basque_profile_and_probes():
    pv = countries.load_countries()["ES-PV"]
    assert pv.elevation == "es_pv" and pv.crs == "EPSG:25830" and not pv.cz_sources and pv.status == "beta"
    assert "geoEuskadi" in pv.citation and not reference_es.ES_LAZ_REGIONS["es_pv"].ellipsoidal
    assert countries.bbox_share((-2.95, 43.25, -2.91, 43.27), pv) == pytest.approx(1.0)  # Bilbao
    keys = {p.key: p for p in source_status.PROBES}
    assert keys["pv_elevation"].countries == ("ES-PV",) and keys["pv_elevation"].fatal
    assert "ES-PV" in keys["es_ortho"].countries
    assert [w[0] for w in online_templates.wms_layers_for("ES-PV")] == ["es_ortho"]


def test_castilla_index_relative_names_and_split_tiles(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    root = reference_es.ES_LAZ_REGIONS["es_cl"].listing
    f = "PNOA-LIDAR-2025-CyL_H05-{s}_{p}_{e}-{n}_NPC01.laz"
    a, b = f.format(s="0055", p="3-7", e=330, n=4786), f.format(s="0056", p="1-7", e=330, n=4786)
    c = f.format(s="0055", p="3-8", e=330, n=4782)
    pages = {
        root: '<a href="H50_0055/">H50_0055/</a><a href="H50_0056/">H50_0056/</a><a href="/cartografia/">up</a>',
        root + "H50_0055/": f'<a href="{a}">{a}</a><a href="{c}">{c}</a><a href="?C=N;O=D">x</a>',
        root + "H50_0056/": f'<a href="{b}">{b}</a>',
    }
    index = er.es_index("es_cl", fetch=pages.__getitem__)
    assert index[(330, 4782)] == root + "H50_0055/" + c
    assert index[(330, 4786)].split("\n") == [root + "H50_0055/" + a, root + "H50_0056/" + b]  # list ve dvou listech MTN50
    assert er.es_index("es_cl", fetch=lambda u: pytest.fail("bez cache")) == index
    # 330-4782 = x 330–331 km, y 4781–4782 km (N je horní okraj, ověřeno na skutečném listu)
    assert er.es_tiles("es_cl", (330100.0, 4781100.0, 330900.0, 4781900.0), index) == [(330, 4782)]


def test_castilla_tile_merges_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(er, "_es_fetch_laz", lambda url, dest, what: dest.write_bytes(url.encode()) or 100)
    nd = er.NODATA

    def grids(path, e, n, **kw):
        if path.read_bytes() == b"u1":  # západní část listu
            return np.array([[500.0, nd]], dtype="float32"), np.array([[510.0, nd]], dtype="float32")
        return np.array([[502.0, 600.0]], dtype="float32"), np.array([[505.0, 620.0]], dtype="float32")

    monkeypatch.setattr(er, "si_laz_grids", grids)
    written = {}
    monkeypatch.setattr(er, "_write_tif", lambda path, grid, left, top, crs: (written.__setitem__(path.name.split(".")[1], grid), path.write_bytes(b"t")))
    er._es_tile("es_cl", 330, 4782, "u1\nu2")
    assert written["dtm"].tolist() == [[501.0, 600.0]] and written["dsm"].tolist() == [[510.0, 620.0]]


def test_castilla_profile_probe_and_licence_note():
    from app import data_notes

    cl = countries.load_countries()["ES-CL"]
    assert cl.elevation == "es_cl" and cl.crs == "EPSG:25830" and not cl.cz_sources and cl.status == "beta"
    assert "Junta de Castilla y León" in cl.citation and not reference_es.ES_LAZ_REGIONS["es_cl"].ellipsoidal
    assert countries.bbox_share((-2.48, 41.75, -2.44, 41.78), cl) == pytest.approx(1.0)  # Soria
    assert "es_cl" in er.PROVIDERS
    keys = {p.key: p for p in source_status.PROBES}
    assert keys["cl_elevation"].countries == ("ES-CL",) and keys["cl_elevation"].fatal
    assert "ES-CL" in keys["es_ortho"].countries
    assert data_notes.note_keys(cl) == ["noncommercial"]
    assert "IGCYL-NC" in data_notes.text_lines(cl, "cs")[0] and "IGCYL-NC" in data_notes.text_lines(cl, "en")[0]
