"""FMZK (Vídeň) jako obdoba DTM a výšky BEV ALS: třídění tříd FMZK, zahrady, dlaždice a ročník mozaiky BEV."""

import datetime as dt

import pytest

from app.pipeline import fmzk_at


def _poly(x0: float, y0: float, w: float = 10.0, h: float | None = None) -> list:
    h = w if h is None else h
    return [[[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h], [x0, y0]]]


def _feat(coords, cls: int, fid: int) -> dict:
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": coords},
            "properties": {"F_KLASSE": cls, "FMZK_ID": fid}}


def _data() -> dict:
    return {
        "FMZKVERKEHR1OGD": [
            _feat(_poly(0, 0, 100, 8), 21, 1),  # Fahrbahn
            _feat(_poly(0, 0, 4, 8), 30, 2),  # Zebrastreifen (nekreslí se)
            _feat(_poly(100, 0, 20, 8), 33, 3),  # Ruhender Verkehr
        ],
        "FMZKVERKEHR2OGD": [
            _feat(_poly(0, 8, 100, 3), 23, 4),  # Gehsteig
            _feat(_poly(0, 40, 3, 3), 74, 5),  # Stiege
            _feat(_poly(30, 30, 10, 10), 51, 6),  # Innenhof → oliva
        ],
        "FMZKGEBOGD": [_feat(_poly(0, 11, 30, 15), 11, 7), _feat(_poly(60, 11, 5, 5), 13, 8)],  # budova, Flugdach
        "FMZKGRUEN2OGD": [
            _feat(_poly(0, 26, 30, 10), 58, 9),  # zadní zahrada za domem (bez ulice) → oliva
            _feat(_poly(40, 11, 20, 10), 58, 10),  # předzahrádka u chodníku → zeleň
        ],
        "FMZKGRUEN1OGD": [_feat(_poly(200, 0, 50, 50), 52, 11)],  # Wald
    }


def test_classify_fmzk_like_dtm():
    out = fmzk_at.classify(_data())
    counts = {k: len(v) for k, v in out.items()}
    assert counts["roads"] == 1 and counts["parking"] == 1 and counts["sidewalks"] == 1 and counts["stairs"] == 1
    assert counts["buildings"] == 1  # Flugdach ne
    assert counts["yards"] == 2 and counts["green"] == 1  # dvůr + zadní zahrada; předzahrádka zůstane zelení
    assert counts["other_cover"] == 1 and counts["public"] == 3


def test_fetch_layers_dedup_by_id():
    def fake(url):
        return {"features": [_feat(_poly(0, 0), 11, 42)]}, 5

    data, nbytes = fmzk_at.fetch_layers((0, 0, 1900, 900), "EPSG:25833", ("FMZKGEBOGD",), tile_m=1000, fetch=fake)
    assert len(data["FMZKGEBOGD"]) == 1 and nbytes == 10
    url = fmzk_at.items_url("FMZKGEBOGD", (0, 0, 1000, 1000), "EPSG:25833")
    assert "typeName=ogdwien%3AFMZKGEBOGD" in url and "srsName=EPSG%3A25833" in url


def test_fmzk_fetched_in_mgi_and_reprojected_locally():
    # Stephansdom v MGI / GK M34 → ETRS89 / UTM 33N; server sám převádí o ~290 m vedle (bez posunu data)
    data = {"FMZKGEBOGD": [_feat([[[3050.1, 341121.6], [3060.1, 341121.6], [3060.1, 341131.6], [3050.1, 341121.6]]], 11, 1)]}
    fmzk_at.reproject_features(data, fmzk_at.SOURCE_CRS, "EPSG:25833")
    x, y = data["FMZKGEBOGD"][0]["geometry"]["coordinates"][0][0]
    assert abs(x - 602018.0) < 2.0 and abs(y - 5340385.0) < 2.0
    xmin, ymin, xmax, ymax = fmzk_at.source_bounds((601968.0, 5340335.0, 602068.0, 5340435.0), "EPSG:25833")
    assert xmin < 3050.1 < xmax and ymin < 341121.6 < ymax


def test_bev_tiles_and_latest_date(monkeypatch):
    from app.pipeline import elevation_raster as er

    assert er.bev_tiles((4790000, 2799990, 4800010, 2805000)) == [
        (4750000, 2750000), (4750000, 2800000), (4800000, 2750000), (4800000, 2800000)
    ]
    monkeypatch.setattr(er, "_bev_dates", {})
    seen = []

    def head(url):
        seen.append(url)
        return "/20250915/" in url

    assert er.bev_latest_date(4750000, 2800000, head=head, today=dt.date(2026, 11, 15)) == "20250915"
    assert seen[0].endswith("/DTM/20260915/ALS_DTM_CRS3035RES50000mN2800000E4750000.tif")
    assert er.bev_latest_date(4750000, 2800000, head=lambda u: pytest.fail("cache")) == "20250915"


@pytest.mark.parametrize(
    "bbox, source, vienna_keys",
    [
        ((16.365, 48.205, 16.375, 48.212), "at_fmzk", True),  # Innere Stadt
        ((11.39, 47.26, 11.40, 47.27), "none", False),  # Innsbruck: FMZK a Luftbild jen ve Vídni
    ],
)
def test_fmzk_and_vienna_templates_only_in_vienna(tmp_path, monkeypatch, bbox, source, vienna_keys):
    from app import countries
    from app.pipeline import country_profile, online_templates

    assert country_profile.technical_map_source({"country": "AT", "bbox_wgs84": list(bbox)}) == source
    monkeypatch.setattr(online_templates, "osm_sjtsk_vrt", lambda src, vrt, b, mpp: vrt.write_text(src.name) or True)
    covers = countries.load_countries()["AT"].technical_map_covers(bbox)
    written = online_templates.write_online_templates(
        (0.0, 0.0, 1000.0, 1000.0), tmp_path, country="AT", technical_area=covers
    )
    assert {"online_at_ortho", "online_at_basemap", "online_at_relief"} <= set(written)  # basemap.at celé Rakousko
    assert ("online_at_luftbild" in written) is vienna_keys and ("online_at_fmzk" in written) is vienna_keys
    assert "mapsneu.wien.gv.at/basemap/bmaporthofoto30cm" in (tmp_path / "orthofoto_at_dlazdice.xml").read_text()
    assert set(written) <= set(online_templates.online_labels())
