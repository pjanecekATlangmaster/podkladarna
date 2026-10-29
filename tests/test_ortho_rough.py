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
    classified[:, :120] = 1  # louka i paseka jsou v naší 401
    return rgb, classified


def test_rough_open_split():
    rgb, classified = _scene()
    feats = ortho_rough.ortho_features(rgb, classified.shape, 1.0)
    rough = ortho_rough.rough_open_mask(classified, feats, 1.0)
    core = slice(10, -10)
    assert rough[core, 10:50].mean() < 0.05  # louka zůstane 401
    assert rough[core, 70:110].mean() > 0.95  # paseka → 403
    assert not rough[:, 120:].any()  # mimo 401 se nic nemění
    zone = np.zeros_like(classified, dtype=bool)
    zone[:, 60:120] = True  # zástavba přes paseku: zůstane 401
    rough = ortho_rough.rough_open_mask(classified, feats, 1.0, exclude=zone)
    assert not rough[:, 60:].any()


def test_rough_small_pieces_merge():
    open_mask = np.ones((60, 60), dtype=bool)
    open_mask[:, 50:] = False  # les
    mask = np.zeros((60, 60), dtype=bool)
    mask[5:9, 5:9] = True  # drobný kus 403 → louka
    mask[20:55, 10:45] = True
    mask[30:35, 20:25] = False  # ostrůvek louky uvnitř 403 → 403
    open_mask[:20, 40:] = False
    open_mask[55:, 40:] = False  # proužek louky mezi 403 a lesem → 403
    out = ortho_rough._clean(mask, open_mask, 2.0)  # 4 m² na pixel
    assert not out[5:9, 5:9].any()
    assert out[30:35, 20:25].all()
    assert out[20:55, 45:50].all()
    assert not out[0:3, 30:40].any()  # louka u okraje zůstane
