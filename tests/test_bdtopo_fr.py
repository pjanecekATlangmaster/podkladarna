"""Francie mimo Paříž: BD TOPO jako obdoba DTM (třídění silnic, stránkování WFS, volba zdroje podle výřezu)."""

import json
import urllib.parse

import pytest

from app.pipeline import bdtopo_fr


def _road(nature, width, etat="En service", pos="0"):
    props = {"nature": nature, "largeur_de_chaussee": width, "etat_de_l_objet": etat, "position_par_rapport_au_sol": pos}
    return {"geometry": {"type": "LineString", "coordinates": [[0, 0], [100, 0]]}, "properties": props}


@pytest.mark.parametrize(
    "road, kept",
    [
        (_road("Route à 1 chaussée", 6), True),
        (_road("Rond-point", 7), True),
        (_road("Route empierrée", 4), False),  # štěrková cesta, kreslí OSM
        (_road("Chemin", 3), False),
        (_road("Sentier", None), False),
        (_road("Route à 1 chaussée", 6, etat="En projet"), False),
        (_road("Route à 1 chaussée", 6, pos="-1"), False),  # tunel
    ],
)
def test_road_features_filter(road, kept):
    assert bool(bdtopo_fr.road_features([road])) == kept


def test_classify_buffers_roads_by_width():
    ring = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
    data = {
        "batiment": [
            {"geometry": {"type": "MultiPolygon", "coordinates": [[ring]]}, "properties": {"etat_de_l_objet": "En service"}},
            {"geometry": {"type": "MultiPolygon", "coordinates": [[ring]]}, "properties": {"etat_de_l_objet": "En projet"}},
        ],
        "troncon_de_route": [_road("Route à 1 chaussée", 6), _road("Route à 1 chaussée", None)],
    }
    out = bdtopo_fr.classify(data)
    assert len(out["buildings"]) == 1 and len(out["roads"]) == 1 and out["public"] == out["roads"]
    from shapely.geometry import Polygon

    assert Polygon(out["roads"][0][0]).area == pytest.approx(600, rel=0.01)  # 100 m × 6 m


def test_fetch_pages_with_sort(monkeypatch):
    monkeypatch.setattr(bdtopo_fr, "PAGE_SIZE", 2)
    calls = []

    def fake(url):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        calls.append(q)
        n = 2 if int(q.get("STARTINDEX", ["0"])[0]) < 4 else 1
        return json.dumps({"features": [{"geometry": {"type": "Point", "coordinates": [0, 0]}, "properties": {}}] * n}).encode()

    data, _n = bdtopo_fr.fetch_layers((840000.0, 6519000.0, 841000.0, 6520000.0), layers=("batiment",), fetch=fake)
    assert len(data["batiment"]) == 5 and len(calls) == 3
    assert calls[0]["COUNT"] == ["2"] and calls[0]["SORTBY"] == ["cleabs"] and "STARTINDEX" not in calls[0]
    assert calls[0]["BBOX"] == ["840000.0,6519000.0,841000.0,6520000.0,urn:ogc:def:crs:EPSG::2154"]


def test_fetch_raises_on_exception_report():
    with pytest.raises(RuntimeError, match="BD TOPO"):
        bdtopo_fr.fetch_layers((0, 0, 1, 1), layers=("batiment",), fetch=lambda _u: b'{"exceptions": ["bad"]}')


def test_france_outside_paris_uses_bdtopo():
    from app import area_probe, countries

    fr = countries.load_countries()["FR"]
    assert fr.technical_map == "fr_pdv" and fr.technical_map_outside == "fr_bdtopo"
    assert area_probe.technical_map_share(fr, (4.825, 45.754, 4.840, 45.762)) == 1.0  # Lyon
