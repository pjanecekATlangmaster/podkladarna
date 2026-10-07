"""Tests for DEM cliff candidates."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.pipeline.cliffs_dem import (
    detect_cliff_ticks,
    generate_job_cliffs_dem,
    resolve_drop_thresholds,
    write_cliff_ticks_dxf,
)


def test_resolve_drop_thresholds_normal():
    c1, c2 = resolve_drop_thresholds({"kp_cliff_sensitivity": "normal"})
    assert c1 == 1.8
    assert c2 == 3.4


def test_resolve_drop_thresholds_default_is_low():
    c1, c2 = resolve_drop_thresholds({})
    assert c1 == 2.2
    assert c2 == 4.0


def test_resolve_drop_thresholds_high():
    c1, c2 = resolve_drop_thresholds({"kp_cliff_sensitivity": "high"})
    assert c1 == 1.4
    assert c2 == 2.8
    c1v, c2v = resolve_drop_thresholds({"kp_cliff_sensitivity": "very_high"})
    assert c1v < c1
    assert c2v <= c2


def test_flat_dem_yields_no_ticks():
    elev = np.full((40, 40), 100.0, dtype=np.float32)
    gt = (0.0, 1.0, 0.0, 40.0, 0.0, -1.0)
    small, large = detect_cliff_ticks(elev, gt, min_drop_m=1.4, major_drop_m=2.8)
    assert small == []
    assert large == []


def test_step_dem_yields_ticks():
    """Schod ~4 m napříč středem → skalní kandidáti (strmý skok)."""
    elev = np.zeros((50, 50), dtype=np.float32)
    elev[:, 25:] = -4.0
    gt = (0.0, 1.0, 0.0, 50.0, 0.0, -1.0)
    earth, rock = detect_cliff_ticks(
        elev, gt, min_drop_m=1.0, major_drop_m=2.0, stride=1
    )
    assert len(earth) + len(rock) > 0
    assert len(rock) > len(earth)


def test_gentle_bank_is_mostly_earth():
    """Nízký schod (~1,6 m) není skála."""
    elev = np.zeros((50, 50), dtype=np.float32)
    elev[:, 25:] = -1.6
    gt = (0.0, 1.0, 0.0, 50.0, 0.0, -1.0)
    earth, rock = detect_cliff_ticks(
        elev, gt, min_drop_m=1.2, major_drop_m=4.0, stride=1
    )
    assert len(earth) > 0
    assert len(rock) < len(earth)


def test_steep_but_short_step_not_demoted_to_earth():
    """Strmý schod pod rock drop (cliff1+bonus) → ani skála, ani 104."""
    elev = np.zeros((50, 50), dtype=np.float32)
    elev[:, 25:] = -2.2  # nad cliff1=1.8, pod 1.8+0.5=2.3
    gt = (0.0, 1.0, 0.0, 50.0, 0.0, -1.0)
    earth, rock = detect_cliff_ticks(
        elev, gt, min_drop_m=1.8, major_drop_m=3.4, stride=1
    )
    assert rock == []
    # Zemní kandidáti mohou existovat jen mimo strmý pás; strmé slabé se zahodí.
    # Hlavní: žádné skalní ticky z ~2,2 m schodu.
    assert len(rock) == 0


def test_tall_steep_step_is_rock():
    """Vysoký strmý schod (≥ cliff1+bonus) → skála."""
    elev = np.zeros((50, 50), dtype=np.float32)
    elev[:, 25:] = -4.0
    gt = (0.0, 1.0, 0.0, 50.0, 0.0, -1.0)
    earth, rock = detect_cliff_ticks(
        elev, gt, min_drop_m=1.8, major_drop_m=3.4, stride=1
    )
    assert len(rock) > 0
    assert len(rock) > len(earth)


def test_uniform_slope_few_or_no_ticks():
    """Rovnoměrný svah bez schodu – trend odečet by měl potlačit falešné srázy."""
    elev = np.zeros((50, 50), dtype=np.float32)
    for c in range(50):
        elev[:, c] = -0.3 * c  # grade 0.3
    gt = (0.0, 1.0, 0.0, 50.0, 0.0, -1.0)
    small, large = detect_cliff_ticks(
        elev, gt, min_drop_m=1.4, major_drop_m=2.8, stride=1
    )
    assert len(small) + len(large) < 20


def test_write_cliff_ticks_dxf(tmp_path: Path):
    ticks = [((100.0, 200.0), (103.0, 200.0))]
    dest = tmp_path / "c2g.dxf"
    out = write_cliff_ticks_dxf(ticks, dest)
    assert out == dest
    text = dest.read_text(encoding="utf-8")
    assert "LINE" in text
    assert "100.000" in text


def test_generate_job_cliffs_dem_off(tmp_path: Path):
    assert generate_job_cliffs_dem(tmp_path, options={"kp_cliff_symbol": "off"}) == {}


def test_generate_job_cliffs_dem_missing_dem(tmp_path: Path):
    assert generate_job_cliffs_dem(tmp_path, options={}) == {}


def test_generate_job_cliffs_dem_calls_generator(tmp_path: Path):
    dem = tmp_path / "dem"
    dem.mkdir()
    (dem / "dem_filled.tif").write_bytes(b"dem" * 200)
    marker = tmp_path / "temp" / "c2g.dxf"

    def fake_gen(dem_tif, temp_dir, *, min_drop_m=1.4, major_drop_m=2.8, log=None):
        temp_dir.mkdir(parents=True, exist_ok=True)
        path = temp_dir / "c2g.dxf"
        path.write_text("0\nEOF\n", encoding="utf-8")
        return {"c2g.dxf": path}

    with patch(
        "app.pipeline.cliffs_dem.generate_cliffs_from_dem",
        side_effect=fake_gen,
    ):
        out = generate_job_cliffs_dem(tmp_path, options={"kp_cliff_symbol": "earth_bank"})

    assert "c2g.dxf" in out
    assert marker.is_file()


def test_vectorized_cliff_ticks_match_reference_loop():
    """numpy detekce = původní smyčka (stejné ticky, pořadí, limit max_ticks)."""
    import numpy as np

    from app.pipeline import cliffs_dem as cd

    rng = np.random.default_rng(5)
    yy, xx = np.mgrid[0:160, 0:200]
    arr = (300 + 0.2 * xx + 4 * np.sin(yy / 9.0) + 6.0 * (xx > 90) + 4.0 * (yy > 70)).astype(np.float32)
    arr += rng.normal(0, 0.3, arr.shape).astype(np.float32)
    arr[40:44, 10:30] = -9999.0
    valid = arr != -9999.0
    gt = (-700000.0, 1.0, 0.0, -1050000.0, 0.0, -1.0)
    for c1, c2, cap in ((1.8, 3.4, 80_000), (1.15, 2.0, 37)):
        kw = dict(near=2, far=8, px=1.0, py=1.0, min_drop_m=c1, major_drop_m=c2,
                  rock_min_drop=c1 + cd.ROCK_MIN_DROP_BONUS_M, stride=2, max_ticks=cap)
        got = cd._detect_cliff_ticks_np(arr, valid, gt, rows_per_block=7, **kw)
        ref = cd._detect_cliff_ticks_loop(arr, valid, gt, **kw)
        assert got == ref
        assert sum(map(len, got)) > 0
