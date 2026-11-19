"""Finsko: Maastotietokanta nad OSM – listy zrcadla, druhy a ořez o OSM, skála jen otevřená, opakované doplnění."""

import json

import pytest

from app import countries
from app.pipeline import mtk_fi
from app.pipeline.osm_paths import feature_oom_code


def _sq(x0, y0, s, **props):
    ring = [[x0, y0], [x0 + s, y0], [x0 + s, y0 + s], [x0, y0 + s], [x0, y0]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": props}


def _pt(x, y, **props):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [x, y]}, "properties": props}


@pytest.mark.parametrize(
    "sheet, half",
    [("L4132D", "L4132L"), ("L4141C", "L4141L"), ("L4141E", "L4141R"), ("M4321H", "M4321R")],
)
def test_half_sheet_on_mirror(sheet, half):
    assert mtk_fi.half_sheet(sheet) == half
    assert mtk_fi.sheet_url(half).endswith(f"/{half[:2]}/{half[:3]}/{half}.shp.zip")


@pytest.mark.parametrize(
    "kind, preset, code",
    [("wetland_indistinct", "forest_10000", "310"), ("wetland_indistinct", "mtbo_10000", "310"),
     ("cliff_mtk", "forest_10000", "201"), ("bare_rock", "forest_10000", "214")],
)
def test_new_kinds_have_symbols(kind, preset, code):
    assert feature_oom_code(kind, preset, "") == code


def _layers(monkeypatch, forest=False):
    layers = {
        "m_L4132L_p": [
            _sq(0, 0, 40, LUOKKA=35412),  # bažina, polovinu kryje OSM
            _sq(100, 0, 40, LUOKKA=35300),  # soistuma
            _sq(110, 10, 15, LUOKKA=35411),  # bažina uvnitř soistumy (vyřízne se ze soistumy)
            _sq(200, 0, 20, LUOKKA=34100),  # skála
            _sq(300, 0, 5, LUOKKA=35412),  # drobná bažina pod minimem
            _sq(400, 0, 40, LUOKKA=32611),  # pole
        ],
        "m_L4132L_s": [_pt(500, 5, LUOKKA=34600), _pt(600, 5, LUOKKA=34600)],
        "m_L4132L_v": [{"type": "Feature", "properties": {"LUOKKA": 34400},
                        "geometry": {"type": "LineString", "coordinates": [[700, 5], [730, 5], [760, 5]]}}],
    }
    monkeypatch.setattr(mtk_fi, "half_sheets", lambda window: ["L4132L"])
    monkeypatch.setattr(mtk_fi, "ensure_sheet", lambda half, **_kw: half)
    monkeypatch.setattr(mtk_fi, "_read_layer", lambda zp, layer, classes, window, tmp: layers.get(layer, []))
    monkeypatch.setattr(mtk_fi, "_open_part_fn",
                        lambda work_dir: (lambda poly: poly.difference(poly) if forest else poly))


def test_features_by_kind_clipped_to_osm(tmp_path, monkeypatch):
    _layers(monkeypatch)
    osm = [_sq(0, 0, 20, kind="wetland", source="osm"), _pt(602, 5, kind="boulder", source="osm")]
    feats, stats = mtk_fi.mtk_features(tmp_path, (0, -10, 750, 50), osm)
    by = {}
    for f in feats:
        assert f["properties"]["source"] == "mtk"
        by.setdefault(f["properties"]["kind"], []).append(f)
    from shapely.geometry import shape

    wet = sum(shape(f["geometry"]).area for f in by["wetland"])
    assert wet == pytest.approx(40 * 40 - 20 * 20 + 15 * 15)  # OSM odečtena, drobná vynechána
    assert shape(by["wetland_indistinct"][0]["geometry"]).area == pytest.approx(40 * 40 - 15 * 15)
    assert len(by["bare_rock"]) == 1 and len(by["farmland"]) == 1
    assert len(by["boulder"]) == 1  # druhý balvan je u OSM balvanu
    assert len(by["cliff_mtk"]) == 1 and shape(by["cliff_mtk"][0]["geometry"]).length == pytest.approx(50)


def test_forested_rock_is_skipped(tmp_path, monkeypatch):
    _layers(monkeypatch, forest=True)
    feats, stats = mtk_fi.mtk_features(tmp_path, (0, -10, 750, 50), [])
    assert not [f for f in feats if f["properties"]["kind"] == "bare_rock"] and stats["bare_rock_forest_pct"] == 100


def test_add_replaces_previous_mtk(tmp_path, monkeypatch):
    _layers(monkeypatch)
    path = tmp_path / "osm_paths" / "features.geojson"
    path.parent.mkdir()
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [_pt(1, 1, kind="bench", source="osm")]}))
    n1 = mtk_fi.add_mtk_features(tmp_path, (0, -10, 750, 50))
    n2 = mtk_fi.add_mtk_features(tmp_path, (0, -10, 750, 50))
    feats = json.loads(path.read_text())["features"]
    assert n1 == n2 > 0 and len(feats) == n1 + 1


def test_finland_profile_uses_mtk():
    fi = countries.load_countries()["FI"]
    assert fi.terrain_vectors == "fi_mtk"
    assert any("Maastotietokanta" in line for line in fi.attribution)
    assert countries.load_countries()["NO"].terrain_vectors == ""
