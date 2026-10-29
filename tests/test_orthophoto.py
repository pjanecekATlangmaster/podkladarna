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


@pytest.mark.parametrize("tiles, downloads", [(2, False), (1, True)])
def test_fetch_orthophoto_from_reference_tiles(tmp_path, monkeypatch, tiles, downloads):
    """Ortofoto podkladu (JPEG dlaždice 0,25 m + .jgw) se převezme bez druhého stahování; nepokryje-li výřez, stáhne se."""
    from PIL import Image

    from app.pipeline import reference_layers

    calls = []
    monkeypatch.setattr(reference_layers, "_download_wms_raster", lambda *a, **k: calls.append(1) or False)
    ref = tmp_path / "references"
    ref.mkdir()
    # dvě dlaždice vedle sebe, každá 5 × 5 m po 0,25 m; levá zelená, pravá šedá
    for name, x0, color in (("orthophoto_r0c0", 0.0, (0, 200, 0)), ("orthophoto_r0c1", 5.0, (100, 100, 100)))[:tiles]:
        Image.new("RGB", (20, 20), color).save(ref / f"{name}.jpg", quality=95)
        (ref / f"{name}.jgw").write_text(f"0.25\n0\n0\n-0.25\n{x0 + 0.125}\n{5 - 0.125}\n", encoding="utf-8")
    logs = []
    got = orthophoto.fetch_orthophoto(tmp_path, BOUNDS, 0.5, log=logs.append)
    assert bool(calls) == downloads
    if downloads:
        assert got is None  # WMS v testu nic nevrátí
        return
    rgb, _gt = got
    assert rgb.shape == (10, 20, 3)
    mask = orthophoto.green_mask(rgb)
    assert mask[:, :10].all() and not mask[:, 10:].any()
    assert any("referenčního podkladu" in ln for ln in logs)


def test_fetch_orthophoto_from_larger_prefetch(tmp_path, monkeypatch):
    """Předstažené ortofoto větší obálky (stejné rozlišení) se pro menší výřez ořízne bez stahování a cache zůstane."""
    from PIL import Image

    from app.pipeline import reference_layers
    from app.pipeline.run_job import ortho_prefetch_bounds

    calls = []

    def fake_download(_url, _layer, bounds, png, pgw, *, width, height, **_k):
        calls.append(bounds)
        arr = np.zeros((height, width, 3), dtype=np.uint8)
        arr[:, : width // 2] = (0, 200, 0)  # levá půlka (x < 5 m) zelená
        Image.fromarray(arr).save(png)
        reference_layers._write_pgw_for_extent(pgw, *bounds, width, height)
        return True

    monkeypatch.setattr(reference_layers, "_download_wms_raster", fake_download)
    assert orthophoto.prefetch_orthophoto(tmp_path, (-5.0, -5.0, 15.0, 10.0), 0.5)
    rgb, gt = orthophoto.fetch_orthophoto(tmp_path, (0.3, 0.0, 10.3, 5.0), 0.5)
    assert len(calls) == 1 and rgb.shape == (10, 20, 3) and gt[0] == 0.3
    mask = orthophoto.green_mask(rgb)
    assert mask[:, :9].all() and not mask[:, 11:].any()
    assert json.loads((tmp_path / "orthophoto" / "ortofoto.json").read_text())["bounds"][0] == -5.0
    assert ortho_prefetch_bounds((0, 0, 10, 10), "sprint_2m", {}) is None
    assert ortho_prefetch_bounds((0, 0, 10, 10), "forest_10000", {}) == (-10.0, -10.0, 20.0, 20.0)
