"""Polsko: dotazy WCS GUGiK a čtení NMPT z ASCII odpovědi (multipart)."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

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


@pytest.fixture
def fake_gdal(monkeypatch):
    """Stub osgeo: CI ani cloud nemají GDAL pro Python, test ověřuje jen opakování dotazu."""
    gdal = MagicMock()
    osgeo = types.ModuleType("osgeo")
    osgeo.gdal, osgeo.osr = gdal, MagicMock()
    monkeypatch.setitem(sys.modules, "osgeo", osgeo)
    monkeypatch.setitem(sys.modules, "osgeo.gdal", osgeo.gdal)
    monkeypatch.setitem(sys.modules, "osgeo.osr", osgeo.osr)
    return gdal


def test_surface_tile_retries_empty_reply(monkeypatch, tmp_path, fake_gdal):
    """Prázdná odpověď 200 (server zahodil souběžný dotaz) se zkusí znovu (review PR 351)."""
    bodies = [b"", RAW]
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: _Resp(bodies.pop(0)))
    monkeypatch.setattr("time.sleep", lambda s: None)
    er._pl_dsm_tile("https://example.invalid/wcs", tmp_path / "t.tif")
    assert not bodies
    create = fake_gdal.GetDriverByName.return_value.Create
    assert create.call_count == 1 and create.call_args.args[1:3] == (3, 2)


def test_surface_tile_gives_up_after_deadline(monkeypatch, tmp_path, fake_gdal):
    monkeypatch.setattr(er, "PL_DSM_TILE_DEADLINE_S", 0.0)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: _Resp(b""))
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises((TimeoutError, RuntimeError)):
        er._pl_dsm_tile("https://example.invalid/wcs", tmp_path / "t.tif")


# Záloha při výpadku WCS (2026-11-23): listy opendata GUGiK podle indexu WFS.
from app.pipeline.reference_pl import choose_sheets, parse_sheet_index, pl_sheet_index_url  # noqa: E402

WFS = b"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:gugik="http://www.gugik.gov.pl">
<wfs:member><gugik:SkorowidzNMT2025><gugik:godlo>M-34-64-D-d-1-4</gugik:godlo><gugik:akt_data>2025-04-01</gugik:akt_data>
<gugik:url_do_pobrania>https://opendata.geoportal.gov.pl/NumDaneWys/NMT/83137/83137_1729554_M-34-64-D-d-1-4.asc</gugik:url_do_pobrania>
<gugik:zrodlo_danych>Skaning laserowy</gugik:zrodlo_danych></gugik:SkorowidzNMT2025></wfs:member>
<wfs:member><gugik:SkorowidzNMT2025><gugik:godlo>7.125.11.12</gugik:godlo><gugik:akt_data>2025-04-01</gugik:akt_data>
<gugik:url_do_pobrania>https://opendata.geoportal.gov.pl/NumDaneWys/NMT/1/1_2_7.125.11.12.asc</gugik:url_do_pobrania>
<gugik:zrodlo_danych>Skaning laserowy</gugik:zrodlo_danych></gugik:SkorowidzNMT2025></wfs:member>
<wfs:member><gugik:SkorowidzNMT2025><gugik:godlo>M-34-64-D-d-1-2</gugik:godlo><gugik:akt_data>2025-04-01</gugik:akt_data>
<gugik:url_do_pobrania>https://opendata.geoportal.gov.pl/NumDaneWys/NMT/1/1_3_M-34-64-D-d-1-2.xyz.zip</gugik:url_do_pobrania>
<gugik:zrodlo_danych>Skaning laserowy</gugik:zrodlo_danych></gugik:SkorowidzNMT2025></wfs:member>
</wfs:FeatureCollection>"""


def test_sheet_index_url_north_east_bbox():
    url = pl_sheet_index_url("nmpt", 2025, (566000, 244000, 567000, 245000))
    assert "NumerycznyModelPokryciaTerenuEVRF2007/WFS/Skorowidze" in url and "TYPENAMES=gugik:SkorowidzNMPT2025" in url
    assert "BBOX=244000,566000,245000,567000,urn:ogc:def:crs:EPSG::2180" in url


def test_sheet_index_keeps_only_pl1992_ascii_on_opendata():
    recs = parse_sheet_index(WFS, 2025)
    assert [r["godlo"] for r in recs] == ["M-34-64-D-d-1-4"] and recs[0]["laser"]


@pytest.mark.parametrize(
    "recs, want",
    [
        # laser před snímky i když je starší
        ([{"godlo": "A", "year": 2024, "date": "", "laser": False, "url": "foto"},
          {"godlo": "A", "year": 2021, "date": "", "laser": True, "url": "laser"}], ["laser"]),
        # mezi laserovými nejnovější
        ([{"godlo": "A", "year": 2023, "date": "", "laser": True, "url": "old"},
          {"godlo": "A", "year": 2025, "date": "", "laser": True, "url": "new"},
          {"godlo": "B", "year": 2020, "date": "", "laser": True, "url": "b"}], ["new", "b"]),
    ],
)
def test_choose_sheets_one_per_sheet(recs, want):
    assert [r["url"] for r in choose_sheets(recs)] == want


def test_asc_extent_center_and_corner(tmp_path):
    p = tmp_path / "a.asc"
    p.write_text("ncols 4\nnrows 2\nxllcenter 100.25\nyllcenter 200.25\ncellsize 0.5\nnodata_value -9999\n")
    assert er._asc_extent(p) == (100.0, 200.0, 102.0, 201.0, 0.5)


def test_provider_uses_sheets_when_wcs_down(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(er, "_pl_wcs_alive", lambda log=None: False)
    monkeypatch.setattr(er, "pl_sheet_rasters", lambda kind, b, d, log=None: calls.append(kind) or [d / f"{kind}.tif"])
    monkeypatch.setattr(er, "_bounds_in", lambda b, s, t: (566000.0, 244000.0, 567000.0, 245000.0))
    monkeypatch.setattr(er, "run_cmd", lambda cmd, log=None: None)
    monkeypatch.setattr(er, "_gdal_tool", lambda name: name)
    monkeypatch.setattr(er, "_download_retry", lambda *a, **k: pytest.fail("WCS se při výpadku nevolá"))
    res = er.pl_gugik_provider((0, 0, 1, 1), tmp_path)
    assert calls == ["nmt", "nmpt"] and res.dsm is not None
