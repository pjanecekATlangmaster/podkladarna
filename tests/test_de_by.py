"""Bavorsko: dlaždice 1 km LDBV, okno převzorkované na 1 m a opakování po přechodné chybě."""

from __future__ import annotations

import subprocess

from app.pipeline import elevation_raster as er
from app.pipeline.reference_by import BY_DGM1_URL, BY_DOM20_URL

WINDOW = (671500.0, 5472200.0, 673100.0, 5473500.0)


def test_tiles_and_urls():
    assert er.by_tiles((671500.0, 5472200.0, 673100.0, 5473000.0)) == [(671, 5472), (672, 5472), (673, 5472)]
    assert BY_DGM1_URL.format(e=671, n=5472).endswith("/dgm/dgm1/671_5472.tif")
    assert BY_DOM20_URL.format(e=671, n=5472).endswith("/dom20/DOM/32671_5472_20_DOM.tif")


def test_window_clamped_to_tile_and_resampled(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, log=None):
        calls.append(cmd)
        open(cmd[-1], "wb").close()

    monkeypatch.setattr(er, "run_cmd", fake_run)
    url = BY_DOM20_URL.format(e=671, n=5472)
    assert er._by_tile_window(url, (671, 5472), WINDOW, tmp_path / "d.tif", resample="max") is not None
    cmd = calls[0]
    assert cmd[cmd.index("-te") + 1:cmd.index("-te") + 5] == ["671500", "5472200", "672000", "5473000"]
    assert cmd[cmd.index("-r") + 1] == "max" and cmd[cmd.index("-tr") + 1] == "1"
    # dlaždice mimo okno a okno, které dlaždici jen lízne (pod 2 m, dřív gdalwarp „822x0 dataset“), se nečtou
    assert er._by_tile_window(url, (680, 5472), WINDOW, tmp_path / "x.tif", resample="max") is None
    assert er._by_tile_window(url, (671, 5471), (671500.0, 5471999.4, 673100.0, 5473500.0), tmp_path / "y.tif",
                              resample="max") is None
    assert len(calls) == 1


def test_transient_403_is_retried(monkeypatch, tmp_path):
    """bayernwolke při souběhu občas vrátí 403 (Fürstenschlag DGM1 673_5472) – zkusit znovu."""
    errors = ["ERROR 11: HTTP response code: 403"]

    def fake_run(cmd, log=None):
        if errors:
            raise subprocess.CalledProcessError(1, cmd, stderr=errors.pop())
        open(cmd[-1], "wb").close()

    monkeypatch.setattr(er, "run_cmd", fake_run)
    monkeypatch.setattr("time.sleep", lambda s: None)
    dest = er._by_tile_window(BY_DGM1_URL.format(e=671, n=5472), (671, 5472), WINDOW, tmp_path / "t.tif", resample="near")
    assert dest is not None and not errors
