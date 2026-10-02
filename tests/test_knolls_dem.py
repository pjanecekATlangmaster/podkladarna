"""Knolly z DEM (bez KP)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from app.pipeline.knolls_dem import (
    detect_knolls,
    knoll_min_prominence,
    write_knoll_points_dxf,
)
from app.pipeline.run_job import resolve_want_zip


def test_knoll_factor_raises_threshold():
    assert knoll_min_prominence(0.0) < knoll_min_prominence(0.6)
    assert knoll_min_prominence(0.6) < knoll_min_prominence(1.0)


def test_flat_dem_has_no_knolls():
    elev = np.full((40, 40), 100.0, dtype=np.float32)
    gt = (0.0, 1.0, 0.0, 40.0, 0.0, -1.0)
    assert detect_knolls(elev, gt) == []


def test_bump_is_a_knoll():
    elev = np.full((48, 48), 100.0, dtype=np.float32)
    elev[22:27, 22:27] = 101.6
    gt = (0.0, 1.0, 0.0, 48.0, 0.0, -1.0)
    pts = detect_knolls(elev, gt, factor=0.6)
    assert len(pts) == 1


def test_write_knoll_points_dxf(tmp_path: Path):
    dest = write_knoll_points_dxf([(10.0, 20.0)], tmp_path / "dotknolls.dxf")
    assert dest is not None
    text = dest.read_text(encoding="utf-8")
    assert "POINT" in text
    assert "10.000" in text


def test_bez_kp_forces_zip():
    want, note = resolve_want_zip({"output_zip": False})
    assert want is True
    assert note
    want, note = resolve_want_zip({"use_kp": True, "output_zip": False})
    assert want is True
    assert note is not None
    want, note = resolve_want_zip({"use_kp": False, "output_zip": True})
    assert want is True
    assert note is None
