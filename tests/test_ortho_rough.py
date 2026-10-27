"""Ortofoto 401/403: drsná část louky (nálet, paseka) → 403, hladká louka zůstane 401."""

import numpy as np
import pytest

from app.pipeline import ortho_rough


def _scene():
    rng = np.random.default_rng(1)
    h, w = 240, 360  # 0,5 m ortofoto = 120 × 180 m
    rgb = np.empty((h, w, 3), dtype=np.float32)
    rgb[...] = (110, 140, 70)
    rgb[:, :120] += rng.normal(0, 3, (h, 120, 1))  # louka: hladká
    rgb[:, 120:240] += rng.normal(0, 30, (h, 120, 1))  # paseka s náletem: drsná
    rgb[:, 240:] = (40, 70, 35)
    rgb[:, 240:] += rng.normal(0, 25, (h, 120, 1))  # les
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    classified = np.zeros((120, 180), dtype=np.uint8)
    classified[:, :120] = 1
    chm_now = np.zeros((120, 180), dtype=np.float32)
    chm_now[:, 120:] = 20.0
    chm_now[:, 60:120] = 0.0
    chm_old = chm_now.copy()
    chm_old[:, 60:120] = 15.0  # v DMP 1G ještě les
    return rgb, classified, chm_now, chm_old


@pytest.mark.parametrize("with_lidar_labels", [True, False, "partial"])
def test_rough_open_split(with_lidar_labels):
    pytest.importorskip("sklearn")
    rgb, classified, chm_now, chm_old = _scene()
    if with_lidar_labels == "partial":  # část území mimo pokrytí DMP 1G
        chm_old[:, :30] = np.nan
    feats = ortho_rough.ortho_features(rgb, classified.shape, 1.0)
    rough = ortho_rough.rough_open_mask(
        classified,
        feats,
        1.0,
        chm_now=chm_now if with_lidar_labels else None,
        chm_old=chm_old if with_lidar_labels else None,
    )
    core = slice(10, -10)
    assert rough[core, 10:50].mean() < 0.05  # louka zůstane 401
    assert rough[core, 70:110].mean() > 0.95  # paseka → 403
    assert not rough[:, 120:].any()  # mimo 401 se nic nemění
