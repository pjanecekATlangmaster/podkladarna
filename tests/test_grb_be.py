"""Flandry: GRB jako obdoba DTM (dělení uličního prostoru na vozovku a chodníky, stránkování WFS) a výšky DHMV II."""

import urllib.parse

import pytest

from app.pipeline import elevation_raster, grb_be


def _poly(x0, y0, x1, y1, **props):
    ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": props}


def _line(coords, **props):
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords}, "properties": props}


def test_street_space_split_by_kerbs_and_axis():
    """Ulice 100 × 20 m: obrubníky ve 4 a 16 m, osa uprostřed → vozovka 12 m, dva chodníky; zelený pás u konce."""
    data = {
        "GBG": [_poly(0, 25, 10, 35, TYPE=1), _poly(20, 25, 30, 35, TYPE=2)],
        "WBN": [_poly(0, 0, 100, 20, TYPE=2)],
        "WGO": [
            _line([[0, 4], [100, 4]], TYPE=1), _line([[0, 16], [100, 16]], TYPE=1),
            _line([[90, 16], [90, 20]], TYPE=3), _line([[100, 16], [90, 16]], TYPE=3), _line([[100, 20], [90, 20]], TYPE=3),
        ],
        "Wegsegment": [_line([[0, 10], [100, 10]])],
    }
    out = grb_be.classify(data)
    area = lambda key: sum(abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(p[0], p[0][1:]))) / 2 for p in out[key])
    assert len(out["buildings"]) == 2 and len(out["streets"]) == 1
    assert area("roads") == pytest.approx(1200) and area("sidewalks") == pytest.approx(760) and area("green") == pytest.approx(40)
    assert len(out["public"]) == len(out["roads"]) + len(out["sidewalks"])  # zelený pás není zpevněná veřejná plocha


def test_street_space_without_kerbs_is_roadway():
    """Stará ulice bez obrubníků: celý uliční prostor s osou je vozovka (nic se nevymyslí)."""
    out = grb_be.classify({"WBN": [_poly(0, 0, 50, 8)], "Wegsegment": [_line([[0, 4], [50, 4]])]})
    assert len(out["roads"]) == 1 and not out["sidewalks"]


def test_fetch_pages_until_short_page(monkeypatch):
    monkeypatch.setattr(grb_be, "PAGE_SIZE", 2)
    calls = []

    def fake(url):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        calls.append(q)
        n = 2 if int(q["startIndex"][0]) == 0 else 1
        return {"features": [_poly(0, 0, 1, 1)] * n}, 5

    data, nbytes = grb_be.fetch_layers((104000.0, 193000.0, 105000.0, 194000.0), layers=("GBG",), fetch=fake)
    assert len(data["GBG"]) == 3 and nbytes == 10 and len(calls) == 2
    assert calls[0]["typeNames"] == ["GRB:GBG"] and calls[0]["bbox"] == ["104000.0,193000.0,105000.0,194000.0,EPSG:31370"]


def test_dhmv_urls_tile_window_at_1m():
    urls = elevation_raster.be_dhmv_urls("dsm", (104000.0, 193000.0, 106500.0, 194000.0))
    assert len(urls) == 2  # 2,5 km + okraj → dvě dlaždice nejvýš 2 km
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)
    x0, y0, x1, y1 = map(float, q["BBOX"][0].split(","))
    assert q["COVERAGE"] == ["DHMVII_DSM_1m"] and q["VERSION"] == ["1.0.0"] and q["FORMAT"] == ["GeoTIFF"]
    assert int(q["WIDTH"][0]) == x1 - x0 and int(q["HEIGHT"][0]) == y1 - y0
