from __future__ import annotations

import pytest

from app.tiles import TileError, fetch_tile, tile_cache_path, validate_tile

# Minimální platný PNG (1×1) – fetch_tile odmítá ne-PNG / krátká data.
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_validate_tile_ok():
    validate_tile(7, 68, 43)


def test_validate_tile_bad_zoom():
    with pytest.raises(TileError):
        validate_tile(99, 0, 0)


def test_fetch_tile_uses_cache(tmp_path, monkeypatch):
    from app import tiles as tiles_mod

    monkeypatch.setattr(tiles_mod, "CACHE_DIR", tmp_path)
    z, x, y = 2, 1, 1
    dest = tile_cache_path(z, x, y)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(_PNG + b"\x00" * 20)
    assert fetch_tile(z, x, y) == dest


def test_fetch_tile_downloads(tmp_path, monkeypatch):
    from app import tiles as tiles_mod

    monkeypatch.setattr(tiles_mod, "CACHE_DIR", tmp_path)
    payload = _PNG + b"\x00" * 40

    class Resp:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(tiles_mod.urllib.request, "urlopen", lambda *a, **k: Resp())
    path = fetch_tile(1, 0, 0)
    assert path == tile_cache_path(1, 0, 0)
    assert path.exists()
    assert path.read_bytes() == payload
