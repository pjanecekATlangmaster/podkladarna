"""Polsko: dotazy WCS GUGiK a čtení NMPT z ASCII odpovědi (multipart)."""

from __future__ import annotations

import pytest

from app.pipeline import elevation_raster as er
from app.pipeline.reference_pl import pl_dsm_url, pl_dtm_url

RAW = (
    b"\r\n--wcs\nContent-Type: image/x-aaigrid\nContent-ID: coverage/result.asc\n\n"
    b"ncols        3\nnrows        2\nxllcorner    638000.000\nyllcorner    486000.000\ncellsize     1.000\n"
    b"NODATA_value -9999\n 1.5 2.5 3.5\n 4.5 -9999 6.5\n"
    b"--wcs\nContent-Type: application/octet-stream\n\nPROJCS[\"ETRS89_Poland_CS92\"]\n--wcs--\n"
)


def test_urls_tiff_terrain_and_ascii_surface_at_1m():
    dtm = pl_dtm_url((638000, 486000, 640000, 488000))
    assert "COVERAGEID=DTM_PL-KRON86-NH_TIFF" in dtm and "FORMAT=image/tiff" in dtm
    assert "SUBSET=x(638000,640000)&SUBSET=y(486000,488000)" in dtm
    dsm = pl_dsm_url((638000, 486000, 639000, 487000))
    assert "COVERAGEID=DSM_PL-KRON86-NH" in dsm and "image/x-aaigrid" in dsm and "SCALEFACTOR=0.5" in dsm


def test_surface_tiles_at_most_1km():
    boxes = er.pl_tile_boxes((638000.0, 486000.0, 640500.0, 487000.0), er.PL_DSM_SIDE_M)
    assert len(boxes) == 6 and all(b[2] - b[0] <= 1000 and b[3] - b[1] <= 1000 for b in boxes)


def test_parse_multipart_aaigrid():
    grid, gt, nodata = er.parse_wcs_aaigrid(RAW)
    assert grid.shape == (2, 3) and grid[0].tolist() == [1.5, 2.5, 3.5]
    assert gt == (638000.0, 1.0, 0.0, 486002.0, 0.0, -1.0) and nodata == -9999.0


def test_parse_rejects_non_grid():
    with pytest.raises(RuntimeError, match="ASCII"):
        er.parse_wcs_aaigrid(b"<ServiceException>busy</ServiceException>")


class _Resp:
    def __init__(self, body: bytes):
        self._parts = [body] if body else []

    def read(self, _n):
        return self._parts.pop(0) if self._parts else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_surface_tile_retries_empty_reply(monkeypatch, tmp_path):
    """Prázdná odpověď 200 (server zahodil souběžný dotaz) se zkusí znovu (review PR 351)."""
    bodies = [b"", RAW]
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: _Resp(bodies.pop(0)))
    monkeypatch.setattr("time.sleep", lambda s: None)
    dest = er._pl_dsm_tile("https://example.invalid/wcs", tmp_path / "t.tif")
    assert dest.is_file() and not bodies


def test_surface_tile_gives_up_after_deadline(monkeypatch, tmp_path):
    monkeypatch.setattr(er, "PL_DSM_TILE_DEADLINE_S", 0.0)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: _Resp(b""))
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises((TimeoutError, RuntimeError)):
        er._pl_dsm_tile("https://example.invalid/wcs", tmp_path / "t.tif")
