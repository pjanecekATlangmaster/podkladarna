"""Itálie – Trentino: index dlaždic LiDARu PAT, služba merge s cache po dlaždicích a pokrytí výřezu."""

import io
import json
import urllib.parse
import zipfile

import pytest

from app.pipeline import elevation_raster as er


def _tile_feat(x0: int, y0: int, suffix: str) -> dict:
    tile = f"5h{x0 // 100:04d}{y0 // 100:05d}"
    return {"properties": {"x_min": x0, "y_min": y0, "n_tavola": tile, "location": f"/mnt/share/{tile}_{suffix}.asc.zip"}}


def _wfs(tiles, url=""):
    suffix = "DSMFirst" if "dsmf" in url else "DTM"
    return json.dumps({"features": [_tile_feat(x, y, suffix) for x, y in tiles]}).encode()


def test_tiles_skip_edge_touching(monkeypatch):
    urls = []

    def fake(url, data=None, timeout=60.0):
        urls.append(url)
        return _wfs([(663500, 5102500), (664000, 5102500), (664000, 5103000), (665000, 5103000)])

    monkeypatch.setattr(er, "_tn_request", fake)
    tiles = er.tn_tiles("dtm", (664000.0, 5103000.0, 665000.0, 5104000.0))
    assert set(tiles) == {"5h664051030"}  # ostatní se oknu jen dotýkají hranou nebo rohem
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)
    assert q["typeNames"] == ["stem:inqlid2014_dtm_asc"] and q["bbox"] == ["664000.0,5103000.0,665000.0,5104000.0,EPSG:25832"]


@pytest.mark.parametrize("tile, origin", [("5h663050995", (663000.0, 5099500.0)), ("5h716551560", (716500.0, 5156000.0))])
def test_tile_origin(tile, origin):
    assert er.tn_tile_origin(tile) == origin


def test_provider_merges_missing_tiles_in_batches(monkeypatch, tmp_path):
    monkeypatch.setattr(er.settings, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(er, "_buffered", lambda *a: (663000.0, 5099500.0, 664000.0, 5100000.0))
    monkeypatch.setattr(er, "_gdal_tool", lambda name: name)
    monkeypatch.setattr(er, "TN_BATCH", 2)
    monkeypatch.setattr(er, "TN_POLL_S", 0)
    cached = er.tn_tile_dir("5h663050995")
    cached.mkdir(parents=True)
    (cached / "dtm.tif").write_bytes(b"x")  # DTM první dlaždice už v cache je
    merges, polls = [], []

    def fake_request(url, data=None, timeout=60.0):
        if "/wfs?" in url:
            return _wfs([(663000, 5099500), (663500, 5099500)], url)
        if url.endswith("/merge"):
            merges.append(json.loads(data)["files"])
            return json.dumps({"id": len(merges), "token": "t"}).encode()
        if "/file/info?" in url:
            polls.append(url)
            return json.dumps({"state": 1 if len(polls) % 2 == 0 else 0}).encode()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for loc in merges[-1]:
                name = loc.rsplit("/", 1)[1]
                zf.writestr(name, b"inner")
            zf.writestr("../evil.asc.zip", b"x")  # cizí jméno se ignoruje
        return buf.getvalue()

    def fake_run(cmd, log=None):
        with open(cmd[-1] if "gdal_translate" in cmd[0] else cmd[3], "wb") as fh:
            fh.write(b"x")

    monkeypatch.setattr(er, "_tn_request", fake_request)
    monkeypatch.setattr(er, "run_cmd", fake_run)
    out = er.it_trentino_provider((0.0, 0.0, 1.0, 1.0), tmp_path)
    # 2 dlaždice × DTM a DSM, DTM první už v cache → 3 soubory po 2
    assert [len(m) for m in merges] == [2, 1]
    assert out.dtm.name == "it_trentino_dtm.vrt" and out.dsm.name == "it_trentino_dsm.vrt"
    assert all((er.tn_tile_dir(t) / f"{k}.tif").is_file() for t in ("5h663050995", "5h663550995") for k in ("dtm", "dsm"))
    assert not list(tmp_path.glob("tn_merge_*.zip"))  # stažené ZIPy se po převodu mažou


def test_coverage_share(monkeypatch):
    monkeypatch.setattr(er, "_bounds_in", lambda *a: (663000.0, 5099500.0, 664000.0, 5100500.0))
    monkeypatch.setattr(er, "_tn_request", lambda url, data=None, timeout=60.0: _wfs([(663000, 5099500), (663500, 5099500)]))
    assert er.it_trentino_coverage((11.0, 46.0, 11.01, 46.01)) == pytest.approx(0.5)
    assert "it_trentino" in er.COVERAGE
