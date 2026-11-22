"""Finsko (Espoo): InfStreet jako obdoba DTM vedle Helsinek (GML s oblouky, třídění částí ulice, volba města)."""

import math
import urllib.parse

import pytest
from shapely.geometry import Polygon

from app.pipeline import espoo_fi, ylre_fi

# Část ulice: vnější obrys ze segmentů (úsečky + půlkruh nad osou x), jako GML ze serveru Tekla.
GML = """<?xml version="1.0" encoding="utf-8"?>
<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs" xmlns:gml="http://www.opengis.net/gml"
  xmlns:GIS="http://www.tekla.com/schemas/GIS" numberOfFeatures="2">
<gml:featureMember><GIS:InfStreet>
  <GIS:PARTCLASSTEXT>Kevyt liikenne</GIS:PARTCLASSTEXT><GIS:USETYPETEXT>Tori/Aukio</GIS:USETYPETEXT>
  <GIS:PARTLAYER>Base</GIS:PARTLAYER>
  <GIS:GEOMETRY><gml:Polygon><gml:exterior><gml:Ring><gml:curveMember><gml:Curve><gml:segments>
    <gml:LineStringSegment><gml:pos>-10 0</gml:pos><gml:pos>10 0</gml:pos></gml:LineStringSegment>
    <gml:Arc><gml:pos>10 0</gml:pos><gml:pos>0 10</gml:pos><gml:pos>-10 0</gml:pos></gml:Arc>
  </gml:segments></gml:Curve></gml:curveMember></gml:Ring></gml:exterior></gml:Polygon></GIS:GEOMETRY>
</GIS:InfStreet></gml:featureMember>
<gml:featureMember><GIS:InfStreet>
  <GIS:PARTCLASSTEXT>Ajorata</GIS:PARTCLASSTEXT><GIS:USETYPETEXT>Pääkatu</GIS:USETYPETEXT><GIS:PARTLAYER>Base</GIS:PARTLAYER>
  <GIS:GEOMETRY><gml:Polygon><gml:exterior><gml:LinearRing>
    <gml:pos>0 0</gml:pos><gml:pos>20 0</gml:pos><gml:pos>20 5</gml:pos><gml:pos>0 5</gml:pos><gml:pos>0 0</gml:pos>
  </gml:LinearRing></gml:exterior></gml:Polygon></GIS:GEOMETRY>
</GIS:InfStreet></gml:featureMember>
</wfs:FeatureCollection>""".encode()


def test_parse_ring_with_arc():
    feats = espoo_fi.parse_features(GML)
    assert len(feats) == 2 and feats[0]["properties"]["USETYPETEXT"] == "Tori/Aukio"
    half_disc = Polygon(feats[0]["geometry"]["coordinates"][0][0])
    assert half_disc.area == pytest.approx(math.pi * 100 / 2, rel=0.02)  # půlkruh r = 10 m
    assert Polygon(feats[1]["geometry"]["coordinates"][0][0]).area == pytest.approx(100)


def test_parse_exception_report_raises():
    raw = b'<ExceptionReport xmlns="http://www.opengis.net/ows"><Exception><ExceptionText>bad</ExceptionText></Exception></ExceptionReport>'
    with pytest.raises(RuntimeError, match="Espoo WFS: bad"):
        espoo_fi.parse_features(raw)


@pytest.mark.parametrize(
    "props, target",
    [
        ({"PARTCLASSTEXT": "Ajorata", "USETYPETEXT": "Pääkatu"}, "roads"),
        ({"PARTCLASSTEXT": "Ajorata", "USETYPETEXT": "Teollisuuskatu"}, "roads"),  # jiná vozovka
        ({"PARTCLASSTEXT": "Ajorata", "USETYPETEXT": "Pysäköintialue"}, "parking"),
        ({"PARTCLASSTEXT": "Ajorata", "USETYPETEXT": "Liikenteenjakaja"}, "small_paved"),
        ({"PARTCLASSTEXT": "Kevyt liikenne", "USETYPETEXT": "Jalkakäytävä"}, "sidewalks"),
        ({"PARTCLASSTEXT": "Kevyt liikenne", "USETYPETEXT": "Tori/Aukio"}, "sidewalks"),
        ({"PARTCLASSTEXT": "Kevyt liikenne", "USETYPETEXT": "Jalkakäytävä", "ADDINFO2TEXT": "Portaat"}, "stairs"),
        ({"PARTCLASSTEXT": "Kevyt liikenne", "USETYPETEXT": "Puistoraitti"}, None),  # parková cesta: čára OSM
        ({"PARTCLASSTEXT": "Ajorata", "USETYPETEXT": "Erotus/Välikaista"}, None),  # dělicí pás bývá zelený
        ({"PARTCLASSTEXT": "Ajorata", "USETYPETEXT": "Pääkatu", "PARTLAYER": "Under"}, None),  # podjezd
    ],
)
def test_street_target(props, target):
    assert espoo_fi.street_target(props) == target


def test_classify_street_space_from_parts():
    out = espoo_fi.classify({espoo_fi.STREETS: espoo_fi.parse_features(GML)})
    assert len(out["sidewalks"]) == 1 and len(out["roads"]) == 1 and len(out["streets"]) == 2


def test_wfs_url_bbox_without_crs():
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(espoo_fi.wfs_url(espoo_fi.STREETS, (1.0, 2.0, 3.0, 4.0))).query)
    assert q["version"] == ["1.1.0"] and q["bbox"] == ["1.0,2.0,3.0,4.0"]  # s CRS server vrátí 0 objektů


@pytest.mark.parametrize(
    "bbox, espoo, helsinki",
    [
        ((24.800, 60.172, 24.812, 60.178), True, False),  # Tapiola
        ((24.940, 60.165, 24.950, 60.170), False, True),  # centrum Helsinek
        ((25.000, 61.000, 25.010, 61.005), False, False),  # mimo obě města
    ],
)
def test_city_dispatch(bbox, espoo, helsinki):
    assert espoo_fi.intersects(bbox) == espoo and ylre_fi.helsinki_intersects(bbox) == helsinki
