"""Mimo Česko OSM náhrady za vrstvy ZABAGED (audit zdrojů 2026-11-15); v Česku beze změny."""

import pytest

from app.pipeline import osm_paths


@pytest.mark.parametrize(
    "tags, geom, want",
    [
        ({"landuse": "cemetery"}, "way", ("garden", "520")),  # ZABAGED Hrbitov → oliva
        ({"amenity": "grave_yard"}, "way", ("garden", "520")),
        ({"landuse": "allotments"}, "way", ("garden", "520")),  # zahrádkářská kolonie (OvocnySadZahrada)
        ({"landuse": "cemetery"}, "node", None),
        ({"man_made": "tower", "tower:type": "communication"}, "node", ("water_tower", "524")),  # VezovitaStavba
        ({"man_made": "chimney"}, "way", ("water_tower", "524")),
        ({"man_made": "chimney", "building": "yes"}, "way", ("building", "521")),  # obrys budovy zůstává budovou
        ({"man_made": "tower", "tower:type": "bell_tower"}, "node", None),
        ({"man_made": "mast", "tower:type": "lighting"}, "node", None),
        ({"man_made": "mast", "height": "6 m"}, "node", None),
        ({"man_made": "mast", "location": "roof"}, "node", None),
        ({"aerialway": "chair_lift"}, "way", ("aerialway", "510")),  # LanovaDrahaLyzarskyVlek
        ({"aerialway": "pylon"}, "node", None),
        ({"barrier": "city_wall"}, "way", ("wall", "513.2")),  # HradbaVal
        ({"natural": "stone"}, "node", ("boulder", "204")),  # OsamelyBalvanSkalaSkalniSuk
        ({"building": "ruins"}, "way", ("ruin", "523")),  # RozvalinaZricenina (dřív plná budova 521)
        ({"building": "yes", "ruins": "yes"}, "way", ("ruin", "523")),
        ({"historic": "ruins"}, "way", ("ruin", "523")),
        ({"historic": "ruins"}, "node", None),
    ],
)
def test_abroad_substitutes_classify(monkeypatch, tags, geom, want):
    monkeypatch.setattr(osm_paths, "_cz_process", lambda: False)
    assert osm_paths.classify_osm_feature(tags, geom=geom) == want
    monkeypatch.setattr(osm_paths, "_cz_process", lambda: True)
    cz = osm_paths.classify_osm_feature(tags, geom=geom)
    assert cz is None or cz[0] == "building"  # v Česku je kreslí ZABAGED


@pytest.mark.parametrize(
    "kind, preset, code",
    [
        ("aerialway", "forest_10000", "510"),
        ("aerialway", "mtbo_10000", "516"),
        ("boulder", "sprint_2m", "204"),
        ("boulder", "mtbo_10000", "204"),
        ("ruin", "forest_10000", "523"),
        ("ruin", "mtbo_10000", "530"),  # katalog ISMTBOM → 523.0 Zřícenina
        ("ruin", "sprint_2m", "521"),
    ],
)
def test_abroad_substitute_codes(kind, preset, code):
    assert osm_paths.feature_oom_code(kind, preset) == code


def test_abroad_query_and_cz_query_unchanged(monkeypatch):
    new = ("cemetery", "grave_yard", '"aerialway"', "city_wall", '"natural"="stone"', "communications_tower", "wayside_cross", '"historic"="ruins"')
    monkeypatch.setattr(osm_paths, "_cz_process", lambda: True)
    cz = osm_paths._overpass_ql(50.0, 14.0, 50.1, 14.1)
    assert not any(s in cz for s in new)  # pomníky v Česku jen s volbou objektů
    monkeypatch.setattr(osm_paths, "_cz_process", lambda: False)
    abroad = osm_paths._overpass_ql(50.0, 14.0, 50.1, 14.1)
    assert all(s in abroad for s in new)


def _el(tags, lat=50.0005, lon=14.0005):
    return {"type": "node", "id": 1, "lat": lat, "lon": lon, "tags": tags}


@pytest.mark.parametrize("cz, drawn", [(False, True), (True, False)])
def test_memorial_drawn_abroad_without_objects_option(monkeypatch, tmp_path, cz, drawn):
    """Kříž a památný strom mimo Česko i v lese bez volby objektů (v Česku je kreslí ZABAGED)."""
    import json

    monkeypatch.setattr(osm_paths, "_cz_process", lambda: cz)
    elements = [_el({"historic": "wayside_cross"}), _el({"natural": "tree", "denotation": "landmark"}, lon=14.001)]
    monkeypatch.setattr(osm_paths, "fetch_osm_path_elements", lambda *a, **k: elements)
    osm_paths.prepare_osm_paths(tmp_path, (50.0, 14.0, 50.002, 14.002), None, preset_id="forest_10000")
    data = json.loads((tmp_path / "osm_paths" / "features.geojson").read_text(encoding="utf-8"))
    kinds = {f["properties"]["kind"] for f in data["features"]}
    assert ({"memorial", "landmark_tree"} <= kinds) is drawn


def test_tower_inside_building_dropped():
    bldg = {"type": "Feature", "properties": {"kind": "building"},
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]}}
    roof = {"type": "Feature", "properties": {"kind": "water_tower", "tower": "tower"},
            "geometry": {"type": "Point", "coordinates": [5, 5]}}
    free = {"type": "Feature", "properties": {"kind": "water_tower", "tower": "mast"},
            "geometry": {"type": "Point", "coordinates": [50, 5]}}
    water_tower = {"type": "Feature", "properties": {"kind": "water_tower"},  # vodojem (i v Česku) beze změny
                   "geometry": {"type": "Point", "coordinates": [5, 6]}}
    out = osm_paths.drop_towers_in_buildings([bldg, roof, free, water_tower])
    assert roof not in out and free in out and water_tower in out


@pytest.mark.parametrize("preset, scale, code", [("forest_10000", 10000, "523"), ("mtbo_10000", 10000, "523"), ("sprint_2m", 4000, "521")])
def test_ruin_drawn_like_zabaged_ruin(monkeypatch, tmp_path, preset, scale, code):
    """Zřícenina z OSM mimo Česko: les a MTBO obrys zříceniny, sprint budova (jako ZABAGED RozvalinaZricenina)."""
    import json
    import re

    import app.pipeline.oom_symbol_map as sm

    monkeypatch.setattr(osm_paths, "_cz_process", lambda: False)
    ring = [(50.0005, 14.0005), (50.0005, 14.0009), (50.0009, 14.0009), (50.0009, 14.0005), (50.0005, 14.0005)]
    way = {"type": "way", "id": 7, "tags": {"building": "ruins"}, "geometry": [{"lat": la, "lon": lo} for la, lo in ring]}
    monkeypatch.setattr(osm_paths, "fetch_osm_path_elements", lambda *a, **k: [way])
    osm_paths.prepare_osm_paths(tmp_path, (50.0, 14.0, 50.002, 14.002), None, preset_id=preset)
    data = json.loads((tmp_path / "osm_paths" / "features.geojson").read_text(encoding="utf-8"))
    assert [f["properties"]["kind"] for f in data["features"]] == ["ruin"]
    parts = osm_paths.build_osm_feature_parts(
        tmp_path, preset_id=preset, scale=scale, ref_x=0.0, ref_y=0.0, grivation_deg=0.0
    )
    id2code = {v: k for k, v in sm._code_to_index(sm.symbol_set_path(preset, scale)).items()}
    drawn = {id2code[int(m)] for p in parts for m in re.findall(r'<object\b[^>]*\bsymbol="(\d+)"', p.objects_xml)}
    assert {c.removesuffix(".0") for c in drawn} == {code}
