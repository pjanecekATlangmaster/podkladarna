"""Testy masek pro odečet KP 401."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.pipeline.open_land_subtract import (
    collect_kp401_subtract_wkbs,
    _shp_wkbs,
)


def test_collect_empty_does_not_require_pyogrio(tmp_path: Path):
    assert collect_kp401_subtract_wkbs(zabaged_clean=None, work_dir=tmp_path) == []


def test_collect_osm_farmland(tmp_path: Path):
    pytest.importorskip("osgeo.ogr")
    osm = tmp_path / "osm_paths"
    osm.mkdir()
    (osm / "features.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"kind": "farmland"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
                            ],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {"kind": "meadow"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[0, 0], [5, 0], [5, 5], [0, 5], [0, 0]]
                            ],
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    wkbs = collect_kp401_subtract_wkbs(zabaged_clean=None, work_dir=tmp_path)
    assert len(wkbs) == 1


def _write_square_shp(shp: Path) -> None:
    from osgeo import ogr

    driver = ogr.GetDriverByName("ESRI Shapefile")
    ds = driver.CreateDataSource(str(shp))
    layer = ds.CreateLayer(shp.stem, geom_type=ogr.wkbPolygon)
    feat = ogr.Feature(layer.GetLayerDefn())
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((0, 0), (20, 0), (20, 20), (0, 20), (0, 0)):
        ring.AddPoint(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    feat.SetGeometry(poly)
    layer.CreateFeature(feat)
    ds = None


def test_shp_wkbs_via_ogr(tmp_path: Path):
    ogr = pytest.importorskip("osgeo.ogr")
    shp = tmp_path / "poly.shp"
    _write_square_shp(shp)
    wkbs = _shp_wkbs(shp)
    assert len(wkbs) == 1
    geom = ogr.CreateGeometryFromWkb(wkbs[0])
    assert geom.GetArea() == pytest.approx(400.0)


def test_zabaged_layers_via_ogr(tmp_path: Path):
    pytest.importorskip("osgeo.ogr")
    layer_dir = tmp_path / "src"
    layer_dir.mkdir()
    _write_square_shp(layer_dir / "TrvalyTravniPorost.shp")

    zabaged = tmp_path / "zabaged_clean.zip"
    with zipfile.ZipFile(zabaged, "w") as zf:
        for path in layer_dir.iterdir():
            zf.write(path, path.name)

    wkbs = collect_kp401_subtract_wkbs(
        zabaged_clean=zabaged, work_dir=tmp_path / "work"
    )
    assert len(wkbs) == 1
