"""Paralelní stahování referenčních podkladů (background × hlavní build)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.pipeline.job_grid import write_job_grid
from app.pipeline.job_progress import JobProgress
from app.pipeline.preview import ensure_georef_template
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
    ):
        seen["force_refresh"] = force_refresh
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
    assert any("začínám stahování na pozadí" in ln for ln in lines)
    assert any(ln.startswith("[ref] ") for ln in lines)
    assert any("staženo na pozadí — orthophoto" in ln for ln in lines)
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
    assert any("staženo na pozadí" in ln for ln in lines)


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
    assert any("pozadí selhalo" in ln and "WMS down" in ln for ln in lines)
    assert any("referenční podklady" in ln for ln in progress_lines)


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
    assert any("přeskočeny" in ln for ln in lines)


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
    ref_lines = [ln for ln in lines if ln.startswith("[ref] ")]
    assert "[ref] detail A" in ref_lines
    assert "[ref] detail B" in ref_lines


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
        ["georef mřížka", "referenční podklady", "OOM"],
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
    assert any("referenční podklady" in ln for ln in progress_lines)
