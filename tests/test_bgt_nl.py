"""BGT (Nizozemsko) jako obdoba DTM: třídění objektů do souborů DTM a jejich čtení kresbou."""

import json

from app.pipeline import bgt_nl, dtm_import


def _poly(x0: float, y0: float, size: float = 10.0) -> list:
    return [[[x0, y0], [x0 + size, y0], [x0 + size, y0 + size], [x0, y0 + size], [x0, y0]]]


def _feat(geom_type: str, coords, **props) -> dict:
    props.setdefault("lokaal_id", f"id{len(props)}{coords[0][0]}")
    return {"type": "Feature", "geometry": {"type": geom_type, "coordinates": coords}, "properties": props}


def _data() -> dict:
    return {
        "pand": [_feat("MultiPolygon", [_poly(0, 0)], relatieve_hoogteligging=0)],
        "wegdeel": [
            _feat("Polygon", _poly(20, 0), functie="rijbaan lokale weg", fysiek_voorkomen="gesloten verharding", relatieve_hoogteligging=0),
            _feat("Polygon", _poly(40, 0), functie="voetpad", fysiek_voorkomen="open verharding", relatieve_hoogteligging=0),
            _feat("Polygon", _poly(60, 0), functie="voetpad", fysiek_voorkomen="onverhard", relatieve_hoogteligging=0),
            _feat("Polygon", _poly(80, 0), functie="parkeervlak", fysiek_voorkomen="open verharding", relatieve_hoogteligging=0),
            _feat("Polygon", _poly(100, 0), functie="voetpad op trap", fysiek_voorkomen="gesloten verharding", relatieve_hoogteligging=0),
            _feat("Polygon", _poly(120, 0), functie="rijbaan lokale weg", fysiek_voorkomen="gesloten verharding", relatieve_hoogteligging=1),
            _feat("Polygon", _poly(140, 0), functie="fietspad", fysiek_voorkomen="gesloten verharding", relatieve_hoogteligging=0),
        ],
        "onbegroeidterreindeel": [
            _feat("Polygon", _poly(0, 20), fysiek_voorkomen="erf", relatieve_hoogteligging=0),
            _feat("Polygon", _poly(20, 20), fysiek_voorkomen="gesloten verharding", relatieve_hoogteligging=0),
        ],
        "begroeidterreindeel": [
            _feat("Polygon", _poly(40, 20), fysiek_voorkomen="groenvoorziening", relatieve_hoogteligging=0),
            _feat("Polygon", _poly(60, 20), fysiek_voorkomen="heide", relatieve_hoogteligging=0),
            _feat("Polygon", _poly(80, 20), fysiek_voorkomen="fruitteelt", relatieve_hoogteligging=0),
        ],
        "scheiding_lijn": [
            _feat("LineString", [[0, 40], [30, 40]], type="hek", relatieve_hoogteligging=0),
            _feat("LineString", [[0, 50], [30, 50]], type="muur", relatieve_hoogteligging=0),
        ],
        "overbruggingsdeel": [_feat("Polygon", _poly(120, 0), relatieve_hoogteligging=1)],
    }


def test_classify_bgt_like_dtm():
    out = bgt_nl.classify(_data())
    counts = {k: len(v) for k, v in out.items()}
    assert counts["buildings"] == 1 and counts["roads"] == 1 and counts["parking"] == 1 and counts["stairs"] == 1
    assert counts["sidewalks"] == 1  # nezpevněný voetpad ani fietspad (jako CZ cyklostezka) ne
    assert counts["yards"] == 1 and counts["small_paved"] == 1
    assert counts["green"] == 1 and counts["other_cover"] == 1 and counts["garden_cover"] == 1
    assert counts["fences"] == 1  # jen hek, zdi kreslí OSM
    assert sorted(d["l"] for d in out["decks"]) == [1, 1]  # vozovka na mostě + mostní konstrukce
    assert counts["public"] == 6  # všechny wegdeel na úrovni 0 (orientace plotů)


def test_write_dtm_files_read_by_drawing(tmp_path):
    dest = bgt_nl.write_dtm_files(tmp_path, bgt_nl.classify(_data()), (-100.0, -100.0, 300.0, 300.0))
    assert dest == dtm_import.fences_path(tmp_path)
    assert dtm_import.dtm_owns_fences(tmp_path) and dtm_import.dtm_owns_buildings(tmp_path)
    assert dtm_import.dtm_owns_roads(tmp_path) and dtm_import.dtm_owns_sidewalks(tmp_path)
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["source"] == "nl_bgt" and data["opl_files"] == []
    assert len(dtm_import.load_yard_polygons(tmp_path)) == 1
    areas = dtm_import._load_opl_areas(tmp_path)
    assert len(areas["olive"]) == 1 and len(areas["building"]) == 1 and areas["public"]
    olive = dtm_import._olive_geometry(tmp_path, [])
    assert abs(olive.area - 100.0) < 1e-6  # erf je kreslená oliva (orientace plotů, ploty uvnitř)


def test_fetch_collections_tiles_and_dedup():
    calls = []

    def fake(col, tile):
        calls.append((col, tile))
        f = _feat("Polygon", _poly(0, 0), lokaal_id="same")
        return [f], 10

    data, nbytes = bgt_nl.fetch_collections((0, 0, 900, 400), ("pand",), tile_m=500, workers=2, fetch=fake)
    assert len(calls) == 2 and nbytes == 20
    assert len(data["pand"]) == 1  # objekt přes hranu dlaždic jen jednou
    assert "eind_registratie+IS+NULL" in bgt_nl.items_url("pand", (0, 0, 1, 1))


def test_node_coverage_shares_edges_exactly():
    """Sdílená hrana linearizovaná u sousedů jinak (body o milimetry vedle) se po uzlování kryje přesně."""
    import shapely
    from shapely.geometry import Polygon

    left = [[(0, 0), (10, 0), (10.0004, 5), (10, 10), (0, 10), (0, 0)]]
    right = [[(10, 0), (20, 0), (20, 10), (10, 10), (9.9996, 5.0003), (10, 0)]]
    out = bgt_nl.node_coverage({"roads": [left], "parking": [right]})
    road = Polygon(out["roads"][0][0])
    park = shapely.union_all([Polygon(p[0], p[1:]) for p in out["parking"]])
    assert road.intersection(park).area < 1e-9  # žádný překryv
    assert abs(road.area + park.area - 200.0) < 0.01  # ani mezera
    assert park.difference(road).area > 99  # rozdíl projde bez chyby GEOS
