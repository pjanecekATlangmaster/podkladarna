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
