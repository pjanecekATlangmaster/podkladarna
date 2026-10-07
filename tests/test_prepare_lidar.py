from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from app.pipeline.prepare_lidar import (
    ensure_contains_bounds,
    expand_crop_bounds,
    kp_pad_crop_bounds,
    kp_safe_crop_bounds,
    merge_dmr_dmp,
    resolve_merge_crop_bounds,
)


def test_kp_pad_expands_sprint_scale():
    xmin, ymin, xmax, ymax = kp_pad_crop_bounds((0.0, 0.0, 2000.0, 1000.0), 0.4)
    # scale=0.8 m → pad cca 0.85 m ven
    assert xmin < -0.5
    assert ymin < -0.5
    assert xmax > 2000.5
    assert ymax > 1000.5


def test_kp_safe_crop_is_expand_alias():
    """Dřívější inset by zmenšoval výběr – alias musí expandovat."""
    xmin, ymin, xmax, ymax = kp_safe_crop_bounds(
        (0.0, 0.0, 2000.0, 1000.0), 0.4, extra_inset_m=2.0
    )
    assert xmin < -2.0
    assert xmax > 2002.0


def test_kp_oob_pads_stay_near_user_crop():
    """Retry pad musí zůstat u výběru – ne skok na celé SM5 (km)."""
    crop = (0.0, 0.0, 2000.0, 1000.0)
    for extra in (50.0, 150.0, 300.0, 600.0):
        xmin, ymin, xmax, ymax = kp_pad_crop_bounds(crop, 0.4, extra_pad_m=extra)
        assert xmin >= -extra - 2.0
        assert xmax <= 2000.0 + extra + 2.0
        assert (xmax - xmin) < 4000.0


def test_expand_and_ensure_contains():
    assert expand_crop_bounds((0.0, 0.0, 10.0, 10.0), 5.0) == (-5.0, -5.0, 15.0, 15.0)
    assert ensure_contains_bounds((-100.0, -50.0, 0.0, 0.0), (0.0, 0.0, 10.0, 20.0)) == (
        -100.0,
        -50.0,
        10.0,
        20.0,
    )


def test_resolve_merge_crop_bounds_adds_kp_pad():
    base = (0.0, 0.0, 100.0, 50.0)
    assert resolve_merge_crop_bounds(None, 0.4) is None
    out = resolve_merge_crop_bounds(base, 0.4)
    assert out is not None
    assert out[0] < 0.0 and out[2] > 100.0
    wide = resolve_merge_crop_bounds(base, 0.4, extra_pad_m=50.0)
    assert wide is not None
    assert wide[0] < out[0]


def _fake_pipeline_output(cmd):
    """Fake PDAL streaming merge: zapíše výstup writers.las z pipeline JSON."""
    import json

    spec = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
    Path(spec["pipeline"][-1]["filename"]).write_bytes(b"x" * 2000)


def test_merge_dmr_dmp_crops_each_sheet_before_merge(tmp_path, monkeypatch):
    """Early-crop: translate obsahuje crop; žádný dodatečný crop po merge."""
    cmds: list[list[str]] = []

    def fake_run_cmd(cmd, **kwargs):
        cmds.append(list(cmd))
        if cmd[1] == "translate":
            Path(cmd[3]).write_bytes(b"x" * 2000)
        elif cmd[1] == "pipeline":
            _fake_pipeline_output(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("app.pipeline.prepare_lidar.run_cmd", fake_run_cmd)
    monkeypatch.setattr("app.pipeline.prepare_lidar.find_tool", lambda _n: "pdal")
    monkeypatch.setattr(
        "app.pipeline.prepare_lidar.subprocess.run",
        lambda *a, **k: MagicMock(stdout="ok", stderr="", returncode=0),
    )

    dmr = [tmp_path / "a_dmr.laz", tmp_path / "b_dmr.laz"]
    dmp = [tmp_path / "a_dmp.laz", tmp_path / "b_dmp.laz"]
    for p in dmr + dmp:
        p.write_bytes(b"src" * 100)

    work = tmp_path / "lidar"
    out = merge_dmr_dmp(
        dmr,
        dmp,
        work,
        crop_bounds=(0.0, 0.0, 100.0, 100.0),
        scalefactor=0.4,
    )
    # KP merged_crop (ground+veg v jednom LAZ) se už nevytváří.
    assert out.name == "ground_merged.laz"
    assert out.is_file()
    assert (work / "veg_merged.laz").is_file()
    assert not (work / "merged_crop.laz").exists()

    translates = [c for c in cmds if len(c) > 1 and c[1] == "translate"]
    assert len(translates) == 4
    for c in translates:
        assert "crop" in c
        assert "--stream" in c
        assert any(a.startswith("--filters.crop.bounds=") for a in c)

    # Merge listů proudově (pdal pipeline --stream), ne in-memory `pdal merge`.
    assert not [c for c in cmds if len(c) > 1 and c[1] == "merge"]
    merges = [c for c in cmds if len(c) > 1 and c[1] == "pipeline"]
    assert len(merges) == 2
    assert all("--stream" in c for c in merges)
    post_merge_crop = [
        c
        for c in cmds
        if c[1] == "translate" and any("merged" in a for a in c[2:4])
    ]
    assert post_merge_crop == []


def test_merge_dmr_dmp_skips_empty_crop_sheet(tmp_path, monkeypatch):
    def fake_run_cmd(cmd, **kwargs):
        if cmd[1] == "translate":
            dest = Path(cmd[3])
            if dest.name == "dmr_ground_0.laz":
                raise subprocess.CalledProcessError(1, cmd, "", "empty")
            dest.write_bytes(b"x" * 2000)
        elif cmd[1] == "pipeline":
            _fake_pipeline_output(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("app.pipeline.prepare_lidar.run_cmd", fake_run_cmd)
    monkeypatch.setattr("app.pipeline.prepare_lidar.find_tool", lambda _n: "pdal")
    monkeypatch.setattr(
        "app.pipeline.prepare_lidar.subprocess.run",
        lambda *a, **k: MagicMock(stdout="", stderr="", returncode=0),
    )

    dmr = [tmp_path / "a.laz", tmp_path / "b.laz"]
    dmp = [tmp_path / "c.laz"]
    for p in dmr + dmp:
        p.write_bytes(b"src" * 100)

    out = merge_dmr_dmp(
        dmr,
        dmp,
        tmp_path / "w",
        crop_bounds=(0.0, 0.0, 10.0, 10.0),
        scalefactor=0.4,
    )
    assert out.is_file()


def test_merge_dmr_dmp_sheet_cache_skips_pdal(tmp_path, monkeypatch):
    """Exact sheet-crop cache hit → žádný pdal translate pro daný list."""
    from app import settings
    from app.download_cache import SHEET_CROP_VEG, persist_sheet_crop

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path / "cache_root")
    cmds: list[list[str]] = []
    logs: list[str] = []

    def fake_run_cmd(cmd, **kwargs):
        cmds.append(list(cmd))
        if cmd[1] == "translate":
            Path(cmd[3]).write_bytes(b"x" * 2000)
        elif cmd[1] == "pipeline":
            _fake_pipeline_output(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("app.pipeline.prepare_lidar.run_cmd", fake_run_cmd)
    monkeypatch.setattr("app.pipeline.prepare_lidar.find_tool", lambda _n: "pdal")
    monkeypatch.setattr(
        "app.pipeline.prepare_lidar.subprocess.run",
        lambda *a, **k: MagicMock(stdout="ok", stderr="", returncode=0),
    )

    sheet = tmp_path / "sm5" / "VRCH31"
    sheet.mkdir(parents=True)
    dmr = sheet / "DMR5G.laz"
    dmp = sheet / "DMPOK.laz"
    dmr.write_bytes(b"d" * 3000)
    dmp.write_bytes(b"p" * 3000)

    # Předplnit veg cache pro finální crop bounds (user + KP pad scalefactor=0.4).
    from app.pipeline.prepare_lidar import resolve_merge_crop_bounds

    crop_user = (0.0, 0.0, 100.0, 100.0)
    crop = resolve_merge_crop_bounds(crop_user, 0.4)
    assert crop is not None
    pre = tmp_path / "pre_veg.laz"
    pre.write_bytes(b"v" * 2500)
    persist_sheet_crop(dmp, pre, crop, SHEET_CROP_VEG)

    out = merge_dmr_dmp(
        [dmr],
        [dmp],
        tmp_path / "lidar",
        log=logs.append,
        crop_bounds=crop_user,
        scalefactor=0.4,
    )
    assert out.is_file()
    translates = [c for c in cmds if len(c) > 1 and c[1] == "translate"]
    # Jen DMR ground – DMP veg z cache bez PDAL.
    assert len(translates) == 1
    assert "dmr_ground_0.laz" in translates[0][3]
    assert any("přeskakuji PDAL" in m or "preskakuji PDAL" in m for m in logs) or any(
        "Cache zásah" in m or "Cache zasah" in m for m in logs
    )


def test_merge_dmr_dmp_force_refresh_ignores_sheet_cache(tmp_path, monkeypatch):
    from app import settings
    from app.download_cache import SHEET_CROP_GROUND, SHEET_CROP_VEG, persist_sheet_crop
    from app.pipeline.prepare_lidar import resolve_merge_crop_bounds

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path / "cache_root")
    cmds: list[list[str]] = []

    def fake_run_cmd(cmd, **kwargs):
        cmds.append(list(cmd))
        if cmd[1] == "translate":
            Path(cmd[3]).write_bytes(b"x" * 2000)
        elif cmd[1] == "pipeline":
            _fake_pipeline_output(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("app.pipeline.prepare_lidar.run_cmd", fake_run_cmd)
    monkeypatch.setattr("app.pipeline.prepare_lidar.find_tool", lambda _n: "pdal")
    monkeypatch.setattr(
        "app.pipeline.prepare_lidar.subprocess.run",
        lambda *a, **k: MagicMock(stdout="", stderr="", returncode=0),
    )

    sheet = tmp_path / "sm5" / "VRCH31"
    sheet.mkdir(parents=True)
    dmr = sheet / "DMR5G.laz"
    dmp = sheet / "DMPOK.laz"
    dmr.write_bytes(b"d" * 3000)
    dmp.write_bytes(b"p" * 3000)
    crop_user = (0.0, 0.0, 20.0, 20.0)
    crop = resolve_merge_crop_bounds(crop_user, 0.4)
    assert crop is not None
    for src, recipe in ((dmr, SHEET_CROP_GROUND), (dmp, SHEET_CROP_VEG)):
        pre = tmp_path / f"pre_{recipe}.laz"
        pre.write_bytes(b"z" * 2500)
        persist_sheet_crop(src, pre, crop, recipe)

    merge_dmr_dmp(
        [dmr],
        [dmp],
        tmp_path / "lidar",
        crop_bounds=crop_user,
        scalefactor=0.4,
        force_refresh=True,
    )
    translates = [c for c in cmds if len(c) > 1 and c[1] == "translate"]
    assert len(translates) == 2
    assert all("--stream" in c for c in translates)


def test_merge_dmr_dmp_parallel_keeps_sheet_order(tmp_path, monkeypatch):
    """Souběžné ořezy listů: pořadí částí v merge pipeline = pořadí listů."""
    import json
    import time

    pipelines: list[list[str]] = []

    def fake_run_cmd(cmd, **kwargs):
        if cmd[1] == "translate":
            dest = Path(cmd[3])
            # Dřívější listy doběhnou později → bez řazení by se pořadí prohodilo.
            time.sleep(0.05 if dest.name.endswith("_0.laz") else 0.0)
            dest.write_bytes(b"x" * 2000)
        elif cmd[1] == "pipeline":
            spec = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
            pipelines.append(spec["pipeline"][:-1])
            _fake_pipeline_output(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("app.pipeline.prepare_lidar.run_cmd", fake_run_cmd)
    monkeypatch.setattr("app.pipeline.prepare_lidar.find_tool", lambda _n: "pdal")
    monkeypatch.setattr(
        "app.pipeline.prepare_lidar.subprocess.run",
        lambda *a, **k: MagicMock(stdout="", stderr="", returncode=0),
    )
    monkeypatch.setenv("PODKLADARNA_LIDAR_WORKERS", "2")

    dmr = [tmp_path / f"r{i}.laz" for i in range(3)]
    dmp = [tmp_path / f"p{i}.laz" for i in range(3)]
    for p in dmr + dmp:
        p.write_bytes(b"src" * 100)
    work = tmp_path / "lidar"
    (work).mkdir()
    (work / "merged_crop.laz").write_bytes(b"old" * 1000)  # stará KP kopie z reuse
    out = merge_dmr_dmp(dmr, dmp, work, crop_bounds=(0.0, 0.0, 10.0, 10.0), scalefactor=0.4)
    assert out.name == "ground_merged.laz"
    assert [Path(p).name for p in pipelines[0]] == [f"dmr_ground_{i}.laz" for i in range(3)]
    assert [Path(p).name for p in pipelines[1]] == [f"dmp_veg_{i}.laz" for i in range(3)]
    assert not (work / "merged_crop.laz").exists()
