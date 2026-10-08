"""Kupky, ďolíky a jámy z DEM."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from app.pipeline.knolls_dem import (
    _pick_spaced,
    detect_relief,
    write_knoll_points_dxf,
)
from app.pipeline.run_job import resolve_want_zip

GT = (0.0, 1.0, 0.0, 120.0, 0.0, -1.0)


def _slope(grade: float = 0.15) -> np.ndarray:
    """Svah 120 × 120 m (1 m), stoupá na východ."""
    yy, xx = np.mgrid[0:120, 0:120]
    return (300.0 + grade * xx).astype(np.float32)


def _bump(height: float, sigma: float, cy: int = 60, cx: int = 60) -> np.ndarray:
    yy, xx = np.mgrid[0:120, 0:120]
    return (height * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * sigma**2))).astype(np.float32)


def _counts(arr) -> dict[str, int]:
    return {k: len(v) for k, v in detect_relief(arr, GT).items()}


def test_flat_and_plain_slope_have_nothing():
    assert _counts(np.full((120, 120), 100.0, dtype=np.float32)) == {
        "knolls": 0, "depressions": 0, "pits": 0,
    }
    assert _counts(_slope()) == {"knolls": 0, "depressions": 0, "pits": 0}


def test_broad_knoll_on_slope_is_found():
    """Ve svahu – starý detektor (vrchol nad celým prstencem) ji neviděl."""
    got = _counts(_slope() + _bump(2.2, 4.0))
    assert got["knolls"] == 1
    assert got["depressions"] == got["pits"] == 0


def test_narrow_heap_is_not_a_knoll():
    """Hromada větví: vysoká, ale úzká – v půlce poloměru už nic."""
    assert _counts(_slope() + _bump(2.0, 1.2))["knolls"] == 0


def test_low_knoll_is_skipped():
    assert _counts(_slope() + _bump(1.0, 4.0))["knolls"] == 0


def test_shallow_depression_vs_steep_pit():
    shallow = _counts(_slope(0.05) - _bump(1.2, 3.5))
    assert shallow["depressions"] == 1 and shallow["pits"] == 0
    yy, xx = np.mgrid[0:120, 0:120]
    r = np.hypot(yy - 60, xx - 60)
    pit = _slope() - np.where(r < 3.0, 2.0, 0.0).astype(np.float32)
    got = _counts(pit)
    assert got["pits"] == 1 and got["depressions"] == 0


def test_gully_is_not_a_depression():
    """Žlab po spádnici není uzavřený – žádný ďolík."""
    yy, xx = np.mgrid[0:120, 0:120]
    gully = _slope() - (1.5 * np.exp(-((yy - 60) ** 2) / 8.0)).astype(np.float32)
    assert _counts(gully)["depressions"] == 0
    assert _counts(gully)["pits"] == 0


def test_shallow_dip_in_channel_is_not_a_depression():
    """Mělký „korálek“ v korytě po spádnici (DMR podél potoků a cest)."""
    yy, xx = np.mgrid[0:120, 0:120]
    channel = _slope() - (1.5 * np.exp(-((yy - 60) ** 2) / 8.0)).astype(np.float32)
    channel -= _bump(1.0, 2.5)
    got = _counts(channel)
    assert got["depressions"] == 0 and got["pits"] == 0


def test_steep_slope_is_ignored():
    """Ve strmém svahu (skály, srázy) nic nehlásit, i když je tam boule."""
    assert _counts(_slope(0.6) + _bump(2.2, 4.0))["knolls"] == 0


def test_pick_spaced_keeps_most_prominent():
    pts = _pick_spaced(
        [(1.0, 0.0, 0.0), (2.0, 5.0, 0.0), (1.5, 50.0, 0.0)],
        min_spacing_m=12.0,
        max_points=10,
    )
    assert pts == [(5.0, 0.0), (50.0, 0.0)]


def test_write_knoll_points_dxf(tmp_path: Path):
    dest = write_knoll_points_dxf([(10.0, 20.0)], tmp_path / "dotpits.dxf", layer="PIT")
    assert dest is not None
    text = dest.read_text(encoding="utf-8")
    assert "POINT" in text and "PIT" in text
    assert "10.000" in text
    # Prázdný výsledek smaže starý soubor (opakovaný běh ve stejném work/).
    assert write_knoll_points_dxf([], dest) is None
    assert not dest.exists()


def test_dxf_codes_for_relief_points():
    from app.pipeline.oom_symbol_map import oom_code_for_dxf

    assert oom_code_for_dxf("dotknolls.dxf", preset_id="forest_10000") == "109"
    assert oom_code_for_dxf("dotdepressions.dxf", preset_id="forest_10000") == "111"
    assert oom_code_for_dxf("dotpits.dxf", preset_id="sprint_2m") == "112"
    assert oom_code_for_dxf("dotdepressions.dxf", preset_id="mtbo_10000") == "112"


def test_resolve_want_zip_always_true():
    want, note = resolve_want_zip({"output_zip": False})
    assert want is True
    assert note
    want, note = resolve_want_zip({"output_zip": True})
    assert want is True
    assert note is None
