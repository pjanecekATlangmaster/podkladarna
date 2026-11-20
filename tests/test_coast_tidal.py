"""Moře mimo Česko: pláž, písek, bahno a přílivová zóna jako písčitá půda (213), slaná bažina mokřadem."""

import json

import numpy as np
import pytest

from app.pipeline import coast_tidal, osm_paths


@pytest.mark.parametrize(
    "tags, geom, want",
    [
        ({"natural": "beach"}, "way", ("sand", "213")),
        ({"natural": "sand"}, "way", ("sand", "213")),
        ({"natural": "mud"}, "way", ("sand", "213")),
        ({"natural": "wetland", "wetland": "tidalflat"}, "way", ("sand", "213")),
        ({"natural": "wetland", "wetland": "saltmarsh"}, "way", ("wetland", "308")),  # slaná bažina = bažina
        ({"natural": "beach"}, "node", None),
    ],
)
def test_classify_sand(monkeypatch, tags, geom, want):
    monkeypatch.setattr(osm_paths, "_cz_process", lambda: False)
    assert osm_paths.classify_osm_feature(tags, geom=geom) == want


@pytest.mark.parametrize(
    "tags, want",
    [
        ({"leisure": "pitch", "sport": "beachvolleyball", "natural": "sand"}, None),  # hřiště jako dosud
        ({"natural": "wetland", "wetland": "tidalflat"}, ("wetland", "308")),
    ],
)
def test_classify_sand_cz_unchanged(monkeypatch, tags, want):
    monkeypatch.setattr(osm_paths, "_cz_process", lambda: True)
    got = osm_paths.classify_osm_feature(tags, geom="way")
    assert got != ("sand", "213")
    if want is not None:
        assert got == want


@pytest.mark.parametrize("preset, code", [("forest_10000", "213"), ("sprint_4000", "213"), ("mtbo_10000", "213")])
def test_sand_code_per_set(preset, code):
    assert osm_paths.feature_oom_code("sand", preset, "213") == code


def _grid():
    """Výřez 200 × 100 m po 1 m: moře vpravo (x > 150), u něj holý plochý pruh stoupající k 2 m na pobřežní čáře,
    dál svah do 11 m."""
    h, w = 100, 200
    xs = np.arange(w) + 0.5
    flat = 1.0 + (xs - 100) / 50  # 1 m u horní hrany pruhu, 2 m na pobřeží (hranice přílivu)
    dem = np.tile(np.where(xs > 150, 0.0, np.where(xs > 100, flat, 1.0 + (100 - xs) * 0.1)), (h, 1)).astype("float32")
    chm = np.zeros((h, w), dtype="float32")
    chm[:, 100:110] = 5.0  # porost u horní hrany pruhu
    gt = (0.0, 1.0, 0.0, 100.0, 0.0, -1.0)
    return dem, chm, gt


def test_tidal_mask_connected_low_flat_bare():
    from shapely.geometry import box

    dem, chm, gt = _grid()
    sea = box(150, 0, 200, 100)
    zone, (x0, y_top, step), ref = coast_tidal.tidal_mask(dem, chm, gt, sea, grid_m=2.0)
    assert ref == pytest.approx(2.0, abs=0.1)
    xs = x0 + (np.arange(zone.shape[1]) + 0.5) * step
    cols = zone.mean(axis=0)
    assert cols[(xs > 115) & (xs < 145)].min() > 0.9  # holý nízký pruh u moře
    assert cols[xs < 109].max() == 0  # porost a vyšší svah ne
    assert cols[xs > 152].max() == 0  # moře samo ne


def test_tidal_mask_needs_connection_to_sea():
    from shapely.geometry import box

    dem, chm, gt = _grid()
    dem[:, 110:140] = 0.5  # nízká plocha …
    dem[:, 140:146] = 4.0  # … oddělená hrází
    zone, (x0, _y, step), _ref = coast_tidal.tidal_mask(dem, chm, gt, box(150, 0, 200, 100), grid_m=2.0)
    xs = x0 + (np.arange(zone.shape[1]) + 0.5) * step
    assert zone[:, (xs > 112) & (xs < 138)].sum() == 0  # polder za hrází není přílivová zóna


def test_tidal_mask_skips_built_up_and_roads():
    from shapely.geometry import LineString, box

    dem, chm, gt = _grid()
    sea = box(150, 0, 200, 100)
    feats = [{"type": "Feature", "properties": {"kind": "parking"}, "geometry": box(110, 0, 150, 50).__geo_interface__}]
    built = coast_tidal._built_geometry(feats, None).union(LineString([(130, 50), (130, 100)]).buffer(3))
    zone, (x0, y_top, step), _ref = coast_tidal.tidal_mask(dem, chm, gt, sea, grid_m=2.0, built=built)
    xs = x0 + (np.arange(zone.shape[1]) + 0.5) * step
    ys = y_top - (np.arange(zone.shape[0]) + 0.5) * step
    assert zone[np.ix_(ys < 48, (xs > 112) & (xs < 148))].sum() == 0  # nábřeží s parkovištěm ne
    assert zone[np.ix_(ys > 52, (xs > 112) & (xs < 126))].sum() == 0  # za silnicí bez napojení na moře ne
    assert zone[np.ix_(ys > 52, (xs > 136) & (xs < 148))].mean() > 0.9  # u moře před silnicí ano


def test_apply_clips_beach_by_sea_and_skips_without_dem(tmp_path):
    from shapely.geometry import box, mapping, shape

    d = tmp_path / "osm_paths"
    d.mkdir()
    feats = [
        {"type": "Feature", "properties": {"kind": "water_body", "oom_code": "301", "sea": True},
         "geometry": mapping(box(150, 0, 200, 100))},
        {"type": "Feature", "properties": {"kind": "sand", "oom_code": "213"}, "geometry": mapping(box(120, 0, 170, 100))},
    ]
    (d / "features.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    assert coast_tidal.apply_tidal_zone(tmp_path) == 0
    out = json.loads((d / "features.geojson").read_text(encoding="utf-8"))["features"]
    sand = [shape(f["geometry"]) for f in out if f["properties"]["kind"] == "sand"]
    assert sum(g.area for g in sand) == pytest.approx(3000)  # pláž jen na pevnině
