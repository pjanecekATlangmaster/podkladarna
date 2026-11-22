"""Tests for CHM vegetation classification."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import numpy as np

from app.pipeline.vegetation_chm import (
    CHM_GREEN_DENSE_MAX_M,
    CHM_GREEN_LIGHT_MAX_M,
    CHM_GREEN_MID_MAX_M,
    CHM_OPEN_MAX_M,
    OPEN_REINFORCE_ITERS,
    WHITE_EDGE_STRICT_M,
    WHITE_MORPH_OPEN_ITERS,
    ChmVegeThresholds,
    classify_chm_array,
    cleanup_white_forest,
    generate_job_vegetation_chm,
    reinforce_open_land,
)


def test_default_thresholds_calibrated_vs_kp():
    """Po opravě CHM (louka = 0 m): pásy zeleně v zónách 1–5.5 m."""
    assert CHM_OPEN_MAX_M <= 1.5
    assert CHM_OPEN_MAX_M < CHM_GREEN_LIGHT_MAX_M < CHM_GREEN_MID_MAX_M
    assert 5.0 <= CHM_GREEN_DENSE_MAX_M <= 8.0
    assert WHITE_EDGE_STRICT_M >= CHM_GREEN_DENSE_MAX_M + 5.0
    assert WHITE_MORPH_OPEN_ITERS >= 1
    assert OPEN_REINFORCE_ITERS >= 0


def test_classify_open_vs_white_thresholds():
    """Louka (nízké CHM) → 401; vysoký porost → bílý les (0), ne bleed."""
    thr = ChmVegeThresholds(median_size=0, open_reinforce_iters=0)
    # meadow – CHM residual trávy pod open_max
    assert thr.classify_height(0.3) == 1
    assert thr.classify_height(CHM_OPEN_MAX_M - 0.01) == 1
    # scrub / greens (úzké pásy nad open)
    assert thr.classify_height(CHM_OPEN_MAX_M + 0.2) == 2
    assert thr.classify_height((CHM_GREEN_LIGHT_MAX_M + CHM_GREEN_MID_MAX_M) / 2) == 3
    assert thr.classify_height((CHM_GREEN_MID_MAX_M + CHM_GREEN_DENSE_MAX_M) / 2) == 4
    # white forest – above dense max, not open
    assert thr.classify_height(CHM_GREEN_DENSE_MAX_M) == 0
    assert thr.classify_height(20.0) == 0


def test_classify_chm_array_bands():
    thr = ChmVegeThresholds(median_size=0, open_reinforce_iters=0)
    chm = np.array(
        [
            [0.5, thr.open_max_m + 0.3, thr.green_light_max_m + 0.3],
            [thr.green_mid_max_m + 0.5, thr.green_dense_max_m + 2.0, -9999.0],
        ],
        dtype=np.float32,
    )
    out = classify_chm_array(chm, nodata=-9999.0, thresholds=thr)
    assert out[0, 0] == 1  # open
    assert out[0, 1] == 2  # light green
    assert out[0, 2] == 3  # mid
    assert out[1, 0] == 4  # dense
    # white-range single pixel u open → 401 (strom v louce) nebo 0/4
    assert out[1, 1] in (0, 1, 4)
    assert out[1, 2] == 0  # nodata


def test_white_tree_in_meadow_becomes_open():
    """Izolovaná koruna uprostřed louky → 401 (ne bílý les / 410)."""
    h = np.full((5, 5), 0.5, dtype=np.float32)
    h[2, 2] = 16.0  # above dense max, below edge-strict
    classified = np.ones((5, 5), dtype=np.uint8)
    classified[2, 2] = 0
    thr = ChmVegeThresholds(
        green_dense_max_m=12.0,
        white_edge_strict_m=22.0,
        white_morph_iters=2,
        median_size=0,
        open_reinforce_iters=0,
    )
    out = cleanup_white_forest(classified, h, thresholds=thr)
    assert out[2, 2] == 1


def test_white_forest_edge_demoted_to_dense_not_open():
    """Okraj bílého lesa do louky → 410, ne roztažení 401 do lesa."""
    h = np.full((7, 7), 0.5, dtype=np.float32)
    h[:, 3:] = 16.0  # right half = canopy below edge-strict
    classified = np.ones((7, 7), dtype=np.uint8)
    classified[:, 3:] = 0
    thr = ChmVegeThresholds(
        green_dense_max_m=12.0,
        white_edge_strict_m=22.0,
        white_morph_iters=1,
        median_size=0,
        open_reinforce_iters=0,
    )
    out = cleanup_white_forest(classified, h, thresholds=thr)
    # na styku louka|les: demoted → 410; hlouběji v „lese“ může zůstat 0/4
    assert out[3, 3] in (0, 4)
    assert out[3, 0] == 1


def test_white_core_kept_when_strict_and_clustered():
    """Souvislý vysoký porost (≥ edge strict) zůstane bílý."""
    h = np.full((9, 9), 0.5, dtype=np.float32)
    h[2:7, 2:7] = 25.0
    classified = np.ones((9, 9), dtype=np.uint8)
    classified[2:7, 2:7] = 0
    thr = ChmVegeThresholds(
        green_dense_max_m=12.0,
        white_edge_strict_m=22.0,
        white_morph_iters=2,
        median_size=0,
        open_reinforce_iters=0,
    )
    out = cleanup_white_forest(classified, h, thresholds=thr)
    assert out[4, 4] == 0


def test_reinforce_open_pulls_scrub_in_meadow():
    """Nízký 406 uprostřed louky → 401."""
    h = np.full((7, 7), 1.0, dtype=np.float32)
    classified = np.ones((7, 7), dtype=np.uint8)
    classified[3, 3] = 2  # light green speck
    h[3, 3] = 4.5
    thr = ChmVegeThresholds(
        open_max_m=4.0,
        green_light_max_m=5.5,
        green_mid_max_m=8.5,
        open_reinforce_iters=2,
        median_size=0,
    )
    out = reinforce_open_land(classified, h, thresholds=thr)
    assert out[3, 3] == 1


def test_median_smooth_reduces_salt(tmp_path: Path):
    """Median filtr: izolovaný pixel zeleně v louce zmizí."""
    del tmp_path
    chm = np.full((9, 9), 0.3, dtype=np.float32)  # open
    chm[4, 4] = CHM_OPEN_MAX_M + 0.8  # single light-green speck
    out = classify_chm_array(
        chm, nodata=None, thresholds=ChmVegeThresholds(median_size=5)
    )
    assert out[4, 4] == 1  # smoothed / reinforced back to open


def test_generate_job_vegetation_chm_missing(tmp_path: Path):
    assert generate_job_vegetation_chm(tmp_path) is None


def test_generate_job_vegetation_chm_calls_generator(tmp_path: Path):
    dem = tmp_path / "dem"
    dem.mkdir()
    chm = dem / "chm.tif"
    chm.write_bytes(b"chm" * 200)
    dest_marker = tmp_path / "vegetation" / "vegetation.shp"

    def fake_gen(
        src,
        dest,
        *,
        thresholds=None,
        tint_png=None,
        log=None,
        veg_size_profile="default",
    ):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"shp")
        if tint_png:
            tint_png.write_bytes(b"png")
        return dest

    with patch(
        "app.pipeline.vegetation_chm.generate_vegetation_from_chm",
        side_effect=fake_gen,
    ):
        out = generate_job_vegetation_chm(tmp_path)

    assert out == dest_marker
    assert dest_marker.is_file()


def _cover_feat(cover, ring, hole=None):
    rings = [ring] + ([hole] if hole else [])
    return {"type": "Feature", "properties": {"cover": cover}, "geometry": {"type": "Polygon", "coordinates": rings}}


@pytest.mark.parametrize(
    "feats, px, expected",
    [
        # les z OSM → maska, mimo les ne
        ([_cover_feat("forest", [[0, 0], [10, 0], [10, 10], [0, 10]])], (5, -5), True),
        ([_cover_feat("forest", [[0, 0], [10, 0], [10, 10], [0, 10]])], (15, -5), False),
        # díra v lese (mýtina) a louka uvnitř lesa zůstávají otevřené
        ([_cover_feat("wood", [[0, 0], [20, 0], [20, 20], [0, 20]], [[8, 8], [12, 8], [12, 12], [8, 12]])], (10, -10), False),
        ([_cover_feat("wood", [[0, 0], [20, 0], [20, 20], [0, 20]]),
          _cover_feat("meadow", [[8, 8], [12, 8], [12, 12], [8, 12]])], (10, -10), False),
        # vřesoviště samo les není
        ([_cover_feat("heath", [[0, 0], [10, 0], [10, 10], [0, 10]])], (5, -5), None),
    ],
)
def test_leafoff_forest_mask(tmp_path: Path, feats, px, expected):
    import json

    from app.pipeline.vegetation_chm import leafoff_forest_mask

    (tmp_path / "osm_paths").mkdir()
    (tmp_path / "osm_paths" / "natural_cover.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    # mřížka 1 m, levý horní roh (0, 20) → řádek = 20 − y
    mask = leafoff_forest_mask(tmp_path, (0.0, 1.0, 0.0, 20.0, 0.0, -1.0), (25, 25))
    if expected is None:
        assert mask is None
    else:
        x, y = px
        assert bool(mask[20 + y, x]) is expected


def test_regenerate_leafoff_only_for_chm_vegetation(tmp_path: Path):
    from app.pipeline.vegetation_chm import regenerate_leafoff_vegetation

    (tmp_path / "dem").mkdir()
    (tmp_path / "dem" / "chm.tif").write_bytes(b"chm" * 200)
    # vegetace z hustoty bodů (bez značky CHM) se nepřepisuje
    with patch("app.pipeline.vegetation_chm.generate_vegetation_from_chm") as gen:
        assert regenerate_leafoff_vegetation(tmp_path) is None
    gen.assert_not_called()


@pytest.mark.parametrize(
    "texture, mode, share",
    [(True, "laser", None), ("understory", "understory", None), ("understory:0.25", "understory", 0.25),
     ("understory:x", "understory", None)],
)
def test_texture_mode_and_share(texture, mode, share):
    from app.pipeline.vegetation_chm import texture_mode, texture_share

    assert texture_mode(texture) == mode and texture_share(texture) == share
