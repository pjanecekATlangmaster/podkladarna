"""Adaptivní vyhlazení DEM pro vrstevnice (contours_adaptive)."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy")

from app.pipeline.contours_adaptive import adaptive_contour_array, adaptive_params

N = 300  # 300 × 300 m po 1 m


def _grid():
    y, x = np.mgrid[0:N, 0:N].astype(np.float64)
    return x, y


def _run(z, *, scale=10000, sf=1.0, interval=5.0, valid=None):
    if valid is None:
        valid = np.ones(z.shape, dtype=bool)
    return adaptive_contour_array(
        z.astype(np.float32),
        valid,
        cell_m=1.0,
        interval_m=interval,
        scale=scale,
        params=adaptive_params(sf),
    )


def _noise(seed=1, amp=0.3):
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    n = gaussian_filter(rng.normal(size=(N, N)), 3.0)
    return n / np.abs(n).max() * amp


@pytest.mark.parametrize(
    "sf,scale,interval,slope",
    [
        (1.0, 10000, 5.0, 0.4),  # les: od ~25 % beze změny
        (0.4, 4000, 2.0, 0.8),  # sprint: vyhlazuje víc, beze změny až od ~50 %
    ],
)
def test_steep_slope_unchanged(sf, scale, interval, slope):
    """Členitý / strmý terén (vrstevnice hustě) zůstane bit po bitu."""
    x, _y = _grid()
    z = 300.0 + slope * x + _noise()
    out, w = _run(z, sf=sf, scale=scale, interval=interval)
    assert np.array_equal(out, z.astype(np.float32))
    assert not w.any()


@pytest.mark.parametrize("sf,scale,interval", [(1.0, 10000, 5.0), (0.4, 4000, 2.0)])
def test_flat_noise_smoothed(sf, scale, interval):
    """V rovině se šum DMR (vlnky pod 0,3 m) vyhladí."""
    z = 250.0 + _noise()
    out, w = _run(z, sf=sf, scale=scale, interval=interval)
    core = (slice(60, -60), slice(60, -60))
    assert w[core].min() > 0.9
    # Sprint má σ' jen 3 m – šum s korelací ~3 m utlumí méně než les.
    assert out[core].std() < (0.5 if sf >= 1.0 else 0.8) * z[core].std()


@pytest.mark.parametrize(
    "height,kept",
    [
        (3.5, True),  # kupka nad prahem detailu – tvar zůstane
        (2.5, True),
        (0.3, False),  # vlnka – vyhladí se
    ],
)
def test_knoll_on_flat(height, kept):
    x, y = _grid()
    r = np.hypot(x - N / 2, y - N / 2)
    z = 250.0 + height * np.exp(-(r / 8.0) ** 2)
    out, _w = _run(z)
    top_before = z[N // 2, N // 2] - 250.0
    top_after = float(out[N // 2, N // 2]) - 250.0
    if kept:
        assert top_after > 0.8 * top_before
    else:
        assert top_after < 0.6 * top_before


def test_nodata_not_spread():
    """Díra nodata (rybník −9999) se nerozteče a zůstane nodata."""
    z = 250.0 + _noise()
    valid = np.ones(z.shape, dtype=bool)
    valid[140:170, 140:170] = False
    z = np.where(valid, z, -9999.0)
    out, w = _run(z, valid=valid)
    assert np.all(out[~valid] == np.float32(-9999.0))
    assert not w[~valid].any()
    ring = valid.copy()
    ring[130:180, 130:180] = False
    near = ~ring & valid
    assert out[near].min() > 240.0


def test_change_capped():
    """Změna výšky nikdy nepřekročí 0,3 ekvidistance."""
    x, y = _grid()
    z = 250.0 + 0.04 * x + _noise(amp=1.0)
    out, _w = _run(z)
    assert np.abs(out.astype(np.float64) - z.astype(np.float32)).max() <= 0.3 * 5.0 + 1e-4


def test_transition_within_one_contour():
    """Rovina vlevo, strmý svah vpravo: vyhlazení se mění plynule podél čáry."""
    x, _y = _grid()
    slope = np.where(x < 150, 0.02, 0.4)
    z = 250.0 + np.cumsum(slope, axis=1) + _noise()
    _out, w = _run(z)
    row = w[N // 2]
    assert row[20:100].min() > 0.9
    assert row[260:].max() == 0.0
    # Na zlomu svahu chrání tvar ostrá ochrana detailu – i tak bez skoku.
    assert np.abs(np.diff(row)).max() < 0.25
