"""Zahraniční build: beta země, profil zdrojů jobu a výšky z rastru."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import countries, i18n
from app.pipeline import country_profile


def _intl_site(monkeypatch):
    """Mezinárodní doména (všechny země): jen ta smí dostat beta zemi."""
    monkeypatch.setenv("SITES", "intl.example=en:paid:*")
    return i18n.sites()["intl.example"]


@pytest.fixture
def sk_beta(monkeypatch):
    """SK jako beta země: v configs je vypnuté (bez volně stažitelného DMP), mechanismus beta se zkouší na něm."""
    import dataclasses

    patched = dict(countries.load_countries())
    patched["SK"] = dataclasses.replace(patched["SK"], status="beta", elevation="sk_ugkk")  # bez dat na disku
    monkeypatch.setattr(countries, "load_countries", lambda: patched)
    monkeypatch.setattr(country_profile, "load_countries", lambda: patched)


@pytest.mark.parametrize(
    "beta, bbox, code",
    [
        ("SK", (17.09, 48.165, 17.10, 48.175), "SK"),  # Bratislava, Kamzík
        ("*", (19.95, 49.20, 19.97, 49.21), "SK"),  # Vysoké Tatry
        ("", (14.40, 50.08, 14.42, 50.09), "CZ"),  # Praha beze změny
    ],
)
def test_beta_country_allowed_with_flag(monkeypatch, sk_beta, beta, bbox, code):
    monkeypatch.setenv("COUNTRIES_BETA", beta)
    country, share = countries.check_bbox(bbox, _intl_site(monkeypatch))
    assert country.code == code and share > 0.99


def test_beta_country_hidden_without_flag(monkeypatch, sk_beta):
    monkeypatch.setenv("COUNTRIES_BETA", "")
    assert [c.code for c in countries.allowed_countries(None)] == ["CZ"]
    with pytest.raises(countries.CountryError):
        countries.check_bbox((17.09, 48.165, 17.10, 48.175))
    # NL je beta: jen s COUNTRIES_BETA (test B), na ostré se nenabízí.
    assert "NL" not in countries.offered_codes()
    monkeypatch.setenv("COUNTRIES_BETA", "*")
    assert "NL" in [c.code for c in countries.allowed_countries(_intl_site(monkeypatch))]


@pytest.mark.parametrize(
    "sites, host",
    [
        ("", None),  # test B bez SITES (pt.kibos.link)
        ("podkladarna.kibos.link=cs:free:CZ,map.ulvi.app=en:paid:*", "podkladarna.kibos.link"),
        ("podkladarna.kibos.link=cs:free:CZ,map.ulvi.app=en:paid:*", "neznama.domena"),  # Host mimo SITES
    ],
)
def test_beta_never_on_czech_site(monkeypatch, sk_beta, sites, host):
    """Zahraniční mapy jen na mezinárodní doméně, nikdy na české Podkladárně (Petr 2026-11-15)."""
    monkeypatch.setenv("COUNTRIES_BETA", "*")
    monkeypatch.setenv("SITES", sites)
    site = i18n.sites().get(host or "")
    assert [c.code for c in countries.allowed_countries(site)] == ["CZ"]
    assert countries.coverage_payload(site)["countries"][0]["code"] == "CZ"
    assert len(countries.coverage_payload(site)["countries"]) == 1
    with pytest.raises(countries.CountryError):
        countries.check_bbox((17.09, 48.165, 17.10, 48.175), site)  # Bratislava
    with pytest.raises(countries.CountryError):
        countries.check_bbox((17.09, 48.165, 17.10, 48.175), site, "SK")
    if sites:
        assert "NL" in countries.offered_codes()  # mezinárodní doména ji nabízí
        assert "NL" in [c.code for c in countries.allowed_countries(i18n.sites()["map.ulvi.app"])]
    else:
        assert "NL" not in countries.offered_codes()


@pytest.mark.parametrize("with_data", [False, True])
def test_sk_beta_only_with_local_data(tmp_path, monkeypatch, with_data):
    """Slovensko (výšky z disku) se nabízí jen na instanci, kde jsou GeoTIFFy ``dtm`` i ``dsm`` (Petr 2026-11-17)."""
    from app import settings

    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("COUNTRIES_BETA", "*")
    if with_data:
        for sub in ("dtm", "dsm"):
            (tmp_path / "cache/static/sk_ugkk" / sub).mkdir(parents=True)
            (tmp_path / "cache/static/sk_ugkk" / sub / "x.tif").write_bytes(b"")
    assert countries.load_countries()["SK"].status == "beta"
    assert ("SK" in [c.code for c in countries.allowed_countries(_intl_site(monkeypatch))]) is with_data


@pytest.mark.parametrize(
    "options, cz, elevation",
    [
        ({}, True, "cz_openzu"),  # starší joby bez země = Česko
        ({"country": "CZ"}, True, "cz_openzu"),
        ({"country": "sk"}, False, countries.load_countries()["SK"].elevation),
        ({"country": "NL"}, False, "nl_ahn"),
        ({"country": "CH"}, False, "ch_swisstopo"),
        ({"country": "AT"}, False, "at_bev"),
        ({"country": "DE"}, False, "de_berlin"),
        ({"country": "FR"}, False, "fr_lidarhd"),
        ({"country": "EE"}, False, "ee_maaamet"),
        ({"country": "IT"}, False, "it_trentino"),
        ({"country": "BE"}, False, "be_dhmv"),
        ({"country": "FI"}, False, "fi_mml"),
        ({"country": "LU"}, False, "lu_act"),
        ({"country": "DE-NW"}, False, "de_nrw"),
        ({"country": "PL"}, False, "pl_gugik"),
        ({"country": "GB-ENG"}, False, "gb_ea"),
        ({"country": "DE-BY"}, False, "de_by"),
        ({"country": "DE-ST"}, False, "de_st"),
        ({"country": "DK"}, False, "dk_dhm"),
        ({"country": "NO"}, False, "no_hoyde"),
        ({"country": "PT"}, False, "pt_dgt"),
        ({"country": "DE-MV"}, False, "de_mv"),
        ({"country": "DE-NI"}, False, "de_ni"),
        ({"country": "DE-BB"}, False, "de_bb"),
        ({"country": "DE-RP"}, False, "de_rp"),
        ({"country": "DE-BW"}, False, "de_bw"),
        ({"country": "DE-SN"}, False, "de_sn"),
        ({"country": "DE-HE"}, False, "de_he"),
        ({"country": "DE-SL"}, False, "de_sl"),
        ({"country": "DE-HH"}, False, "de_hh"),
        ({"country": "DE-HB"}, False, "de_hb"),
        ({"country": "XX"}, False, "none"),
    ],
)
def test_job_profile(options, cz, elevation):
    assert country_profile.uses_cz_sources(options) is cz
    assert country_profile.elevation_source(options) == elevation


def _write_tif(path: Path, values, origin, nodata=-9999.0, epsg_wkt=None):
    import numpy as np
    from osgeo import gdal

    from app.pipeline.crs_5514 import CRS_WKT

    arr = np.asarray(values, dtype="float32")
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((origin[0], 1.0, 0, origin[1], 0, -1.0))
    ds.SetProjection(epsg_wkt or CRS_WKT)
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(nodata)
    band.WriteArray(arr)
    ds = None


def test_raster_surfaces_from_local_tiles(tmp_path, monkeypatch):
    pytest.importorskip("osgeo.gdal_array")
    from app.pipeline import elevation_raster

    try:
        elevation_raster._gdal_tool("gdalwarp")
    except RuntimeError:
        pytest.skip("gdalwarp není k dispozici")
    import numpy as np

    monkeypatch.setattr(elevation_raster.settings, "DATA_ROOT", tmp_path)
    x0, y0 = -600000.0, -1160000.0  # Bratislava v S-JTSK
    dtm = np.full((40, 40), 200.0)
    dtm[10:14, 10:14] = -9999.0  # díra (budova) – CHM tam 0
    dsm = dtm.copy()
    dsm[20:30, 20:30] = 215.0  # porost 15 m
    dsm[10:14, 10:14] = 210.0
    _write_tif(tmp_path / "elev" / "dtm" / "a.tif", dtm, (x0, y0 + 40))
    _write_tif(tmp_path / "elev" / "dsm" / "a.tif", dsm, (x0, y0 + 40))
    work = tmp_path / "work"
    res = elevation_raster.prepare_raster_surfaces(
        work, (x0 + 5, y0 + 5, x0 + 35, y0 + 35), source="local:elev", resolution_m=1.0
    )
    chm = elevation_raster._read(res.chm)
    filled = elevation_raster._read(res.dem_filled)
    assert chm.shape == (30, 30)
    assert (filled > 150).all()  # díra vyplněná
    assert chm.max() == pytest.approx(15.0, abs=0.01)
    assert chm[5:9, 5:9].max() == 0.0  # pod dírou DTM žádný porost
    assert (work / "dem" / "dem_meta.json").is_file()


def test_raster_surfaces_unknown_source(tmp_path):
    from app.pipeline import elevation_raster

    with pytest.raises(elevation_raster.ElevationUnavailable):
        elevation_raster.prepare_raster_surfaces(tmp_path, (0, 0, 10, 10), source="none")


def test_raster_surfaces_without_dsm_degrade(tmp_path, monkeypatch):
    """Zdroj bez modelu povrchu (Ålandy bez latvusmalli) job neshodí: terén ano, povrch a CHM ne, značka a ⚠."""
    from app.pipeline import elevation_raster

    monkeypatch.setitem(
        elevation_raster.PROVIDERS, "test_dtm_only", lambda b, d, log=None: elevation_raster.SourceRasters(tmp_path / "dtm.tif")
    )

    def fake_warp(src, dest, *_a, **_k):
        dest.write_bytes(b"x")
        return dest

    monkeypatch.setattr(elevation_raster, "_warp_to_grid", fake_warp)
    monkeypatch.setattr(elevation_raster, "_fill", lambda src, dest, log=None: fake_warp(src, dest))
    monkeypatch.setattr(elevation_raster, "_valid_share", lambda _p: 1.0)
    lines: list[str] = []
    work = tmp_path / "work"
    res = elevation_raster.prepare_raster_surfaces(work, (0, 0, 10, 10), source="test_dtm_only", log=lines.append)
    assert res.dem_filled.is_file()
    assert res.dsm_raw is None and res.chm is None
    assert not (work / "dem" / "chm.tif").exists()
    assert elevation_raster.surface_missing(work)
    assert any(line.startswith("⚠") and "povrchu" in line for line in lines)


@pytest.mark.parametrize(
    "covers, expected",
    [
        (["forest"], 0),  # les bez výšky porostu bílý
        (["forest", "meadow"], 1),  # louka v lese je louka (401)
        (["grassland"], 1),
        (["heath"], 5),  # 403
        (["scrub"], 2),  # 406
        (["farmland"], 0),  # pole kreslí OSM vrstva 412, ne vegetace
    ],
)
def test_vegetation_from_osm_cover(tmp_path, monkeypatch, covers, expected):
    import json

    import numpy as np

    from app.pipeline import gdal_cli_raster, vegetation_chm

    (tmp_path / "dem").mkdir()
    (tmp_path / "dem" / "dem_filled.tif").write_bytes(b"x")
    (tmp_path / "osm_paths").mkdir()
    ring = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
    feats = [{"type": "Feature", "properties": {"cover": c}, "geometry": {"type": "Polygon", "coordinates": [ring]}}
             for c in covers]
    (tmp_path / "osm_paths" / "natural_cover.geojson").write_text(json.dumps({"features": feats}), encoding="utf-8")
    gt = (0.0, 1.0, 0.0, 10.0, 0.0, -1.0)
    monkeypatch.setattr(gdal_cli_raster, "read_float32_geotiff", lambda *_a, **_k: (np.zeros((10, 10), "float32"), gt, None))
    got = {}

    def fake_polygonize(classified, _gt, dest, **_k):
        got["cls"] = classified
        return dest

    monkeypatch.setattr(vegetation_chm, "polygonize_vegetation_classes", fake_polygonize)
    out = vegetation_chm.generate_vegetation_from_osm(tmp_path)
    if expected:
        assert out is not None and int(np.bincount(got["cls"].ravel()).argmax()) == expected
    else:
        assert out is None and "cls" not in got


def test_api_beta_country_without_sheets(client, monkeypatch, sk_beta):
    import app.main as main

    def _no_sheets(*_a, **_k):
        raise AssertionError("mimo Česko se listy SM5 nedotazují")

    monkeypatch.setenv("COUNTRIES_BETA", "SK")
    monkeypatch.setenv("SITES", "testserver=cs:free:*")  # beta jen na mezinárodní doméně
    monkeypatch.setattr(main, "query_sm5_sheets", _no_sheets)
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "enqueue", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "queue_position", lambda *_a, **_k: 0)
    bbox = "17.085,48.165,17.100,48.175"
    body = client.get("/api/sheets", params={"bbox": bbox}).json()
    assert body["country"] == "SK" and body["count"] == 0 and body["estimate_minutes"]
    assert body["needs_sheets"] is False and "SM5" not in body["label"]  # GUI nehlásí „mimo data“
    lic = body["licenses"]  # shrnutí zdrojů a licencí nad Spustit (stejné texty jako ZIP a /licence)
    assert lic["url"] == "/licence" and lic["citation"] and lic["lines"]
    r = client.post("/api/jobs", data={"preset_id": "sprint_2m", "output_mode": "png_zip", "name": "ba", "bbox": bbox})
    assert r.status_code == 200, r.text
    opts = r.json()["options"]
    assert opts["country"] == "SK" and opts["sm5_sheets"] == []


def test_ahn_coverage_urls_tiling():
    from app.pipeline.elevation_raster import AHN_MAX_SIDE_M, ahn_coverage_urls

    small = ahn_coverage_urls("dtm_05m", (117000.2, 480000.0, 118000.0, 481000.7))
    assert len(small) == 1
    assert "COVERAGEID=dtm_05m" in small[0] and "SUBSET=x(116980.0,118020.0)" in small[0]
    assert "SUBSET=y(479980.0,481021.0)" in small[0]
    big = ahn_coverage_urls("dsm_05m", (100000.0, 400000.0, 100000.0 + 2.5 * AHN_MAX_SIDE_M, 401000.0))
    assert len(big) == 3


def test_krovak_source_not_shifted(tmp_path, monkeypatch):
    """Rastr v oficiálním S-JTSK (bez TOWGS84, SK data) se na mřížku mapy nepřevádí přes datum."""
    pytest.importorskip("osgeo.gdal_array")
    from osgeo import osr

    from app.pipeline import elevation_raster

    try:
        elevation_raster._gdal_tool("gdalwarp")
    except RuntimeError:
        pytest.skip("gdalwarp není k dispozici")
    import numpy as np

    srs = osr.SpatialReference()
    srs.ImportFromProj4(
        "+proj=krovak +lat_0=49.5 +lon_0=24.83333333333333 +alpha=30.28813972222222 +k=0.9999 +x_0=0 +y_0=0 "
        "+ellps=bessel +units=m +no_defs"
    )
    monkeypatch.setattr(elevation_raster.settings, "DATA_ROOT", tmp_path)
    x0, y0 = -600000.0, -1160000.0
    dtm = np.tile(np.arange(40, dtype="float32"), (40, 1))  # výška = sloupec → posun je vidět
    for kind in ("dtm", "dsm"):
        _write_tif(tmp_path / "elev" / kind / "a.tif", dtm, (x0, y0 + 40), epsg_wkt=srs.ExportToWkt())
    res = elevation_raster.prepare_raster_surfaces(
        tmp_path / "work", (x0 + 10, y0 + 10, x0 + 30, y0 + 30), source="local:elev"
    )
    filled = elevation_raster._read(res.dem_filled)
    assert filled[5, 0] == pytest.approx(10.0, abs=0.6)


def test_sk_wcs_urls_and_geoid(tmp_path):
    pytest.importorskip("osgeo.gdal_array")
    import numpy as np
    from osgeo import gdal, osr

    from app.pipeline import elevation_raster as er

    urls = er.sk_wcs_urls((208000.3, 5341500.0, 210000.0, 5343500.0))
    assert len(urls) == 1 and "subset=E(207980,210020)" in urls[0] and "el__EL.GridCoverage" in urls[0]
    assert "subset=N(5341480,5343520)" in urls[0] and "e+" not in urls[0]  # ne vědecký zápis (GeoServer 400)
    assert len(er.sk_wcs_urls((200000.0, 5300000.0, 209000.0, 5301000.0))) == 3
    # Mřížka kvazigeoidu: lineární v délce → bilineární interpolace přesná.
    grid = tmp_path / "geoid.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(grid), 4, 3, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((16.0, 1.0, 0, 50.0, 0, -1.0))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4258)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(np.tile(np.array([40.0, 41.0, 42.0, 43.0], dtype="float32"), (3, 1)))
    ds = None
    assert er.geoid_offsets(grid, [(17.0, 48.5)])[0] == pytest.approx(40.5)  # mezi středy pixelů
    assert er.geoid_offsets(grid, [(17.5, 49.0)])[0] == pytest.approx(41.0)


def test_raster_surfaces_without_osgeo_and_vertical_offset(tmp_path, monkeypatch):
    """Lokální Windows Python bez ``osgeo``: vše přes GDAL CLI; elipsoidické výšky se srovnají kvazigeoidem."""
    pytest.importorskip("osgeo.gdal_array")
    import sys

    import numpy as np

    from app.pipeline import elevation_raster

    try:
        elevation_raster._gdal_tool("gdalwarp")
    except RuntimeError:
        pytest.skip("gdalwarp není k dispozici")
    x0, y0 = -600000.0, -1160000.0
    dtm = np.full((40, 40), 244.0)
    dsm = dtm.copy()
    dsm[20:30, 20:30] = 259.0
    _write_tif(tmp_path / "dtm.tif", dtm, (x0, y0 + 40))
    _write_tif(tmp_path / "dsm.tif", dsm, (x0, y0 + 40))
    monkeypatch.setitem(
        elevation_raster.PROVIDERS,
        "test_ell",
        lambda b, d, log=None: elevation_raster.SourceRasters(tmp_path / "dtm.tif", tmp_path / "dsm.tif", lambda lon, lat: 44.0),
    )
    monkeypatch.setitem(sys.modules, "osgeo", None)  # import osgeo → ImportError
    res = elevation_raster.prepare_raster_surfaces(
        tmp_path / "work", (x0 + 5, y0 + 5, x0 + 35, y0 + 35), source="test_ell", resolution_m=1.0
    )
    monkeypatch.delitem(sys.modules, "osgeo")
    filled = elevation_raster._read(res.dem_filled)
    chm = elevation_raster._read(res.chm)
    assert filled.mean() == pytest.approx(200.0, abs=0.01)
    assert chm.max() == pytest.approx(15.0, abs=0.01)


def test_process_country_gates_cuzk_orthophoto(tmp_path, monkeypatch):
    from app.pipeline import orthophoto

    import os

    os.environ.pop(country_profile.JOB_COUNTRY_ENV, None)
    assert country_profile.process_uses_cz_sources()
    country_profile.set_process_country({"country": "SK"})
    try:
        assert not country_profile.process_uses_cz_sources()
        monkeypatch.setattr(orthophoto, "_fetch_orthophoto", lambda *a, **k: pytest.fail("ČÚZK mimo Česko"))
        assert orthophoto.fetch_orthophoto(tmp_path, (0, 0, 10, 10)) is None
    finally:
        os.environ.pop(country_profile.JOB_COUNTRY_ENV, None)  # monkeypatch.delenv by „SK“ po testu vrátil


@pytest.mark.parametrize(
    "country, must, must_not",
    [
        ("CZ", ("Data: ČÚZK (DMR 5G", "Podkladárna · ČÚZK · OSM"), ("AHN",)),
        ("NL", ("Bron: AHN", "Bron: Kadaster", "Beeldmateriaal.nl", "OpenStreetMap contributors (ODbL)"), ("ČÚZK",)),
        ("CH", ("© swisstopo", "geodienste.ch", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "AHN")),
        ("AT", ("Datenquelle: BEV", "www.basemap.at", "Stadt Wien – data.wien.gv.at", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "swisstopo")),
        ("DE", ("Geoportal Berlin", "Datenlizenz Deutschland – Zero 2.0", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "BEV")),
        ("FR", ("© IGN", "data as downloaded on 20", "Ville de Paris", "Etalab 2.0", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "Berlin", "[data date]")),
        ("EE", ("Maa- ja Ruumiamet", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "IGN")),
        ("IT", ("Provincia autonoma di Trento", "CC BY 4.0", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "ETAK")),
        ("BE", ("Bron: Grootschalig Referentie Bestand Vlaanderen, Digitaal Vlaanderen", "Modellicentie Gratis Hergebruik", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "Trento")),
        ("FI", ("Maanmittauslaitos", "Suomen metsäkeskus", "Helsingin kaupunki", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "GRB")),
        ("LU", ("Administration du cadastre et de la topographie (ACT), CC0 1.0", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "Metsäkeskus")),
        ("DE-NW", ("Geobasis NRW (Datenlizenz Deutschland – Zero 2.0)", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "Berlin")),
        ("PL", ("Główny Urząd Geodezji i Kartografii (GUGiK), dane przetworzone", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "NRW")),
        ("DE-BY", ("Bayerische Vermessungsverwaltung – www.geodaten.bayern.de (CC BY 4.0)", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "NRW")),
        ("DE-ST", ("© GeoBasis-DE / LVermGeo ST (Datenlizenz Deutschland – Namensnennung – Version 2.0)", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "Bayern")),
        ("DK", ("Klimadatastyrelsen via Datafordeleren (CC BY 4.0)", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "Bayern")),
        ("NO", ("©Kartverket (CC BY 4.0, https://www.kartverket.no)", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "Klimadatastyrelsen")),
        ("DE-MV", ("© GeoBasis-DE/M-V 20", "(CC BY 4.0)", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "[data year]", "LVermGeo")),
        ("DE-NI", ("LGLN (20", "(CC BY 4.0)", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "[data year]", "M-V")),
        ("DE-BB", ("© GeoBasis-DE/LGB, dl-de/by-2-0", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "LGLN")),
        ("DE-RP", ("©GeoBasis-DE / LVermGeoRP (20", "dl-de/by-2-0", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "[data year]", "LGB")),
        ("DE-BW", ("Datenquelle: LGL, www.lgl-bw.de, dl-de/by-2-0", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "LVermGeoRP")),
        ("DE-SN", ("Quelle: GeoSN, dl-de/by-2-0", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "LGL")),
        ("DE-HE", ("HVBG", "dl-de/zero-2-0", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "GeoSN")),
        ("DE-SL", ("© GeoBasis DE/LVGL-SL", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "[data year]")),
        ("DE-HH", ("Landesbetrieb Geoinformation und Vermessung (LGV)", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "Orthophoto")),
        ("DE-HB", ("Landesamt GeoInformation Bremen (CC BY 4.0)", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "LGV")),
        ("GB-ENG", ("© Environment Agency copyright and/or database right 2022", "OpenStreetMap contributors (ODbL)"), ("ČÚZK", "GUGiK")),
    ],
)
def test_citation_by_country(country, must, must_not):
    """Citace zdrojů podle země výřezu, jak ji vyžadují licence místních dat (README a CO_JE_PODKLADARNA)."""
    from app.guide_text import zip_about_txt
    from app.pipeline.package_oom import _country_citation, oom_metadata, oom_readme

    meta = oom_metadata("sprint_2m", {"label": "Sprint", "scalefactor": 0.4}, {"country": country})
    readme = oom_readme(meta)
    about = zip_about_txt(**_country_citation(meta))
    data_part = readme[readme.index("Data"):]
    for text in must:
        assert text in data_part
    for text in must_not:
        assert text not in data_part and text not in about[about.index("Právní"):]
    assert meta["citation_short"] in about
    if country != "CZ":  # zahraniční citace jsou anglicky (Omapic), bez českých slov (dřív „AV kantonů“)
        from app.pipeline.country_profile import attribution_lines, citation

        texts = citation({"country": country}) + " ".join(attribution_lines({"country": country}))
        assert not set("ěřůĚŘŮ") & set(texts), texts
    # Mezinárodní výstup neodkazuje na náš GitHub, český ano.
    has_github = "github.com/pjanecek" in about
    assert has_github == (country == "CZ") and ("github.com/pjanecek" in readme) == (country == "CZ")


def test_job_crs_from_env_and_worker(monkeypatch):
    """Proces jobu v NL počítá v EPSG:28992 (proměnnou nastaví worker), web a Česko zůstávají v S-JTSK."""
    import subprocess
    import sys

    from app import worker
    from app.pipeline import crs_5514

    code = (
        "from app.pipeline import crs_5514 as c; from app.pipeline.fetch_openzu import crop_bounds_5514 as b;"
        "x0, y0, x1, y1 = b(4.88, 52.36, 4.90, 52.37, buffer_m=0);"
        "print(c.CRS_LABEL, c.CRS_EPSG, round(x0), round(y1))"
    )
    env = {**__import__("os").environ, crs_5514.JOB_CRS_ENV: "EPSG:28992"}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout.split()
    assert out[:2] == ["EPSG:28992", "28992"]
    assert 120000 < int(out[2]) < 122000 and 486000 < int(out[3]) < 488000  # Amsterdam v RD
    assert crs_5514.CRS_LABEL == "EPSG:5514"

    jobs = {"nl": {"options": {"country": "NL"}}, "cz": {"options": {}}}
    monkeypatch.setattr(worker.db, "get_job", lambda job_id: jobs[job_id])
    monkeypatch.setenv(crs_5514.JOB_CRS_ENV, "EPSG:28992")  # zbytek z jiného jobu se nedědí
    assert worker._job_env("nl")[crs_5514.JOB_CRS_ENV] == "EPSG:28992"
    assert crs_5514.JOB_CRS_ENV not in worker._job_env("cz")


@pytest.mark.parametrize(
    "country, bounds, keys, ortho, host, layer",
    [
        ("NL", (120000.0, 486000.0, 121000.0, 487000.0), {"online_nl_luchtfoto", "online_nl_kadaster", "online_nl_top10nl"},
         "online_nl_luchtfoto", "service.pdok.nl", "Actueel_orthoHR"),
        ("CH", (2600000.0, 1199000.0, 2601000.0, 1200000.0), {"online_ch_swissimage", "online_ch_av", "online_ch_landeskarte"},
         "online_ch_swissimage", "wms.geo.admin.ch", "ch.swisstopo.swissimage"),
        # Vídeň: WMS v nativním MGI (server převádí bez posunu data), okno výřezu jako VRT; výřez v souřadnicích testu (S-JTSK)
        ("AT", (-628500.0, -1268000.0, -627500.0, -1267000.0), {"online_at_fmzk", "online_at_cadastre"}, "online_at_fmzk",
         "data.wien.gv.at", "FMZKVERKEHR2OGD"),
        ("DE", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_de_truedop", "online_de_k5", "online_de_alkis"},
         "online_de_truedop", "gdi.berlin.de", "truedop_2024"),
        ("FR", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_fr_ortho", "online_fr_plan", "online_fr_cadastre"},
         "online_fr_ortho", "data.geopf.fr", "ORTHOIMAGERY.ORTHOPHOTOS"),
        ("EE", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_ee_ortho", "online_ee_basemap", "online_ee_cadastre"},
         "online_ee_ortho", "kaart.maaamet.ee", "EESTIFOTO"),
        ("IT", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_it_ortho", "online_it_ctp", "online_it_hillshade"},
         "online_it_ortho", "siat.provincia.tn.it", "ecw-rgb-2015"),
        ("BE", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_be_ortho", "online_be_grb", "online_be_cadastre"},
         "online_be_ortho", "geo.api.vlaanderen.be", "OMWRGB25VL"),
        ("FI", (-625000.0, -1200000.0, -624000.0, -1199000.0),
         {"online_fi_ortho", "online_fi_basemap", "online_fi_cadastre", "online_fi_hel_ortho", "online_fi_hel_basemap"},
         "online_fi_ortho", "tiles.kartat.kapsi.fi", "ortokuva"),
        ("LU", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_lu_ortho", "online_lu_topo", "online_lu_cadastre"},
         "online_lu_ortho", "wms.geoportail.lu", "ortho_latest"),
        ("DE-NW", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_de_nw_dop", "online_de_nw_abk", "online_de_nw_alkis"},
         "online_de_nw_dop", "www.wms.nrw.de", "nw_dop_rgb"),
        ("PL", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_pl_ortho", "online_pl_topo"},
         "online_pl_ortho", "mapy.geoportal.gov.pl", "Raster"),
        ("DE-BY", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_de_by_dop", "online_de_by_dok", "online_de_by_alkis"},
         "online_de_by_dop", "geoservices.bayern.de", "by_dop40c"),
        ("DE-ST", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_de_st_dop", "online_de_st_dtk", "online_de_st_alkis"},
         "online_de_st_dop", "www.geodatenportal.sachsen-anhalt.de", "lsa_lvermgeo_dop20_2"),
        ("DE-MV", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_de_mv_dop", "online_de_mv_dtk", "online_de_mv_alkis"},
         "online_de_mv_dop", "www.geodaten-mv.de", "mv_dop"),
        ("DE-NI", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_de_ni_dop", "online_de_ni_dtk"},
         "online_de_ni_dop", "opendata.lgln.niedersachsen.de", "ni_dop20"),
        ("DE-BB", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_de_bb_dop", "online_de_bb_alkis"},
         "online_de_bb_dop", "isk.geobasis-bb.de", "bebb_dop20c"),
        ("DE-RP", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_de_rp_dop", "online_de_rp_dtk"},
         "online_de_rp_dop", "geo4.service24.rlp.de", "rp_dop20"),
        ("DE-BW", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_de_bw_dop", "online_de_bw_alkis"},
         "online_de_bw_dop", "owsproxy.lgl-bw.de", "IMAGES_DOP_20_RGB"),
        ("DE-SN", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_de_sn_dop", "online_de_sn_alkis"},
         "online_de_sn_dop", "geodienste.sachsen.de", "sn_dop_020"),
        ("DE-HE", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_de_he_dop", "online_de_he_dtk", "online_de_he_alkis"},
         "online_de_he_dop", "gds-srv.hessen.de", "he_dop20_rgb"),
        ("DE-SL", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_de_sl_dop"},
         "online_de_sl_dop", "geoportal.saarland.de", "sl_dop20_rgb"),
        ("DE-HB", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_de_hb_dop"},
         "online_de_hb_dop", "geodienste.bremen.de", "dop10_2025_HB,dop10_2025_BHV"),
        ("SK", (-450000.0, -1250000.0, -449000.0, -1249000.0), {"online_sk_ortho", "online_sk_zbgis", "online_sk_cadastre"},
         "online_sk_ortho", "zbgisws.skgeodesy.sk", "3,2,1"),
        ("GB-ENG", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_gb_cadastre"},
         "online_gb_cadastre", "inspire.landregistry.gov.uk", "inspire:CP.CadastralParcel"),
        ("NO", (-625000.0, -1200000.0, -624000.0, -1199000.0), {"online_no_topo", "online_no_cadastre"},
         "online_no_cadastre", "wms.geonorge.no", "eiendomsgrense,teig"),
    ],
)
def test_foreign_online_templates(tmp_path, monkeypatch, country, bounds, keys, ortho, host, layer):
    from app.pipeline import online_templates
    from app.pipeline.online_templates import online_labels, write_online_templates

    def fake_warp(src, vrt, bounds, mpp):  # bez GDAL v CI: VRT jen odkazuje na zdroj
        vrt.write_text(src.name, encoding="utf-8")
        return True

    monkeypatch.setattr(online_templates, "osm_sjtsk_vrt", fake_warp)
    written = write_online_templates(bounds, tmp_path, country=country)
    assert keys <= set(written)
    assert not any(k.startswith("online_orthophoto") for k in written)  # ČÚZK mimo Česko ne
    path = written[ortho]
    if path.suffix == ".vrt":
        path = tmp_path / path.read_text(encoding="utf-8")
        assert f"<CRS>{online_templates.WMS_NATIVE_CRS[ortho[len('online_'):]]}</CRS>" in path.read_text(encoding="utf-8")
    xml = path.read_text(encoding="utf-8")
    assert host in xml and layer in xml
    assert set(written) <= set(online_labels())


# (země, podklady) – výchozí online šablony (.xml) dávají do README adresy WMS, i ty musí být anglicky.
@pytest.mark.parametrize(
    "country, refs",
    [("CZ", None), ("AT", None), ("CZ", ["online_orthophoto.xml"]), ("NL", ["online_nl_luchtfoto.xml"]),
     ("AT", ["online_at_ortho.vrt"]), ("BE", ["online_be_ortho.xml"])],
)
def test_zip_texts_follow_job_language(country, refs):
    """Zadání z anglické stránky (Omapic) má texty ZIPu anglicky, české zadání a starší joby česky."""
    from datetime import date

    from app.pipeline import sentinel2
    from app.pipeline.package_oom import oom_metadata, oom_readme, zip_about
    from app.pipeline.zip_texts_en import sentinel_info_en

    preset = {"label": "Lesní 1:10000 · 5 m", "scalefactor": 1}
    en = oom_metadata("les_10000", preset, {"country": country, "lang": "en"}, reference_layers=refs)
    name, about = zip_about(en)
    readme = oom_readme(en, sentinel_info="")
    assert name == "ABOUT_OMAPIC.txt" and en["lang"] == "en"
    czech = re.compile(r"[ěščřžůĚŠČŘŽŮ]|Podkladárn|Nejprve|Měřítko|Doporučený")
    allowed = ("ČÚZK", "Křovák", "Janeček", "Ruumiamet", "Straßen", "husté_vrstevnice")
    for text in (about, readme):
        scrub = text
        for word in allowed:
            scrub = scrub.replace(word, "")
        assert not czech.search(scrub), czech.search(scrub)
        assert "github.com" not in text
    assert "Forest 1:10000" in readme and "Base map: Omapic" in readme and "Base map: Omapic" in about
    assert ("(layer " in readme) is bool(refs)
    assert en["label"].startswith("Forest") and ("hillshade_source" in en) is (country == "CZ")
    assert ("map_layers" in en) is (country == "CZ")
    cs = oom_metadata("les_10000", preset, {"country": country}, reference_layers=refs)
    assert zip_about(cs)[0] == "CO_JE_PODKLADARNA.txt" and "Doporučený postup" in oom_readme(cs)
    scene = sentinel2.Scene("S2B_X", date(2026, 9, 1), 3.5, {}, 0.0)
    info = sentinel_info_en(sentinel2.info_text([scene]))
    assert "image date 2026-09-01" in info and "cloud cover of the whole tile 3.5 %" in info and "2026" in info



def test_dtm_summary_lines_in_english():
    from app.pipeline.zip_texts_en import dtm_lines_en, dtm_readme_en

    summary = {
        "state": "weak", "lines": ["Digitální technická mapa …", "DTM pokrývá jen …"], "warn_lines": ["DTM pokrývá jen …"],
        "maps": {"sprint": True, "les": False}, "counts": {"ploty": 3, "budovy": 5}, "missing": ["Lochkov"], "units_total": 2,
    }
    parts = dtm_lines_en(summary)
    assert parts["lines"][0] == "ČÚZK Digital Technical Map (DTM): used for sprint (fences 3, buildings 5)."
    assert parts["warn_lines"] == [parts["lines"][1]] and "1 of 2 municipalities" in parts["lines"][1]
    assert "WARNING: DTM covers only" in dtm_readme_en(summary)
    failed = dtm_lines_en({"state": "failed", "lines": ["x"], "notice_lines": ["x"]})
    assert failed["notice_lines"] == failed["lines"] and "unavailable" in failed["lines"][0]


def _cli_tif(path: Path, values, origin, *, tmp: Path) -> None:
    """Dlaždicovaný LZW GeoTIFF v S-JTSK přes GDAL CLI (jako balíky ÚGKK: bloky 128, přehledy)."""
    import subprocess

    import numpy as np

    from app.pipeline import elevation_raster
    from app.pipeline.crs_5514 import CRS_WKT

    arr = np.asarray(values, dtype="float32")
    asc = tmp / (path.stem + ".asc")
    header = (f"ncols {arr.shape[1]}\nnrows {arr.shape[0]}\nxllcorner {origin[0]}\n"
              f"yllcorner {origin[1] - arr.shape[0]}\ncellsize 1\nNODATA_value -9999\n")
    asc.write_text(header + "\n".join(" ".join(f"{v:.2f}" for v in row) for row in arr) + "\n", encoding="utf-8")
    wkt = tmp / "crs.wkt"
    wkt.write_text(CRS_WKT, encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([elevation_raster._gdal_tool("gdal_translate"), "-q", "-ot", "Float32", "-a_srs", str(wkt),
                    "-co", "TILED=YES", "-co", "BLOCKXSIZE=128", "-co", "BLOCKYSIZE=128", "-co", "COMPRESS=LZW",
                    str(asc), str(path)], check=True)
    subprocess.run([elevation_raster._gdal_tool("gdaladdo"), "-q", "-ro", str(path), "2", "4"], check=True)


@pytest.mark.parametrize("dsm_shift, ok", [(0.0, True), (40.0, False)])  # 40 m ≈ elipsoidické výšky proti Bpv
def test_sk_local_elevation_check(tmp_path, monkeypatch, dsm_shift, ok):
    """``sk_ugkk_local`` čte terén i povrch z ``cache/static/sk_ugkk``; kontrola odhalí posun výšek."""
    from app.pipeline import elevation_raster

    try:
        for tool in ("gdal_translate", "gdaladdo", "gdalwarp", "gdalbuildvrt", "gdalinfo"):
            elevation_raster._gdal_tool(tool)
    except RuntimeError:
        pytest.skip("GDAL CLI není k dispozici")
    import numpy as np
    from pyproj import Transformer

    from app import local_elevation_check, settings
    from app.pipeline.crs_5514 import CRS_WKT

    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path)
    x0, y0 = -420000.0, -1230000.0  # střed Slovenska v S-JTSK
    yy, xx = np.mgrid[0:400, 0:400]
    dtm = 400.0 + 0.05 * xx + 0.02 * yy
    dsm = dtm.copy()
    dsm[100:250, 150:300] += 18.0  # les
    base = tmp_path / "cache" / "static" / "sk_ugkk"
    _cli_tif(base / "dtm" / "dmr5_jtsk03.tif", dtm, (x0, y0 + 400), tmp=tmp_path)
    _cli_tif(base / "dsm" / "dmp1_jtsk03.tif", dsm + dsm_shift, (x0, y0 + 400), tmp=tmp_path)
    lon, lat = Transformer.from_crs(CRS_WKT, "EPSG:4326", always_xy=True).transform(x0 + 200, y0 + 200)
    lines: list[str] = []
    assert local_elevation_check.run("sk_ugkk_local", lon, lat, 200.0, out=lines.append) is ok, lines
    assert any("bloky 128×128" in line for line in lines)
    if ok:
        dsm_hole = np.full_like(dsm, -9999.0)  # povrch bez dat (okraj pokrytí): hláška, ne výjimka
        _cli_tif(base / "dtm" / "dmr5_jtsk03.tif", dsm_hole, (x0, y0 + 400), tmp=tmp_path)
        assert local_elevation_check.run("sk_ugkk_local", lon, lat, 200.0, out=lines.append) is False
        assert "nejde připravit" in lines[-1]
        _cli_tif(base / "dtm" / "dmr5_jtsk03.tif", dtm, (x0, y0 + 400), tmp=tmp_path)
        res = elevation_raster.prepare_raster_surfaces(
            tmp_path / "work", (x0 + 100, y0 + 100, x0 + 300, y0 + 300), source="sk_ugkk_local"
        )
        chm = elevation_raster._read(res.chm)
        assert chm.max() == pytest.approx(18.0, abs=0.1)
        assert 0.3 < float((chm > 3).mean()) < 0.7


def test_about_en_has_project_url(monkeypatch):
    """ABOUT_OMAPIC.txt nese adresu webu, ať se ví, odkud ZIP je (Petr 2026-11-19)."""
    from app.pipeline.package_oom import oom_metadata, zip_about
    monkeypatch.setenv("SITES", "podkladarna.kibos.link=cs:free:CZ,omapic.example=en:paid:*")
    meta = oom_metadata("les_10000", {"label": "Lesní 1:10000 · 5 m", "scalefactor": 1}, {"country": "NL", "lang": "en", "site": "omapic.example"})
    _, about = zip_about(meta)
    assert "Made with Omapic: https://omapic.example" in about and "Base map: Omapic (omapic.example)" in about
    meta.pop("site_url")
    assert "https://omapic.example" in zip_about(meta)[1]  # starší metadata: první mezinárodní doména


@pytest.mark.parametrize("site, github", [("omapic.example", False), ("podkladarna.kibos.link", True), ("", True)])
def test_czech_zip_texts_follow_domain(monkeypatch, site, github):
    """Česky zadaný výřez v Česku: na mezinárodní doméně ZIP pod značkou Omapic (bez GitHubu, s adresou webu,
    Petr 2026-11-19), česká doména beze změny."""
    from app.pipeline.package_oom import oom_metadata, oom_readme, zip_about

    monkeypatch.setenv("SITES", "podkladarna.kibos.link=cs:free:CZ,omapic.example=en:paid:*")
    preset = {"label": "Lesní 1:10000 · 5 m", "scalefactor": 1}
    meta = oom_metadata("les_10000", preset, {"country": "CZ", "lang": "cs", "site": site})
    name, about = zip_about(meta)
    readme = oom_readme(meta)
    assert ("github.com" in about) is github and ("github.com" in readme) is github
    if github:
        assert name == "CO_JE_PODKLADARNA.txt"
        assert "https://podkladarna.kibos.link/podpora" in about and "Podklad: Podkladárna · ČÚZK" in about
        assert "Podkladárn" in readme and "Omapic" not in about + readme
    else:
        assert name == "CO_JE_OMAPIC.txt"
        assert "Vytvořeno v Omapicu: https://omapic.example" in about
        assert "Podklad: Omapic (omapic.example) · ČÚZK" in about and "Podklad: Omapic (omapic.example) · ČÚZK" in readme
        assert "CO_JE_OMAPIC.txt" in readme
        assert "Podkladárn" not in about + readme + meta["citation"] and "kibos" not in about + readme
