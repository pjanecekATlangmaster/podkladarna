"""Tests for LiDAR return-density vegetation (bez KP, KP-inspired makevege)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from app.pipeline.vegetation_density import (
    DEFAULT_PARAMS,
    DENSE,
    DensityVegeParams,
    MID,
    OPEN,
    WHITE,
    LidarPoints,
    _box_sum,
    classify_points,
    generate_job_vegetation_density,
    median_filter_uint8,
    reclaim_yellow_under_canopy,
    yellow_mask_from_hits,
)

SIZE = 60  # m, 1 m grid
GT = (1000.0, 1.0, 0.0, 2000.0, 0.0, -1.0)


def _dem():
    return np.full((SIZE, SIZE), 500.0, dtype=np.float32)


def _grid_points(x0, x1, y0, y1, per_m2, z_agl, cls, rng, *, nret=1, rn=1):
    area = (x1 - x0) * (y1 - y0)
    n = int(area * per_m2)
    x = rng.uniform(GT[0] + x0, GT[0] + x1, n)
    y = rng.uniform(GT[3] - y1, GT[3] - y0, n)
    z = np.full(n, 500.0, dtype=np.float32) + np.asarray(z_agl(n), dtype=np.float32)
    return x, y, z, np.full(n, cls, np.uint8), np.full(n, rn, np.uint8), np.full(n, nret, np.uint8)


def _pts(*chunks):
    cols = list(zip(*chunks))
    return LidarPoints(*(np.concatenate(c) for c in cols))


def _ground(rng, x0=0, x1=SIZE, y0=0, y1=SIZE, per_m2=0.4):
    return _grid_points(x0, x1, y0, y1, per_m2, lambda n: np.zeros(n), 2, rng, nret=2, rn=2)


def test_meadow_is_open():
    rng = np.random.default_rng(1)
    grass = _grid_points(0, SIZE, 0, SIZE, 0.2, lambda n: rng.uniform(0, 0.5, n), 5, rng)
    out = classify_points(_pts(_ground(rng), grass), _dem(), GT)
    inner = out[8:-8, 8:-8]
    assert np.mean(inner == OPEN) > 0.98


def test_tall_canopy_without_undergrowth_is_white():
    """Vzrostlý les: first returns jen v koruně → bílý, ne zelený (CHM chyba)."""
    rng = np.random.default_rng(2)
    canopy = _grid_points(0, SIZE, 0, SIZE, 8.0, lambda n: rng.uniform(18, 25, n), 5, rng)
    out = classify_points(_pts(_ground(rng), canopy), _dem(), GT)
    inner = out[8:-8, 8:-8]
    assert np.mean(inner == WHITE) > 0.95


def test_low_thicket_is_green_and_denser_is_darker():
    rng = np.random.default_rng(3)
    thicket = _grid_points(0, SIZE, 0, SIZE, 8.0, lambda n: rng.uniform(1.0, 2.5, n), 5, rng)
    out = classify_points(_pts(_ground(rng), thicket), _dem(), GT)
    inner = out[8:-8, 8:-8]
    assert np.mean(np.isin(inner, (MID, DENSE))) > 0.9
    # 410 potřebuje i odrazy nad greenhigh (2 m) – topweight člen jako KP.
    wall = _grid_points(0, SIZE, 0, SIZE, 8.0, lambda n: rng.uniform(1.0, 3.4, n), 5, rng)
    out2 = classify_points(_pts(_ground(rng), wall), _dem(), GT)
    assert np.mean(out2[8:-8, 8:-8] == DENSE) > 0.9


def test_single_tree_in_meadow_does_not_become_forest_blob():
    rng = np.random.default_rng(4)
    grass = _grid_points(0, SIZE, 0, SIZE, 0.2, lambda n: rng.uniform(0, 0.4, n), 5, rng)
    tree = _grid_points(29, 32, 29, 32, 10.0, lambda n: rng.uniform(10, 15, n), 5, rng)
    out = classify_points(_pts(_ground(rng, per_m2=2.0), grass, tree), _dem(), GT)
    # okno žluté ~6 m + canopy close: okolí louka, ne bílý blob
    assert np.sum(out[8:-8, 8:-8] != OPEN) < 150
    assert out[10, 10] == OPEN and out[50, 50] == OPEN


def test_park_trees_in_meadow_stay_open_not_white():
    """ČÚZK-like: řídký ground + husté DMP koruny 14–20 m v louce → pořád 401.

    Bez canopy close / reclaim by husté first-return koruny „vybílily“ louku
    (Barr7 / OSM way 1081943349). Solidní bílý les řeší jiný test.
    """
    rng = np.random.default_rng(11)
    ground = _ground(rng, per_m2=0.35)
    # několik stromů ~8 m od sebe, hustá koruna jako DMP class 5
    chunks = [ground]
    for x0, y0 in ((12, 12), (12, 28), (12, 44), (28, 12), (28, 28), (28, 44), (44, 20), (44, 36)):
        chunks.append(
            _grid_points(
                x0,
                x0 + 3,
                y0,
                y0 + 3,
                25.0,
                lambda n: rng.uniform(14, 20, n),
                5,
                rng,
            )
        )
    out = classify_points(_pts(*chunks), _dem(), GT)
    inner = out[8:-8, 8:-8]
    assert np.mean(inner == OPEN) > 0.75
    assert np.mean(inner == WHITE) < 0.2


def test_reclaim_yellow_under_canopy_only_inside_open():
    classified = np.zeros((5, 5), dtype=np.uint8)
    classified[:, :3] = OPEN
    classified[2, 1] = WHITE  # uvnitř žluté
    classified[2, 4] = WHITE  # v bílém lese
    out = reclaim_yellow_under_canopy(classified, frac_min=0.55)
    assert out[2, 1] == OPEN
    assert out[2, 4] == WHITE


def test_yellow_mask_canopy_close_fills_tree_hole():
    """Hole-fill vyplní malou díru po stromu; velký lesní ostrov nechá."""
    # --- malá koruna uprostřed čisté louky ---
    yhit = np.ones((36, 36), dtype=float) * 10
    noyhit = np.zeros((36, 36), dtype=float)
    yhit[16:19, 16:19] = 1
    noyhit[16:19, 16:19] = 40
    params = DensityVegeParams(yellow_canopy_close_m=6.0, yellow_median_size=0)
    yellow = yellow_mask_from_hits(yhit, noyhit, shape=(36, 36), res=1.0, params=params)
    assert yellow[17, 17]
    assert yellow.mean() > 0.95

    # --- velký uzavřený lesní ostrov (> max hole) zůstane ne-žlutý ---
    yhit2 = np.ones((48, 48), dtype=float) * 10
    noyhit2 = np.zeros((48, 48), dtype=float)
    yhit2[12:36, 12:36] = 1
    noyhit2[12:36, 12:36] = 40
    yellow2 = yellow_mask_from_hits(yhit2, noyhit2, shape=(48, 48), res=1.0, params=params)
    assert not yellow2[24, 24]
    assert yellow2[2, 2]


def test_points_outside_grid_are_ignored():
    rng = np.random.default_rng(5)
    x, y, z, c, r, n = _ground(rng)
    x = np.concatenate([x, [GT[0] - 50.0]])
    y = np.concatenate([y, [GT[3] + 50.0]])
    z = np.concatenate([z, [600.0]]).astype(np.float32)
    c, r, n = (np.concatenate([a, a[:1]]) for a in (c, r, n))
    out = classify_points(LidarPoints(x, y, z, c, r, n), _dem(), GT)
    assert out.shape == (SIZE, SIZE)


def test_box_sum_matches_naive():
    rng = np.random.default_rng(6)
    a = rng.integers(0, 5, (13, 17)).astype(float)
    for size in (1, 3, 6):
        got = _box_sum(a, size)
        before = (size - 1) // 2
        after = size - 1 - before
        p = np.pad(a, ((before, after), (before, after)))
        want = np.array(
            [[p[i : i + size, j : j + size].sum() for j in range(a.shape[1])] for i in range(a.shape[0])]
        )
        assert np.allclose(got, want)


def test_median_filter_chunking_is_seamless():
    rng = np.random.default_rng(7)
    a = rng.integers(0, 5, (50, 40)).astype(np.uint8)
    assert np.array_equal(
        median_filter_uint8(a, 7, rows_per_chunk=7), median_filter_uint8(a, 7, rows_per_chunk=500)
    )


def test_defaults_match_kp_base_ini():
    """Kalibrováno = KP defaulty Podkladárny (pullauta.base.ini)."""
    p = DEFAULT_PARAMS
    assert p.yellow_height_m == 0.9 and p.yellow_threshold == 0.9
    assert p.yellow_cell_m == 3.0 and p.yellow_window_cells == 2
    assert p.yellow_window_m == 6.0
    assert p.yellow_canopy_close_m == 6.0
    assert p.block_m == 2.0 and p.green_ground_m == 0.9 and p.top_weight == 0.8
    assert p.zones[0] == (1.0, 2.65, 99.0, 1.0)
    assert p.median_size == 7


def test_generate_job_vegetation_density_missing_inputs(tmp_path: Path):
    assert generate_job_vegetation_density(tmp_path) is None
