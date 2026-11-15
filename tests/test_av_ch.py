"""AV (Švýcarsko) jako obdoba DTM: třídění Bodenbedeckung a Einzelobjekte do souborů DTM, stahování po dlaždicích."""

import json

import pytest

from app.pipeline import av_ch, dtm_import


def _poly(x0: float, y0: float, w: float = 10.0, h: float | None = None) -> list:
    h = w if h is None else h
    return [[[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h], [x0, y0]]]


def _feat(coords, art: str) -> dict:
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": coords}, "properties": {"Art": art}}


def _data() -> dict:
    return {
        "LCSF": [
            _feat(_poly(0, 0), "Gebaeude"),
            _feat(_poly(10, 0, 20, 10), "Gartenanlage"),  # u budovy, 200 m² → oliva
            _feat(_poly(0, 100, 60, 60), "Gartenanlage"),  # park bez budovy → zeleň
            _feat(_poly(0, 20, 40, 4), "Strasse_Weg"),  # ulice u domů
            _feat(_poly(0, 24, 40, 2), "Trottoir"),
            _feat(_poly(40, 20, 4, 4), "Verkehrsinsel"),
            _feat(_poly(500, 0, 200, 40), "geschlossener_Wald"),
            _feat(_poly(500, 44, 200, 56), "geschlossener_Wald"),
            _feat(_poly(500, 40, 200, 4), "Strasse_Weg"),  # lesní cesta daleko od zástavby → OSM
            _feat(_poly(50, 50), "Reben"),
            _feat(_poly(70, 50), "Gewaesser_fliessendes"),
        ],
        "SOSF": [
            _feat(_poly(0, 30, 3, 3), "wichtige_Treppe"),
            _feat(_poly(0, 40, 5, 20), "Bruecke_Passerelle"),
            _feat(_poly(0, 70), "unterirdisches_Gebaeude"),
        ],
    }


def test_classify_av_like_dtm():
    out = av_ch.classify(_data())
    counts = {k: len(v) for k, v in out.items()}
    assert counts["buildings"] == 1 and counts["sidewalks"] == 1 and counts["stairs"] == 1
    assert counts["roads"] == 1  # lesní cesta odpadla, ulice zůstala
    assert counts["yards"] == 1 and counts["green"] == 1  # zahrada u domu = oliva, park = zeleň
    assert counts["small_paved"] == 1 and counts["garden_cover"] == 1 and counts["other_cover"] == 2
    assert [d["l"] for d in out["decks"]] == [1] and counts["fences"] == 0
    assert counts["public"] == 4  # silnice, chodník, ostrůvek (orientace plotů)


@pytest.mark.parametrize(
    "buildings, expected_yard",
    [
        (1, True),  # rodinný dům se zahradou
        (0, False),  # park
    ],
)
def test_split_gardens(buildings, expected_yard):
    garden = _poly(0, 0, 40, 40)  # 1 600 m²
    houses = [_poly(40, i * 12, 8, 8) for i in range(buildings)]
    yards, green = av_ch.split_gardens([garden], houses)
    assert (len(yards), len(green)) == ((1, 0) if expected_yard else (0, 1))


def test_split_gardens_large_block_per_building():
    garden = _poly(0, 0, 100, 100)  # 10 000 m²: dvě budovy = park sídliště, deset řadových domů = zahrady
    assert av_ch.split_gardens([garden], [_poly(100, 0, 8, 8), _poly(100, 20, 8, 8)]) == ([], [garden])
    rows = [_poly(100, i * 10, 8, 8) for i in range(10)]
    assert av_ch.split_gardens([garden], rows) == ([garden], [])


def test_write_dtm_files_read_by_drawing(tmp_path):
    from app.pipeline.bgt_nl import write_dtm_files

    dest = write_dtm_files(tmp_path, av_ch.classify(_data()), (-100.0, -100.0, 800.0, 300.0), source="ch_av", label="AV")
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["source"] == "ch_av" and data["packages"] == ["AV"]
    assert dtm_import.dtm_owns_buildings(tmp_path) and dtm_import.dtm_owns_roads(tmp_path)
    assert dtm_import.dtm_owns_sidewalks(tmp_path) and len(dtm_import.load_yard_polygons(tmp_path)) == 1


def test_fetch_layers_tiles_and_dedup():
    calls = []

    def fake(layer, tile):
        calls.append((layer, tile))
        return [_feat(_poly(0, 0), "Gebaeude")], 7  # stejný objekt ve všech dlaždicích (přesah)

    data, nbytes = av_ch.fetch_layers((0, 0, 1900, 900), ("LCSF", "SOSF"), tile_m=1000, workers=2, fetch=fake)
    assert len(calls) == 4 and nbytes == 28
    assert len(data["LCSF"]) == 1 and len(data["SOSF"]) == 1
    assert "BBOX=0.0%2C0.0%2C1000.0" in av_ch.items_url("LCSF", (0, 0, 1000, 1000))


def test_stac_latest_assets():
    from app.pipeline.elevation_raster import stac_latest_assets

    def item(year, tile, gsd_hrefs):
        return {
            "id": f"swissalti3d_{year}_{tile}",
            "properties": {"datetime": f"{year}-01-01T00:00:00Z"},
            "assets": {h: {"href": h, "eo:gsd": g} for g, h in gsd_hrefs},
        }

    items = [
        item(2019, "2600-1199", [(0.5, "a19_05.tif"), (2.0, "a19_2.tif")]),
        item(2025, "2600-1199", [(0.5, "a25_05.tif"), (0.5, "a25_05.xyz.zip")]),
        item(2019, "2601-1199", [(0.5, "b19_05.tif")]),
    ]
    assert stac_latest_assets(items) == ["a25_05.tif", "b19_05.tif"]
