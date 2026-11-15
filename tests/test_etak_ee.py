"""Estonsko: ETAK jako obdoba DTM (třídění, stránkování WFS, uliční prostor pro sprint) a listy výšek Maa- ja Ruumiamet."""

import json
import os
import urllib.parse

import pytest

from app.pipeline import country_profile, elevation_raster, etak_ee, street_parcels


def _feat(x0: float, y0: float, w: float = 10.0, tyyp: int = 10) -> dict:
    ring = [[x0, y0], [x0 + w, y0], [x0 + w, y0 + w], [x0, y0 + w], [x0, y0]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": {"tyyp": tyyp}}


def _line(tyyp: int) -> dict:
    geom = {"type": "LineString", "coordinates": [[0, 0], [20, 0], [20, 15]]}
    return {"type": "Feature", "geometry": geom, "properties": {"tyyp": tyyp, "etak_id": 7}}


def test_classify_etak_like_dtm():
    data = {
        "e_401_hoone_ka": [_feat(0, 0), _feat(20, 0, tyyp=20), _feat(40, 0, tyyp=40)],  # 40 = zřícenina, ne
        "e_501_tee_a": [_feat(0, 100, 60), _feat(100, 100, tyyp=20), _feat(200, 100, tyyp=60), _feat(300, 100, tyyp=50)],
        "e_302_ou_a": [_feat(0, 200)],
        "e_301_muu_kolvik_a": [_feat(0, 300)],
        "e_405_piire_j": [_line(10), _line(30)],  # zeď (30) kreslí OSM
    }
    out = etak_ee.classify(data)
    counts = {k: len(v) for k, v in out.items()}
    assert counts["buildings"] == 2 and counts["streets"] == 1 and counts["parking"] == 1 and counts["sidewalks"] == 1
    assert counts["roads"] == 0  # uliční prostor od fasády k fasádě není vozovka (šířky silnic, koridory)
    assert counts["yards"] == 1 and counts["green"] == 1 and counts["public"] == 3
    assert counts["fences"] == 1 and out["fences"][0].oid == "7"


def test_fetch_layer_pages_until_short_page(monkeypatch):
    monkeypatch.setattr(etak_ee, "PAGE_SIZE", 2)
    calls = []

    def fake(url):
        calls.append(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query))
        start = int(calls[-1]["startIndex"][0])
        n = 1 if start < 4 else 0  # + objekt bez geometrie (na stránku se počítá, do dat ne)
        return {"features": [_feat(0, 0)] * n + [{"type": "Feature", "geometry": None}]}, 3

    data, nbytes = etak_ee.fetch_layers((540000.0, 6587000.0, 541000.0, 6588000.0), layers=("e_401_hoone_ka",), fetch=fake)
    assert len(data["e_401_hoone_ka"]) == 2 and nbytes == 9 and len(calls) == 3
    assert calls[0]["typeNames"] == ["etak:e_401_hoone_ka"] and calls[0]["srsName"] == ["EPSG:3301"]
    assert calls[0]["bbox"] == ["540000.0,6587000.0,541000.0,6588000.0,EPSG:3301"]


def test_street_space_feeds_sprint_streets_abroad(tmp_path, monkeypatch):
    dest = etak_ee.write_streets(tmp_path, [[[(0, 0), (50, 0), (50, 20), (0, 20), (0, 0)]]])
    assert dest == tmp_path / street_parcels.CACHE_SUBDIR / street_parcels.PARCELS_NAME
    assert json.loads(dest.read_text())["features"][0]["geometry"]["type"] == "Polygon"
    country_profile.set_process_country({"country": "EE"})
    try:
        monkeypatch.setattr(street_parcels.urllib.request, "urlopen", lambda *a, **k: pytest.fail("RÚIAN mimo Česko"))
        assert street_parcels.fetch_street_parcels(tmp_path, (24.73, 59.42, 24.75, 59.43)) == dest
        assert etak_ee.write_streets(tmp_path, []) is None and not dest.exists()
        assert street_parcels.fetch_street_parcels(tmp_path, (24.73, 59.42, 24.75, 59.43)) is None
    finally:
        os.environ.pop(country_profile.JOB_COUNTRY_ENV, None)


@pytest.mark.parametrize(
    "e, n, sheet",
    [
        (541426, 6587787, "63843"),  # Tallinn, Tõnismäe
        (647000, 6377000, "44744"),
        (657000, 6472000, "54752"),
        (631000, 6631000, "74331"),
    ],
)
def test_ee_dtm_sheet_number(e, n, sheet):
    assert elevation_raster.ee_sheet_10k(e, n) == sheet


def test_ee_sheets_and_urls():
    window = (541406.4, 6587767.8, 542509.9, 6588660.3)
    assert elevation_raster.ee_sheets("dtm", window) == ["63843"]
    assert elevation_raster.ee_sheets("dsm", window) == ["587541", "588541", "587542", "588542"]
    assert elevation_raster.ee_sheet_2k(542100, 6589400) == "589542"
    url = elevation_raster.ee_sheet_url("dsm", "589542")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert q["andmetyyp"] == ["ndsm_abs_1m_geotiff"] and q["f"] == ["589542_dsm_1m.tif"] and q["kaardiruut"] == ["589542"]


def test_ee_windows_mark_padding_as_nodata(monkeypatch, tmp_path):
    """Listy DSM nemají NoData: okno přes okraj listu se doplní nulou, která by ve VRT přepsala sousední list."""
    cmds = []

    def fake_run(cmd, log=None):
        cmds.append(cmd)
        if "gdal_translate" in cmd[0]:
            open(cmd[-1], "wb").close()

    monkeypatch.setattr(elevation_raster, "run_cmd", fake_run)
    monkeypatch.setattr(elevation_raster, "_gdal_tool", lambda name: name)  # bez GDAL v prostředí testů
    monkeypatch.setattr(elevation_raster, "_buffered", lambda *a: (541406.4, 6587767.8, 542509.9, 6588660.3))
    elevation_raster.ee_maaamet_provider((0.0, 0.0, 1.0, 1.0), tmp_path)
    translates = [c for c in cmds if "gdal_translate" in c[0]]
    assert len(translates) == 5 and all(c[c.index("-a_nodata") + 1] == "-9999" for c in translates)
