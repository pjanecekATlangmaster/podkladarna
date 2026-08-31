from __future__ import annotations

import pytest

from app.tiles import TileError, fetch_tile, validate_tile


def test_validate_tile_ok():
    validate_tile(7, 68, 43)


def test_validate_tile_bad_zoom():
    with pytest.raises(TileError):
        validate_tile(99, 0, 0)


def test_fetch_tile_uses_cache(tmp_path, monkeypatch):
    from app import tiles as tiles_mod

    monkeypatch.setattr(tiles_mod, "CACHE_DIR", tmp_path)
    z, x, y = 2, 1, 1
    dest = tmp_path / "tiles" / "2" / "1" / "1.png"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"cached-png-bytes-xxxx")
    assert fetch_tile(z, x, y) == dest


def test_fetch_tile_downloads(tmp_path, monkeypatch):
    from app import tiles as tiles_mod

    monkeypatch.setattr(tiles_mod, "CACHE_DIR", tmp_path)

    class Resp:
        def read(self):
            return b"x" * 80

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(tiles_mod.urllib.request, "urlopen", lambda *a, **k: Resp())
    path = fetch_tile(1, 0, 0)
    assert path.exists()
    assert path.read_bytes() == b"x" * 80
