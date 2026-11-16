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
