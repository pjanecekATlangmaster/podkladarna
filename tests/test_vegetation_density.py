"""Tests for LiDAR return-density vegetation (bez KP, KP-inspired makevege)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from app.pipeline.vegetation_density import (
    DEFAULT_PARAMS,
    DENSE,
    DensityVegeParams,
    LIGHT,
    MID,
    OPEN,
    WHITE,
    LidarPoints,
    _box_sum,
    classify_points,
    generate_job_vegetation_density,
    median_filter_uint8,
    reclaim_yellow_under_canopy,
    reinforce_open_from_chm,
    soften_meadow_forest_edge,
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


def test_narrow_meadow_strip_opens_from_chm():
    """Úzký pás s nízkým CHM mezi korunami → 401 (ne zahodit kvůli velikosti).

    Žluté okno by nabralo stromy ze stran; CHM prior pás otevře. Min. plocha
    401 je 12 m² – dlouhý pás ji splní, filtr velikosti není problém.
    """
    cls = np.zeros((40, 80), dtype=np.uint8)  # white forest
    chm = np.full((40, 80), 15.0, dtype=np.float32)
    # 8 m široký pás uprostřed (řádky 16..23)
    chm[16:24, :] = 0.2
    out = reinforce_open_from_chm(cls, chm, open_max_m=1.5, neighbor_px=5, neighbor_frac=0.5)
    strip = out[17:23, 10:70]
    assert np.mean(strip == OPEN) > 0.9
    # koruny mimo pás zůstanou bílé
    assert np.mean(out[2:10, :] == WHITE) > 0.95
    assert np.mean(out[30:38, :] == WHITE) > 0.95


def test_isolated_chm_hole_in_canopy_stays_white():
    """Jednotlivá DMP díra v koruně (CHM=0) se nestane falešnou loukou."""
    cls = np.zeros((30, 30), dtype=np.uint8)
    chm = np.full((30, 30), 18.0, dtype=np.float32)
    chm[14:16, 14:16] = 0.0
    out = reinforce_open_from_chm(cls, chm, open_max_m=1.5, neighbor_px=5, neighbor_frac=0.5)
    assert out[15, 15] == WHITE
    assert np.mean(out == WHITE) > 0.99


def test_chm_open_does_not_overwrite_green():
    cls = np.full((20, 20), LIGHT, dtype=np.uint8)
    cls[5:15, 5:15] = WHITE
    chm = np.zeros((20, 20), dtype=np.float32)
    out = reinforce_open_from_chm(cls, chm, open_max_m=1.5, neighbor_px=5, neighbor_frac=0.5)
    assert np.all(out[0:5, :] == LIGHT)
    assert np.mean(out[7:13, 7:13] == OPEN) > 0.9


def test_meadow_edge_expands_open_to_tall_canopy_edge():
    """Zlom louka→vysoké stromy: 401 až k hraně; inventovaná 410 vypnutá."""
    cls = np.zeros((20, 30), dtype=np.uint8)
    cls[8:12, 5:25] = OPEN  # meadow strip
    chm = np.full((20, 30), 15.0, dtype=np.float32)
    # low + mid fringe u louky → 401 (až k hraně)
    chm[6:8, 5:25] = 1.0
    chm[12:14, 5:25] = 5.0
    # tall fringe dál → WHITE
    chm[3:5, 5:25] = 16.0
    cls[6:8, 5:25] = WHITE
    cls[12:14, 5:25] = WHITE
    cls[3:5, 5:25] = WHITE

    out = soften_meadow_forest_edge(
        cls, chm, expand_chm_m=8.0, open_frac_min=0.12, green_chm_max_m=0.0, passes=2
    )
    assert np.mean(out[7, 8:22] == OPEN) > 0.9
    assert np.mean(out[12, 8:22] == OPEN) > 0.9  # mid fringe = louka, ne 410
    assert np.mean(out[6, 8:22] == OPEN) > 0.8  # 2. průchod dál k hraně
    assert np.mean(out[3:5, 8:22] == WHITE) > 0.8


def test_meadow_edge_keeps_density_green():
    """Přechod do zeleně z hustoty se na zlomu nepřepisuje na 401."""
    cls = np.zeros((20, 20), dtype=np.uint8)
    cls[8:12, :] = OPEN
    cls[6:8, :] = LIGHT  # density green na severním okraji
    cls[12:14, :] = MID
    chm = np.full((20, 20), 2.0, dtype=np.float32)
    out = soften_meadow_forest_edge(cls, chm)
    assert np.all(out[6:8, :] == LIGHT)
    assert np.all(out[12:14, :] == MID)


def test_meadow_edge_optional_invent_green_when_enabled():
    """Legacy: green_chm_max_m > expand znovu zapne WHITE→410 na zlomu."""
    cls = np.zeros((20, 30), dtype=np.uint8)
    cls[8:12, 5:25] = OPEN
    chm = np.full((20, 30), 15.0, dtype=np.float32)
    chm[12:14, 5:25] = 5.0
    cls[12:14, 5:25] = WHITE
    out = soften_meadow_forest_edge(
        cls, chm, expand_chm_m=3.0, open_frac_min=0.2, green_chm_max_m=8.0
    )
    assert np.mean(out[12, 8:22] == DENSE) > 0.9


def test_garden_canopy_away_from_meadow_stays_white():
    """Koruny v zahradě (vysoké CHM, bez styku s loukou) zůstanou bílé."""
    cls = np.zeros((25, 25), dtype=np.uint8)
    cls[20:23, 20:23] = OPEN  # distant meadow corner
    chm = np.full((25, 25), 14.0, dtype=np.float32)
    out = soften_meadow_forest_edge(cls, chm)
    assert np.mean(out[2:10, 2:10] == WHITE) > 0.99
