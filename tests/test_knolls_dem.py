"""Knolly z DEM."""

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


def test_resolve_want_zip_always_true():
    want, note = resolve_want_zip({"output_zip": False})
    assert want is True
    assert note
    want, note = resolve_want_zip({"output_zip": True})
    assert want is True
    assert note is None


def test_vectorized_knoll_candidates_match_loop():
    import math

    import numpy as np

    from app.pipeline import knolls_dem as k

    rng = np.random.default_rng(11)
    yy, xx = np.mgrid[0:120, 0:150]
    arr = (200 + 0.05 * xx).astype(np.float32)
    for cy, cx, hgt in ((30, 40, 1.5), (80, 100, 2.0), (60, 20, 0.9), (31, 46, 1.2)):
        arr += (hgt * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / 12.0)).astype(np.float32)
    arr += rng.normal(0, 0.02, arr.shape).astype(np.float32)
    valid = np.ones(arr.shape, dtype=bool)
    valid[5:9, 5:9] = False
    gt = (-700000.0, 1.0, 0.0, -1050000.0, 0.0, -1.0)
    kw = dict(rad=8, stride=4, min_prom=0.45, max_prominence_m=2.8, limit=32_000)
    got = k._knoll_candidates_np(arr, valid, gt, **kw)
    ref = k._knoll_candidates_loop(arr, valid, gt, **kw)
    assert got == ref and got
    # Výběr s rozestupem přes mřížku = porovnání se všemi vybranými.
    picked = k._pick_spaced(got, min_spacing_m=14.0, max_points=8000)
    kept = []
    for _p, x, y in sorted(got, key=lambda i: i[0], reverse=True):
        if any(math.hypot(x - a, y - b) < 14.0 for a, b in kept):
            continue
        kept.append((x, y))
    assert picked == kept
