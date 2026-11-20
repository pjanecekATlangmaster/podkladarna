"""USA: planimetrická data měst (NYC, Washington DC) jako obdoba DTM – třídění, stránkování, území."""

import urllib.parse

import pytest

from app import countries
from app.pipeline import city_us


def _poly(x0, y0, x1, y1, **props):
    ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": props}


def test_classify_codes_nyc_and_dc():
    data = {
        "nyc:5zhs-2jue": [_poly(0, 30, 10, 40, feature_code="2100"), _poly(20, 30, 30, 40, feature_code="2110")],
        "nyc:i36f-5ih7": [_poly(0, 0, 100, 8, sub_code="350000")],
        "nyc:52n9-sdep": [_poly(0, 8, 100, 12, sub_code="380000")],
        "nyc:7cgt-uhhz": [_poly(0, 12, 20, 20, sub_code="500000")],
        "nyc:ees7-4ufv": [_poly(40, 2, 60, 4, sub_code="360010"), _poly(60, 2, 70, 4, sub_code="360080")],
        "dc:10": [_poly(200, 0, 300, 8, FEATURECODE=1050), _poly(200, 8, 220, 20, FEATURECODE=1070), _poly(230, 8, 240, 20, FEATURECODE=1091)],
        "dc:11": [_poly(200, 20, 300, 24, FEATURECODE=1480), _poly(250, 24, 255, 28, FEATURECODE=1482), _poly(260, 0, 264, 8, FEATURECODE=1483)],
        "dc:1": [_poly(200, 30, 210, 40, FEATURECODE=2000)],
    }
    out = city_us.classify(data)
    counts = {k: len(out[k]) for k in ("buildings", "roads", "sidewalks", "stairs", "parking", "small_paved")}
    # lávka 2110, malovaný střed 360010, skrytá vozovka 1091 a přechod 1483 se neberou
    assert counts == {"buildings": 2, "roads": 2, "sidewalks": 2, "stairs": 1, "parking": 2, "small_paved": 1}
    assert len(out["public"]) == 8


def test_socrata_pages_and_bbox(monkeypatch):
    monkeypatch.setattr(city_us, "NYC_PAGE", 2)
    calls = []

    def fake(url):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        calls.append(q)
        return {"features": [_poly(0, 0, 1, 1)] * (2 if q["$offset"][0] == "0" else 1)}, 5

    city = city_us.City("nyc", "NYC", city_us.NYC.envelope, "socrata", (city_us.Layer("abcd-1234", "sub_code", {}),))
    data, nbytes = city_us.fetch_city(city, (-73.97, 40.76, -73.96, 40.768), fetch=fake)
    assert len(data["nyc:abcd-1234"]) == 3 and nbytes == 10 and len(calls) == 2
    assert "intersects(the_geom,'POLYGON((-73.970000 40.760000" in calls[0]["$where"][0]


def test_arcgis_follows_transfer_limit():
    pages = []

    def fake(url):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        pages.append(int(q["resultOffset"][0]))
        assert q["inSR"] == ["4326"] and q["outSR"] == ["4326"] and q["f"] == ["geojson"]
        return {"features": [_poly(0, 0, 1, 1)], "exceededTransferLimit": len(pages) < 2}, 1

    city = city_us.City("dc", "DC", city_us.DC.envelope, "arcgis", (city_us.Layer("10", "FEATURECODE", {}),))
    data, _ = city_us.fetch_city(city, (-77.04, 38.895, -77.03, 38.902), fetch=fake)
    assert pages == [0, city_us.DC_PAGE] and len(data["dc:10"]) == 2


@pytest.mark.parametrize(
    "bbox, keys",
    [
        ((-73.97, 40.76, -73.96, 40.768), ["nyc"]),
        ((-77.04, 38.895, -77.03, 38.902), ["dc"]),
        ((-122.31, 47.65, -122.30, 47.66), []),  # Seattle: jen OSM
    ],
)
def test_cities_for_and_area(bbox, keys):
    assert [c.key for c in city_us.cities_for(bbox)] == keys
    assert countries.load_countries()["US"].technical_map_covers(bbox) is bool(keys)


def test_prepare_outside_cities_skips_download(tmp_path, monkeypatch):
    monkeypatch.setattr(city_us, "fetch_city", lambda *a, **k: pytest.fail("mimo města se nestahuje"))
    assert city_us.prepare(tmp_path, (-122.31, 47.65, -122.30, 47.66)) is None
