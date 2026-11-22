"""Švédsko: výšky Lantmäteriet – nejnovější list ze STAC, povrch z mračna COPC, přihlášení jen z prostředí, profil."""

import base64
import types

import numpy as np
import pytest

from app import countries, source_status
from app.pipeline import elevation_raster, reference_se

DL = "https://dl1.lantmateriet.se/hojd/data/pointcloud/sls"


def _item(item_id, when, href):
    return {"id": item_id, "properties": {"datetime": when}, "assets": {"data": {"href": href}}}


def test_latest_asset_per_tile_and_only_lantmateriet_host():
    items = [
        _item("21c031-65775_6700_25", "2021-03-23T00:00:00Z", f"{DL}/21c031/a.copc.laz"),
        _item("23b002-65775_6700_25", "2023-05-01T00:00:00Z", f"{DL}/23b002/b.copc.laz"),  # novější překryv
        _item("21c031-65750_6700_25", "2021-03-23T00:00:00Z", f"{DL}/21c031/c.copc.laz"),
        _item("21c031-65800_6700_25", "2024-01-01T00:00:00Z", "https://evil.example/d.copc.laz"),
        _item("657_67", "2022-10-10T00:00:00Z", "http://dl1.lantmateriet.se/x.tif"),  # bez HTTPS
    ]
    assert elevation_raster.se_latest_assets(items) == [
        (f"{DL}/21c031/c.copc.laz", "2021-03-23T00:00:00Z"),
        (f"{DL}/23b002/b.copc.laz", "2023-05-01T00:00:00Z"),
    ]


def test_stac_search_follows_next_on_same_server():
    pages = {
        "first": {"features": [{"id": "a"}], "links": [{"rel": "next", "href": f"{reference_se.SE_STAC}/search?page=2"}]},
        "second": {"features": [{"id": "b"}], "links": [{"rel": "next", "href": "https://evil.example/p3"}]},
    }
    seen = []

    def fetch(url):
        seen.append(url)
        return pages["first" if len(seen) == 1 else "second"]

    items = elevation_raster.se_stac_items("dtm-cog", (18.0, 59.3, 18.1, 59.4), fetch=fetch)
    assert [i["id"] for i in items] == ["a", "b"] and len(seen) == 2
    assert seen[0].startswith(f"{reference_se.SE_STAC}/search?collections=dtm-cog&bbox=18.000000,59.300000")


def test_surface_max_without_noise_and_gap_fill():
    s = elevation_raster._SeSurface((100.0, 200.0, 105.0, 203.0))
    assert (s.width, s.height) == (5, 3)
    n = s.add(np.array([100.5, 100.6, 100.5, 104.5]), np.array([202.5, 202.5, 202.5, 200.5]),
              np.array([10.0, 12.0, 99.0, 5.0]), np.array([2, 1, 18, 2]))
    assert n == 3  # šum 18 se nepočítá
    g = s.grid()
    assert g[0, 0] == pytest.approx(12.0) and g[2, 4] == pytest.approx(5.0)
    assert g[0, 1] == pytest.approx(12.0)  # prázdná buňka = nejvyšší soused 3 × 3
    assert g[0, 3] == elevation_raster.NODATA  # bez bodů v okolí


def test_copc_window_feeds_surface(monkeypatch):
    class FakeFile:
        bytes_read = 1234

        def __init__(self, url):
            self.url = url

    class FakeReader:
        def __init__(self, fh):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def query(self, bounds):
            assert list(bounds.mins) == [100.0, 200.0]
            return types.SimpleNamespace(x=np.array([100.5]), y=np.array([202.5]), z=np.array([7.0]),
                                         classification=np.array([1]))

    pytest.importorskip("laspy")
    monkeypatch.setattr(elevation_raster, "_SeRangeFile", FakeFile)
    s = elevation_raster._SeSurface((100.0, 200.0, 102.0, 203.0))
    assert elevation_raster._se_copc_window("u", (100.0, 200.0, 102.0, 203.0), s, reader_factory=FakeReader) == (1, 1234)
    assert s.grid()[0, 0] == pytest.approx(7.0)


def test_credentials_only_in_header_for_data_host(monkeypatch):
    monkeypatch.delenv(reference_se.SE_USER_ENV, raising=False)
    monkeypatch.delenv(reference_se.SE_PASSWORD_ENV, raising=False)
    assert reference_se.auth_headers(reference_se.SE_ELEVATION_PROBE) == {}
    monkeypatch.setenv(reference_se.SE_USER_ENV, "u")
    monkeypatch.setenv(reference_se.SE_PASSWORD_ENV, "tajne-heslo")
    head = reference_se.auth_headers(reference_se.SE_ELEVATION_PROBE)
    assert head == {"Authorization": "Basic " + base64.b64encode(b"u:tajne-heslo").decode()}
    assert reference_se.auth_headers("https://api.lantmateriet.se/stac-hojd/v1/search") == {}
    assert reference_se.auth_headers("https://evil.example/m657_67.tif") == {}
    assert "tajne-heslo" not in reference_se.redact("chyba tajne-heslo " + head["Authorization"])
    with reference_se.gdal_header_file() as path:
        assert path.read_text().strip() == "Authorization: " + head["Authorization"]
    assert not path.exists()  # soubor s hlavičkou se po použití smaže


def test_sweden_profile_ready_only_with_account(monkeypatch):
    se = countries.load_countries()["SE"]
    assert se.elevation == "se_lm" and se.crs == "EPSG:3006" and not se.cz_sources and se.technical_map == "none"
    assert "Lantmäteriet" in se.citation and se.status == "beta"
    monkeypatch.setenv(reference_se.SE_USER_ENV, "u")
    monkeypatch.delenv(reference_se.SE_PASSWORD_ENV, raising=False)
    assert not countries.elevation_ready(se)  # bez hesla se země nenabízí
    monkeypatch.setenv(reference_se.SE_PASSWORD_ENV, "p")
    assert countries.elevation_ready(se)
    assert countries.elevation_ready(countries.load_countries()["CZ"])  # Česko beze změny
    probe = next(p for p in source_status.PROBES if p.key == "se_elevation")
    assert probe.countries == ("SE",) and probe.fatal and probe.method == "HEAD"
    assert probe.urls == (reference_se.SE_ELEVATION_PROBE,)


def test_provider_without_account_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.delenv(reference_se.SE_USER_ENV, raising=False)
    with pytest.raises(elevation_raster.ElevationUnavailable):
        elevation_raster.se_lm_provider((0, 0, 10, 10), tmp_path)
