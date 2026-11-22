"""Německé země: ALKIS vereinfacht jako obdoba DTM (čtení GML, třídění využití, stránkování WFS, země výřezu)."""

import os
import urllib.parse

import pytest

from app.pipeline import alkis_de, country_profile, street_parcels

RING = "356450 5644970 356470 5644970 356470 5644990 356450 5644990 356450 5644970"

# NRW (deegree): atributy bez jmenného prostoru, MultiSurface.
GML_NRW = f"""<?xml version="1.0" encoding="utf-8"?>
<wfs:FeatureCollection xmlns="http://repository.gdi-de.org/schemas/adv/produkt/alkis-vereinfacht/2.0"
  xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:wfs="http://www.opengis.net/wfs/2.0" numberReturned="2">
<wfs:member><Nutzung gml:id="a"><oid>a</oid><geometrie>
  <gml:MultiSurface srsDimension="2"><gml:surfaceMember><gml:Polygon><gml:exterior><gml:LinearRing>
  <gml:posList>{RING}</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember></gml:MultiSurface>
</geometrie><nutzart>Platz</nutzart><bez>Fußgängerzone</bez><name>Heumarkt</name></Nutzung></wfs:member>
<wfs:member><Nutzung gml:id="b"><oid>b</oid><geometrie><gml:MultiSurface><gml:surfaceMember><gml:Polygon>
  <gml:exterior><gml:LinearRing><gml:posList>{RING}</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon>
</gml:surfaceMember></gml:MultiSurface></geometrie><nutzart>Wohnbaufläche</nutzart></Nutzung></wfs:member>
</wfs:FeatureCollection>""".encode()

# Durynsko / Porýní-Falc (MapServer): atributy s ``ave:``, holý Polygon s dírou, hodnoty bez přehlásek.
GML_TH = f"""<?xml version='1.0' encoding='UTF-8'?>
<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:gml="http://www.opengis.net/gml/3.2">
<wfs:member><ave:Nutzung xmlns:ave="http://repository.gdi-de.org/schemas/adv/produkt/alkis-vereinfacht/1.0" gml:id="c">
  <ave:geometrie><gml:Polygon><gml:exterior><gml:LinearRing><gml:posList>0 0 100 0 100 100 0 100 0 0</gml:posList>
  </gml:LinearRing></gml:exterior><gml:interior><gml:LinearRing><gml:posList>40 40 60 40 60 60 40 60 40 40</gml:posList>
  </gml:LinearRing></gml:interior></gml:Polygon></ave:geometrie>
  <ave:nutzart>Strassenverkehr</ave:nutzart></ave:Nutzung></wfs:member>
</wfs:FeatureCollection>""".encode()


def test_parse_gml_both_dialects():
    feats, members = alkis_de.parse_features(GML_NRW)
    assert members == 2 and len(feats) == 2
    assert feats[0]["layer"] == "Nutzung" and feats[0]["properties"]["bez"] == "Fußgängerzone"
    assert feats[0]["geometry"]["type"] == "MultiPolygon" and len(feats[0]["geometry"]["coordinates"][0][0]) == 5
    feats, members = alkis_de.parse_features(GML_TH)
    assert members == 1 and feats[0]["properties"]["nutzart"] == "Strassenverkehr"
    assert len(feats[0]["geometry"]["coordinates"][0]) == 2  # vnější obrys + díra


def test_parse_exception_report_raises():
    raw = b'<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1"><ows:Exception><ows:ExceptionText>bad bbox</ows:ExceptionText></ows:Exception></ows:ExceptionReport>'
    with pytest.raises(RuntimeError, match="bad bbox"):
        alkis_de.parse_features(raw)


@pytest.mark.parametrize(
    "nutzart, bez, target",
    [
        ("Straßenverkehr", None, "streets"),
        ("Strassenverkehr", "Begleitfläche Straßenverkehr", "streets"),
        ("Straßenverkehr", "Fußgängerzone", "sidewalks"),
        ("Platz", "Fußgängerzone", "sidewalks"),
        ("Platz", None, "sidewalks"),
        ("Platz", "Marktplatz", "sidewalks"),
        ("Platz", "Parkplatz", "parking"),
        ("Platz", "Festplatz", None),  # bývá tráva, kreslí OSM
        ("Weg", "Fußweg", None),  # čáru cesty kreslí OSM
        ("Wohnbaufläche", None, None),  # oliva ne z bydlení (Petr 2026-11-16)
        ("Wald", None, None),
    ],
)
def test_nutzung_target(nutzart, bez, target):
    props = {"nutzart": nutzart} | ({"bez": bez} if bez else {})
    assert alkis_de.nutzung_target(props) == target


def test_classify_buildings_only_gebaeude():
    def feat(props):
        ring = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        return {"geometry": {"type": "MultiPolygon", "coordinates": [[ring]]}, "properties": props}

    data = {
        "GebaeudeBauwerk": [feat({"gebnutzbez": "Gebäude"}), feat({"gebnutzbez": "Bauteil"}), feat({"gebnutzbez": "Bauwerk"})],
        "Nutzung": [feat({"nutzart": "Straßenverkehr"}), feat({"nutzart": "Platz", "bez": "Parkplatz"})],
    }
    out = alkis_de.classify(data)
    assert len(out["buildings"]) == 1 and len(out["streets"]) == 1 and len(out["parking"]) == 1
    assert out["roads"] == [] and len(out["public"]) == 2  # uliční prostor není vozovka (šířky silnic, koridory)


def test_fetch_pages_until_short_page(monkeypatch):
    monkeypatch.setattr(alkis_de, "PAGE_SIZE", 2)
    calls = []

    def fake(url):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        calls.append(q)
        return GML_NRW if int(q.get("STARTINDEX", ["0"])[0]) < 4 else GML_TH

    state = alkis_de.STATES["DE-NW"]
    data, nbytes = alkis_de.fetch_layers(state, (356000.0, 5644000.0, 357000.0, 5645000.0), layers=("Nutzung",), fetch=fake)
    assert len(data["Nutzung"]) == 5 and len(calls) == 3 and nbytes == 2 * len(GML_NRW) + len(GML_TH)
    assert calls[0]["TYPENAMES"] == ["ave:Nutzung"] and "STARTINDEX" not in calls[0]
    assert calls[0]["BBOX"] == ["356000.0,5644000.0,357000.0,5645000.0,urn:ogc:def:crs:EPSG::25832"]


def test_state_from_job_country_and_probe_url():
    country_profile.set_process_country({"country": "DE-SN"})
    try:
        assert alkis_de.state_for((13.73, 51.04, 13.74, 51.05)).code == "DE-SN"
    finally:
        os.environ.pop(country_profile.JOB_COUNTRY_ENV, None)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(alkis_de.probe_url(alkis_de.STATES["DE-SN"])).query)
    assert q["COUNT"] == ["1"] and q["TYPENAMES"] == ["ave:GebaeudeBauwerk"]


def test_street_space_feeds_sprint_streets(tmp_path):
    from app.pipeline.etak_ee import write_streets

    dest = write_streets(tmp_path, [[[(0, 0), (50, 0), (50, 20), (0, 20), (0, 0)]]])
    assert dest == tmp_path / street_parcels.CACHE_SUBDIR / street_parcels.PARCELS_NAME


def test_configured_states_use_alkis():
    from app import countries

    cfg = countries.load_countries()
    for code, state in alkis_de.STATES.items():
        if state.active:
            assert cfg[code].technical_map == "de_alkis", code
            assert any("ALKIS" in line for line in cfg[code].attribution), code
    assert cfg["DE"].technical_map == "de_sb"  # Berlín zůstává u Straßenbefahrung
