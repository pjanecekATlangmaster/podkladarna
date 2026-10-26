"""Ortofoto: ExG, maska zeleně, cache a chyba sítě bez skutečného HTTP."""

import json

import numpy as np
import pytest

from app.pipeline import orthophoto

BOUNDS = (0.0, 0.0, 10.0, 5.0)


def test_exg_and_green_mask():
    rgb = np.array([[[0, 200, 0], [100, 100, 100], [0, 0, 0], [120, 135, 105], [104, 110, 100]]], dtype=np.uint8)
    assert orthophoto.exg(rgb)[0][:3] == pytest.approx([2.0, 0.0, 0.0])
    # sytá tráva ano; šedá ne; černá ne; suchý trávník (sytost 0,22) ano; nazelenalá dlažba (sytost 0,09) ne
    assert orthophoto.green_mask(rgb)[0].tolist() == [True, False, False, True, False]


def test_fetch_orthophoto_cache_and_error(tmp_path, monkeypatch):
    from PIL import Image

    from app.pipeline import reference_layers

    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise OSError("síť")

    monkeypatch.setattr(reference_layers, "_download_wms_raster", boom)
    logs = []
    assert orthophoto.fetch_orthophoto(tmp_path, BOUNDS, 0.5, log=logs.append) is None
    assert calls and "⚠" in logs[0]
    calls.clear()
    d = tmp_path / "orthophoto"
    d.mkdir(exist_ok=True)
    Image.fromarray(np.full((10, 20, 3), 7, dtype=np.uint8)).save(d / "ortofoto.png")
    (d / "ortofoto.json").write_text(json.dumps({"bounds": list(BOUNDS), "width": 20, "height": 10}), encoding="utf-8")
    rgb, gt = orthophoto.fetch_orthophoto(tmp_path, BOUNDS, 0.5)
    assert not calls and rgb.shape == (10, 20, 3) and gt == (0.0, 0.5, 0.0, 5.0, 0.0, -0.5)
