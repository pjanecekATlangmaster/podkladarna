"""Finsko: listy výšek MML / porostu Metsäkeskus (TM35 1 : 10 000) a Helsinky YLRE jako obdoba DTM."""

import urllib.parse

import pytest

from app.pipeline import elevation_raster, ylre_fi


def _poly(x0, y0, x1, y1, **props):
    ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": props}


@pytest.mark.parametrize(
    "east, north, sheet",
    [
        (385700, 6672127, "L4133B"),  # Helsinky, centrum
        (363542, 6688492, "L4132D"),  # Nuuksio
        (389299, 6680931, "L4134C"),  # Paloheinä
    ],
)
def test_fi_sheet_names(east, north, sheet):
    assert elevation_raster.fi_sheet(east, north) == sheet


def test_fi_sheet_urls_and_window():
    assert elevation_raster.fi_sheet_url("dtm", "L4133B").endswith("/dem2m/2008_latest/L4/L41/L4133B.tif")
    assert elevation_raster.fi_sheet_url("chm", "L4133B").endswith("/Karttalehti/uusin/CHM_L4133B_uusin.tif")
    # výřez přes hranici listů (6 km) → oba listy
    assert set(elevation_raster.fi_sheets((385900.0, 6677900.0, 386100.0, 6678100.0))) == {"L4133B", "L4133D", "L4134A", "L4134C"}


def test_ylre_classify_subtypes():
    data = {
        ylre_fi.BUILDINGS: [_poly(0, 30, 10, 40)],
        ylre_fi.ROADS: [_poly(0, 0, 100, 8, alatyyppi="Ajorata (Silta)"), _poly(0, 8, 20, 12, alatyyppi="Pysäköintialue")],
        ylre_fi.WALKS: [_poly(0, 12, 100, 15, alatyyppi="Erotettu jalkakäytävä"), _poly(50, 15, 55, 18, alatyyppi="Portaat")],
        ylre_fi.OTHER: [_poly(0, 18, 30, 25, paatyyppi="Nurmikot ja niityt"), _poly(30, 18, 40, 25, paatyyppi="Puistokäytävät")],
        ylre_fi.STREETS: [_poly(0, 0, 100, 25)],
    }
    out = ylre_fi.classify(data)
    counts = {k: len(out[k]) for k in ("buildings", "roads", "parking", "sidewalks", "stairs", "green", "streets")}
    assert counts == {"buildings": 1, "roads": 1, "parking": 1, "sidewalks": 1, "stairs": 1, "green": 1, "streets": 1}
    assert len(out["public"]) == 4  # vozovka, parkoviště, chodník, schody; trávník ne


def test_ylre_fetch_pages_native_crs(monkeypatch):
    monkeypatch.setattr(ylre_fi, "PAGE_SIZE", 2)
    calls = []

    def fake(url):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        calls.append(q)
        return {"features": [_poly(0, 0, 1, 1)] * (2 if int(q["startIndex"][0]) == 0 else 0)}, 7

    data, nbytes = ylre_fi.fetch_layers((25496000.0, 6672000.0, 25497000.0, 6673000.0), layers=(ylre_fi.BUILDINGS,), fetch=fake)
    assert len(data[ylre_fi.BUILDINGS]) == 2 and nbytes == 14 and len(calls) == 2
    assert calls[0]["typeNames"] == ["avoindata:Rakennukset_alue"] and calls[0]["srsName"] == ["EPSG:3879"]
