"""Dánsko: GeoDanmark z WFS Datafordeleru – klíč jen při dotazu, třídění vrstev a vozovky z okrajů silnic."""

import io
import urllib.error
import urllib.parse

import pytest
from shapely.geometry import Polygon

from app.pipeline import geodanmark_dk as gd
from app.pipeline import reference_dk

KEY = "TESTKEY0123456789abcdef"


def _line(coords, **props):
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords}, "properties": props}


def test_wfs_url_has_placeholder_not_key(monkeypatch):
    monkeypatch.setenv(reference_dk.DK_API_KEY_ENV, KEY)
    url = gd.wfs_url("bygning", (574000.0, 6223000.0, 575000.0, 6224000.0), start=10000)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert q["typeNames"] == ["geodkv_v001:bygning_current"] and q["startIndex"] == ["10000"]
    assert q["sortBy"] == ["objectid"]  # stálé pořadí stránek
    assert q["outputFormat"] == ["application/json"] and KEY not in url and reference_dk.KEY_PLACEHOLDER in url


def test_get_json_retries_and_hides_key(monkeypatch):
    monkeypatch.setenv(reference_dk.DK_API_KEY_ENV, KEY)
    monkeypatch.setattr(gd.time, "sleep", lambda s: None)
    seen = []

    def fake(req, timeout):
        seen.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(gd.urllib.request, "urlopen", fake)
    with pytest.raises(RuntimeError) as err:
        gd._get_json(gd.wfs_url("hegn", (0, 0, 1, 1)))
    assert len(seen) == gd.HTTP_RETRIES + 1 and KEY in seen[0]
    assert str(err.value) == "GeoDanmark: HTTP 401" and err.value.__suppress_context__


def test_road_from_edges_has_measured_width():
    axis = [(0.0, 0.0), (100.0, 0.0)]
    edges = [[(-5.0, 3.0), (105.0, 3.0)], [(-5.0, -4.0), (105.0, -4.0)]]  # vozovka 7 m, osa mimo střed
    polys = gd.road_polygons([axis], edges)
    assert len(polys) == 1
    minx, miny, maxx, maxy = polys[0].bounds
    assert miny == pytest.approx(-4.0, abs=0.05) and maxy == pytest.approx(3.0, abs=0.05)
    assert polys[0].area == pytest.approx(7 * 100, rel=0.05)


@pytest.mark.parametrize("edges", [[], [[(-5.0, 3.0), (105.0, 3.0)]], [[(-5.0, 20.0), (105.0, 20.0)], [(-5.0, -20.0), (105.0, -20.0)]]])
def test_no_road_without_both_edges_in_reach(edges):
    assert gd.road_polygons([[(0.0, 0.0), (100.0, 0.0)]], edges) == []


def test_classify_layers():
    square = [[574000.0, 6223000.0, 5.0], [574010.0, 6223000.0, 5.0], [574010.0, 6223010.0, 5.0], [574000.0, 6223010.0, 5.0], [574000.0, 6223000.0, 5.0]]
    bld = lambda status: {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [square]},
                          "properties": {"status": status, "synligBygning": True}}
    data = {
        "bygning": [bld("Anlagt"), bld("Projekt godkendt")],
        "hegn": [_line([[0, 0], [5, 0]], id_lokalId="7")],
        "parkering": [_line(square), _line([[0, 0], [5, 0], [5, 5]])],
        "vejmidte": [_line([[0, 0], [100, 0]], vejmidtetype="Vej", trafikart="Al færdsel"),
                     _line([[0, 50], [100, 50]], vejmidtetype="Sti", trafikart="Sti")],
        "vejkant": [_line([[-5, 3], [105, 3]], type="Kørevej"), _line([[-5, -4], [105, -4]], type="Kørevej"),
                    _line([[-5, 52], [105, 52]], type="Sti, diverse"), _line([[-5, 48], [105, 48]], type="Sti, diverse")],
    }
    out = gd.classify(data)
    assert len(out["buildings"]) == 1 and len(out["parking"]) == 1  # projekt ani otevřená čára ne
    assert len(out["fences"]) == 1 and out["fences"][0].oid == "7"
    assert len(out["roads"]) == 1  # stezka se okraji stezky nestane vozovkou
    assert Polygon(out["roads"][0][0]).area == pytest.approx(700, rel=0.05)
