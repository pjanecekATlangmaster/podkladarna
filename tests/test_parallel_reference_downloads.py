"""Paralelní stahování referenčních podkladů (background × hlavní build)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from app.pipeline.job_grid import write_job_grid
from app.pipeline.job_progress import JobProgress
from app.pipeline.preview import ensure_georef_template
from app.pipeline.reference_layers import ALWAYS_REFERENCE_KEYS
from app.pipeline.run_job import (
    ReferenceDownload,
    join_reference_download,
    start_reference_download,
)


def test_start_gate_georef_after_job_grid(tmp_path: Path):
    write_job_grid(
        tmp_path,
        (-700005.0, -1050005.0, -700000.0, -1050000.0),
        resolution_m=1.0,
    )
    pair = ensure_georef_template(tmp_path, prefer_preview=False)
    assert pair is not None
    png, pgw = pair
    assert png.name == "job.png"
    assert pgw.name == "job.pgw"


def test_start_gate_none_without_grid(tmp_path: Path):
    assert ensure_georef_template(tmp_path, prefer_preview=False) is None


def test_want_refs_false_does_not_start(tmp_path: Path):
    """Pipeline gate: bez want_refs se start_reference_download nevolá.

    Helper samotný nemá want_refs — ověříme, že bez volání future nevznikne.
    """
    lines: list[str] = []
    # Bez mřížky by i při volání vrátil None.
    assert (
        start_reference_download(
            tmp_path,
            tmp_path,
            (14.0, 50.0, 14.01, 50.01),
            force_refresh=False,
            log=lines.append,
        )
        is None
    )
    assert lines == []


def test_start_creates_future_with_force_refresh(tmp_path: Path):
    write_job_grid(
        tmp_path,
        (-700005.0, -1050005.0, -700000.0, -1050000.0),
        resolution_m=1.0,
    )
    lines: list[str] = []
    seen: dict = {}

    def fake_build(
        job_dir,
        bbox,
        template_png,
        template_pgw,
        out_dir,
        *,
        log=None,
        force_refresh=False,
        only=None,
    ):
        seen["force_refresh"] = force_refresh
        seen["only"] = only
        seen["template"] = Path(template_png).name
        if log:
            log("orto ok")
        out_dir.mkdir(parents=True, exist_ok=True)
        png = out_dir / "orthophoto.png"
        png.write_bytes(b"x" * 600)
        return {"orthophoto": png}

    with patch(
        "app.pipeline.run_job.build_reference_layers",
        side_effect=fake_build,
    ):
        handle = start_reference_download(
            tmp_path,
            tmp_path,
            (14.0, 50.0, 14.01, 50.01),
            force_refresh=True,
            log=lines.append,
        )
        assert handle is not None
        built, layers = join_reference_download(
            handle,
            log=lines.append,
            want_refs=True,
            bbox=(14.0, 50.0, 14.01, 50.01),
            work_dir=tmp_path,
            job_dir=tmp_path,
            force_refresh=True,
        )
    assert seen["force_refresh"] is True
    assert seen["template"] == "job.png"
    assert "orthophoto" in built
    assert any(ln.startswith("[podklady] Start stahování na pozadí") for ln in lines)
    assert "[podklady] orto ok" in lines
    assert "[podklady] Hotovo (1 vrstev)" in lines
    assert any("Staženo na pozadí: orthophoto" in ln for ln in lines)
    assert layers == ["orthophoto.png"]


def test_join_uses_future_no_double_fetch(tmp_path: Path):
    lines: list[str] = []
    calls = {"n": 0}

    def fake_build(*args, **kwargs):
        calls["n"] += 1
        return {"osm": tmp_path / "references" / "osm.png"}

    executor = ThreadPoolExecutor(max_workers=1)
    future: Future = executor.submit(lambda: {"osm": tmp_path / "x.png"})
    # Počkej, ať je future hotový před joinem.
    future.result()
    handle = ReferenceDownload(
        future=future,
        reference_dir=tmp_path / "references",
        _executor=executor,
    )
    # Nahraď result tak, aby nevolal build znovu — future už má výsledek.
    with patch(
        "app.pipeline.run_job.build_reference_layers",
        side_effect=fake_build,
    ) as mocked:
        built, _ = join_reference_download(
            handle,
            log=lines.append,
            want_refs=True,
            bbox=(14.0, 50.0, 14.01, 50.01),
            work_dir=tmp_path,
            job_dir=tmp_path,
            force_refresh=False,
        )
        mocked.assert_not_called()
    assert calls["n"] == 0
    assert "osm" in built
    assert any("Staženo na pozadí" in ln for ln in lines)


def test_join_exception_logs_and_skips(tmp_path: Path):
    lines: list[str] = []
    progress_lines: list[str] = []
    progress = JobProgress(progress_lines.append, ["referenční podklady", "OOM"])

    executor = ThreadPoolExecutor(max_workers=1)

    def boom():
        raise RuntimeError("WMS down")

    future = executor.submit(boom)
    handle = ReferenceDownload(
        future=future,
        reference_dir=tmp_path / "references",
        _executor=executor,
    )
    built, layers = join_reference_download(
        handle,
        log=lines.append,
        want_refs=True,
        bbox=(14.0, 50.0, 14.01, 50.01),
        work_dir=tmp_path,
        job_dir=tmp_path,
        force_refresh=False,
        progress=progress,
    )
    assert built == {}
    assert layers == []
    assert any("na pozadí selhalo" in ln and "WMS down" in ln for ln in lines)
    assert any("referenční podklady" in ln.lower() for ln in progress_lines)


def test_join_serial_fallback_when_no_future(tmp_path: Path):
    write_job_grid(
        tmp_path,
        (-700005.0, -1050005.0, -700000.0, -1050000.0),
        resolution_m=1.0,
    )
    lines: list[str] = []
    called = {"ok": False}

    def fake_build(*args, **kwargs):
        called["ok"] = True
        out = args[4]
        out.mkdir(parents=True, exist_ok=True)
        png = out / "ztm.png"
        png.write_bytes(b"z" * 600)
        return {"ztm": png}

    with patch(
        "app.pipeline.run_job.build_reference_layers",
        side_effect=fake_build,
    ):
        built, layers = join_reference_download(
            None,
            log=lines.append,
            want_refs=True,
            bbox=(14.0, 50.0, 14.01, 50.01),
            work_dir=tmp_path,
            job_dir=tmp_path,
            force_refresh=False,
        )
    assert called["ok"] is True
    assert "ztm" in built
    assert layers == ["ztm.png"]


def test_join_want_refs_false_skips_build(tmp_path: Path):
    lines: list[str] = []
    with patch("app.pipeline.run_job.build_reference_layers") as mocked:
        built, layers = join_reference_download(
            None,
            log=lines.append,
            want_refs=False,
            bbox=(14.0, 50.0, 14.01, 50.01),
            work_dir=tmp_path,
            job_dir=tmp_path,
            force_refresh=False,
        )
        mocked.assert_not_called()
    assert built == {}
    assert layers == []
    assert any("vypnuto ve formuláři" in ln for ln in lines)


def test_want_refs_false_still_bundles_ortho_and_katastr(tmp_path: Path):
    """Vypnuté podklady: na pozadí jen ALWAYS_REFERENCE_KEYS a join je vrátí do ZIPu."""
    write_job_grid(tmp_path, (-700005.0, -1050005.0, -700000.0, -1050000.0), resolution_m=1.0)
    seen: dict = {}

    def fake_build(*_a, only=None, **_k):
        seen["only"] = only
        return {"orthophoto": tmp_path / "references" / "orthophoto.jpg"}

    lines: list[str] = []
    with patch("app.pipeline.run_job.build_reference_layers", side_effect=fake_build):
        handle = start_reference_download(
            tmp_path, tmp_path, (14.0, 50.0, 14.01, 50.01),
            force_refresh=False, log=lines.append, with_references=False,
        )
        built, layers = join_reference_download(
            handle, log=lines.append, want_refs=False, bbox=(14.0, 50.0, 14.01, 50.01),
            work_dir=tmp_path, job_dir=tmp_path, force_refresh=False,
        )
    assert seen["only"] == ALWAYS_REFERENCE_KEYS
    assert "orthophoto" in built and layers == ["orthophoto.jpg"]


def test_ref_log_prefix_and_lock(tmp_path: Path):
    write_job_grid(
        tmp_path,
        (-700005.0, -1050005.0, -700000.0, -1050000.0),
        resolution_m=1.0,
    )
    lines: list[str] = []
    barrier = threading.Barrier(2)

    def fake_build(*args, **kwargs):
        log = kwargs.get("log")
        barrier.wait(timeout=5)
        if log:
            log("detail A")
            log("> gdalwarp in.tif out.png")
            log("detail B")
        return {}

    with patch(
        "app.pipeline.run_job.build_reference_layers",
        side_effect=fake_build,
    ):
        handle = start_reference_download(
            tmp_path,
            tmp_path,
            (14.0, 50.0, 14.01, 50.01),
            force_refresh=False,
            log=lines.append,
        )
        assert handle is not None
        barrier.wait(timeout=5)
        built, _ = join_reference_download(
            handle,
            log=lines.append,
            want_refs=True,
            bbox=(14.0, 50.0, 14.01, 50.01),
            work_dir=tmp_path,
            job_dir=tmp_path,
            force_refresh=False,
        )
    assert built == {}
    ref_lines = [ln for ln in lines if ln.startswith("[podklady] ")]
    assert "[podklady] detail A" in ref_lines
    assert "[podklady] detail B" in ref_lines
    # Příkazy nástrojů z pozadí se do logu nepletou.
    assert not any("gdalwarp" in ln for ln in lines)


def test_progress_begin_done_only_on_join(tmp_path: Path):
    write_job_grid(
        tmp_path,
        (-700005.0, -1050005.0, -700000.0, -1050000.0),
        resolution_m=1.0,
    )
    log_lines: list[str] = []
    progress_lines: list[str] = []
    progress = JobProgress(
        progress_lines.append,
        ["mřížka mapy", "referenční podklady", "OOM"],
    )

    def slow_build(*args, **kwargs):
        time.sleep(0.05)
        return {"katastr": tmp_path / "references" / "katastr.png"}

    with patch(
        "app.pipeline.run_job.build_reference_layers",
        side_effect=slow_build,
    ):
        # Start nesmí volat progress.begin.
        handle = start_reference_download(
            tmp_path,
            tmp_path,
            (14.0, 50.0, 14.01, 50.01),
            force_refresh=False,
            log=log_lines.append,
        )
        assert not any("referenční podklady" in ln for ln in progress_lines)
        join_reference_download(
            handle,
            log=log_lines.append,
            want_refs=True,
            bbox=(14.0, 50.0, 14.01, 50.01),
            work_dir=tmp_path,
            job_dir=tmp_path,
            force_refresh=False,
            progress=progress,
        )
    assert any("referenční podklady" in ln.lower() for ln in progress_lines)


def test_vector_prefetch_results_and_errors(tmp_path: Path, monkeypatch):
    """ZABAGED, RÚIAN, AOPK a parcely ulic na pozadí; chyba RÚIAN = None (krok si stáhne sám)."""
    from app.pipeline import run_job, street_parcels

    monkeypatch.setattr(run_job, "fetch_zabaged_for_bbox", lambda bbox, log, force_refresh: tmp_path / "z.zip")

    def boom(*_a, **_k):
        raise OSError("síť")

    monkeypatch.setattr(run_job, "fetch_ruian_buildings_for_bbox", boom)
    monkeypatch.setattr(run_job, "fetch_aopk_trees_for_bbox", lambda *a, **k: tmp_path / "a.geojson")
    streets: list = []
    monkeypatch.setattr(street_parcels, "prefetch_street_inputs", lambda *a, **k: streets.append(1))
    lines: list[str] = []
    vp = run_job.start_vector_prefetch(
        tmp_path, (14.0, 50.0, 14.01, 50.01), force_refresh=False, with_streets=True, log=lines.append
    )
    assert vp.zabaged() == tmp_path / "z.zip"
    assert vp.ruian() is None
    assert vp.aopk() == tmp_path / "a.geojson"
    vp.streets()
    assert streets == [1]
    assert any(ln.startswith("[vektory] RÚIAN budovy: přeskočeno") for ln in lines)


@pytest.mark.parametrize(
    ("preset_id", "deferred"),
    [("forest_10000", True), ("sprint_2_5m", False)],
)
def test_package_output_defers_refs_join_except_sprint(
    tmp_path: Path, monkeypatch, preset_id: str, deferred: bool
):
    """Les/MTBO: join podkladů až uvnitř první mapy (jednou); sprint hned, built_refs je dict."""
    from types import SimpleNamespace

    from app.pipeline import oom_preview, run_job

    kp_cwd = tmp_path / "work"
    ref_dir = kp_cwd / "references"
    ref_dir.mkdir(parents=True)
    ortho = ref_dir / "orthophoto.jpg"
    ortho.write_bytes(b"x")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    events: list[str] = []
    seen_refs: list[object] = []
    captured: dict = {}

    def fake_join(*_a, **_k):
        events.append("join")
        return {"orthophoto": ortho}, ["orthophoto.jpg"]

    def fake_prepare(_cwd, dest, **kw):
        refs = kw["built_refs"]
        seen_refs.append(refs)
        events.append("prepare")
        if callable(refs):
            refs()
        Path(dest).write_bytes(b"")
        return Path(dest)

    def fake_zip(*_a, **kw):
        captured["metadata"] = kw["metadata"]

    monkeypatch.setattr(run_job, "join_reference_download", fake_join)
    monkeypatch.setattr(run_job, "prepare_oom_map", fake_prepare)
    monkeypatch.setattr(run_job.app_settings, "PARALLEL_DISCIPLINE_MAPS", False)  # sériová cesta (fake v procesu)
    monkeypatch.setattr(run_job, "fetch_ruian_buildings_for_bbox", lambda *a, **k: None)
    monkeypatch.setattr(run_job, "fetch_aopk_trees_for_bbox", lambda *a, **k: None)
    monkeypatch.setattr(run_job, "write_job_oom_preview", lambda *a, **k: None)
    monkeypatch.setattr(run_job, "build_oom_zip", fake_zip)
    monkeypatch.setattr(oom_preview, "convert_omap_to_ocd", lambda *a, **k: None)

    run_job._package_output(
        kp_cwd,
        output_dir,
        None,
        {"bbox_wgs84": [14.35, 50.02, 14.36, 50.03]},
        lambda *_a, **_k: None,
        preset_id=preset_id,
        ref_download=SimpleNamespace(reference_dir=ref_dir),
    )

    assert events.count("join") == 1
    assert seen_refs
    if deferred:
        assert events[0] == "prepare"
        assert events.index("join") == 1  # uvnitř první mapy
        assert callable(seen_refs[0])
    else:
        assert events[0] == "join"
        assert isinstance(seen_refs[0], dict)
    assert captured["metadata"]["reference_layers"] == ["orthophoto.jpg"]
