"""Anglie: dlaždice 5 km mřížky OS a stažení ZIPu Environment Agency do cache."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.pipeline import elevation_raster as er


@pytest.mark.parametrize(
    ("east", "north", "tile"),
    [
        (427000, 432000, "SE2530"),  # Leeds (sonda)
        (357500, 173000, "ST5570"),  # Bristol
        (530000, 180000, "TQ3080"),  # Londýn
        (356000, 286000, "SO5585"),  # Shropshire
    ],
)
def test_os_grid_tile(east, north, tile):
    assert er.os_grid_tile(east, north) == tile


def test_tiles_cover_window_across_tile_edge():
    assert sorted(er.gb_ea_tiles((354000.0, 284000.0, 356000.0, 286500.0))) == ["SO5080", "SO5085", "SO5580", "SO5585"]


def test_tile_url():
    assert er.gb_ea_tile_url("dsm", "ST5570").endswith("/lidar_composite_first_return_dsm/2022/1/ST5570")


class _Resp(io.BytesIO):
    def __init__(self, body: bytes, ctype: str = "application/zip"):
        super().__init__(body)
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_tile_extracted_to_cache_and_reused(monkeypatch, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ST57se_DTM_1m.tfw", "1\n0\n0\n-1\n355000.5\n174999.5\n")
        zf.writestr("ST57se_DTM_1m.tif", b"II*\x00fake")
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(req.full_url)
        return _Resp(buf.getvalue())

    monkeypatch.setattr(er.settings, "CACHE_DIR", tmp_path)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    first = er._gb_ea_tile("dtm", "ST5570")
    assert first == tmp_path / "gb_ea" / "ST5570" / "dtm.tif" and first.read_bytes() == b"II*\x00fake"
    assert er._gb_ea_tile("dtm", "ST5570") == first and len(calls) == 1
    assert not list((tmp_path / "gb_ea" / "ST5570").glob(".*.zip")) and not list((tmp_path / "gb_ea" / "ST5570").glob(".*.tif"))  # žádné dočasné soubory


def _empty_zip() -> bytes:
    buf = io.BytesIO()
    zipfile.ZipFile(buf, "w").close()
    return buf.getvalue()


def test_empty_zip_outside_coverage_is_none_and_remembered(monkeypatch, tmp_path):
    """Moře / Wales / Skotsko: 200 s prázdným ZIPem (22 B) → None a značka, podruhé se nestahuje (review PR 352)."""
    calls = []
    monkeypatch.setattr(er.settings, "CACHE_DIR", tmp_path)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: calls.append(1) or _Resp(_empty_zip()))
    assert len(_empty_zip()) == 22
    assert er._gb_ea_tile("dtm", "SW0010") is None
    assert er._gb_ea_tile("dtm", "SW0010") is None and len(calls) == 1
    assert (tmp_path / "gb_ea" / "SW0010" / "dtm.none").is_file()


def test_html_reply_is_transient_not_outside(monkeypatch, tmp_path):
    """HTML („Cannot GET“) se zkusí znovu, neuloží se jako mimo pokrytí."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x.tif", b"II*\x00ok")
    replies = [_Resp(b"<html>Cannot GET</html>", "text/html"), _Resp(buf.getvalue())]
    monkeypatch.setattr(er.settings, "CACHE_DIR", tmp_path)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: replies.pop(0))
    monkeypatch.setattr("time.sleep", lambda s: None)
    tile = er._gb_ea_tile("dsm", "ST5570")
    assert tile is not None and tile.read_bytes() == b"II*\x00ok" and not replies
    assert not (tmp_path / "gb_ea" / "ST5570" / "dsm.none").exists()


@pytest.mark.parametrize(
    ("bbox", "blocked"),
    [
        ((-2.6128, 51.4523, -2.5928, 51.4654), False),  # Bristol, 1 dlaždice
        ((-2.62, 51.43, -2.48, 51.52), True),  # ~10 × 10 km, přes 3 dlaždice
    ],
)
def test_area_probe_blocks_too_many_tiles(bbox, blocked):
    from app import area_probe, countries

    country = countries.load_countries()["GB-ENG"]
    result = area_probe.probe(country, bbox, deadline_s=1.0, include_dtm=False)
    assert (result["block"] is not None) is blocked
    if blocked:
        assert result["block"].key == "api.sheets.too_many_tiles"
