"""Paříž (Plan de voirie) jako obdoba DTM a výšky LiDAR HD: třídění, dotaz na obálku, dlaždice WMS-R."""

import urllib.parse

import pytest

from app.pipeline import elevation_raster, pdv_paris


def _feat(x0: float, y0: float, w: float = 10.0) -> dict:
    ring = [[x0, y0], [x0 + w, y0], [x0 + w, y0 + w], [x0, y0 + w], [x0, y0]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": {}}


def test_classify_paris_like_dtm():
    data = {
        "plan-de-voirie-chaussees": [_feat(0, 0, 50)],
        "plan-de-voirie-trottoirs-emprises": [_feat(0, 50, 50)],
        "plan-de-voirie-aires-mixtes-vehicules-et-pietons": [_feat(100, 0, 30)],
        "plan-de-voirie-voies-en-escalier": [_feat(60, 40, 3)],
        "plan-de-voirie-ilots-directionnels": [_feat(60, 0)],
        "plan-de-voirie-emprises-espaces-verts": [_feat(200, 0)],
    }
    counts = {k: len(v) for k, v in pdv_paris.classify(data).items()}
    assert counts["roads"] == 1 and counts["sidewalks"] == 2 and counts["stairs"] == 1 and counts["small_paved"] == 1
    assert counts["green"] == 1 and counts["buildings"] == 0  # budovy Plan de voirie nevede, kreslí OSM
    assert counts["public"] == 4 and counts["yards"] == 0


def test_fetch_all_datasets_in_bbox():
    calls = []

    def fake(url):
        calls.append(url)
        return {"features": [_feat(0, 0), {"type": "Feature", "geometry": None}]}, 5

    data, nbytes = pdv_paris.fetch_datasets((2.348, 48.855, 2.356, 48.859), fetch=fake)
    assert set(data) == set(pdv_paris.DATASETS) and all(len(v) == 1 for v in data.values())  # bez geometrie pryč
    assert nbytes == 5 * len(pdv_paris.DATASETS)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0]).query)
    assert query["where"] == ["in_bbox(geo_shape,48.855000,2.348000,48.859000,2.356000)"]  # jih, západ, sever, východ
    assert query["limit"] == ["-1"] and "/exports/geojson" in calls[0]


def test_lidarhd_tiles_at_half_metre():
    urls = elevation_raster.fr_lidarhd_urls("dtm", (652000.0, 6861000.0, 655000.0, 6862000.0))
    assert len(urls) == 2  # 3,04 km → dvě dlaždice do 2 km
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)
    x0, y0, x1, y1 = map(float, q["BBOX"][0].split(","))
    assert int(q["WIDTH"][0]) == round((x1 - x0) / 0.5) <= 5010 and int(q["HEIGHT"][0]) == round((y1 - y0) / 0.5)
    assert q["CRS"] == ["EPSG:2154"] and "MNT" in q["LAYERS"][0] and q["FORMAT"] == ["image/geotiff"]
    assert "MNS" in elevation_raster.fr_lidarhd_urls("dsm", (652000.0, 6861000.0, 652500.0, 6861500.0))[0]


def test_lidarhd_download_retries_transient_errors(monkeypatch, tmp_path):
    import urllib.error

    calls = []

    def flaky(url, dest, log=None):
        calls.append(url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, None)
        return dest

    monkeypatch.setattr(elevation_raster, "_download", flaky)
    assert elevation_raster._download_retry("u", tmp_path / "t.tif", wait_s=0) == tmp_path / "t.tif"
    assert len(calls) == 3

    def missing(url, dest, log=None):
        calls.append(url)
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)

    monkeypatch.setattr(elevation_raster, "_download", missing)
    calls.clear()
    with pytest.raises(urllib.error.HTTPError):
        elevation_raster._download_retry("u", tmp_path / "t.tif", wait_s=0)
    assert len(calls) == 1  # chyba požadavku se neopakuje
