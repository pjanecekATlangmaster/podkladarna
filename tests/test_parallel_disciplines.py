"""Souběžné sestavení map disciplín (les ‖ MTBO): pořadí, podklady, pojistka paměti, sprint, spawn."""

from __future__ import annotations

import multiprocessing
import pickle
import queue
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

from app.pipeline import discipline_maps as dm
from app.pipeline.discipline_maps import DisciplineTask, MapCall, _RefsFile


def _tasks(tmp_path: Path) -> list[DisciplineTask]:
    return [
        DisciplineTask(
            tag,
            [MapCall(f"{tag}-{i}.omap", bool(i), f"OOM: {tag}-{i}", tmp_path / f"{tag}-{i}.omap", {"scale": 1}) for i in (0, 1)],
        )
        for tag in ("les", "mtbo")
    ]


def _thread_factory(log_queue):
    dm.set_child_log_queue(log_queue)  # potomek v testu = vlákno
    return ThreadPoolExecutor(max_workers=2)


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    seen: dict = {"refs": [], "lines": []}

    def fake_prepare(_cwd, dest, *, built_refs=None, log=None, **kw):
        if str(dest).startswith(str(tmp_path / "les")):
            time.sleep(0.3)  # les skončí po MTBO: pořadí výsledků nesmí záviset na dokončení
        seen["refs"].append(built_refs() if callable(built_refs) else built_refs)
        if "fail" in dest.name:
            return None
        dest.write_text("x")
        return dest

    monkeypatch.setattr(dm, "prepare_oom_map", fake_prepare)
    monkeypatch.setattr("app.pipeline.oom_preview.convert_omap_to_ocd", lambda *a, **k: None)
    monkeypatch.setattr(dm, "available_memory_mb", lambda: 20000.0)
    return seen


def _run(tmp_path, seen, **kw):
    args = dict(built_refs={}, join_refs=None, log=seen["lines"].append, parallel=True,
                executor_factory=_thread_factory, queue_factory=queue.Queue)  # fmt: skip
    args.update(kw)
    return dm.build_discipline_maps(tmp_path, _tasks(tmp_path), **args)


def test_results_in_discipline_order_and_logs_tagged(tmp_path, fake_env):
    res = _run(tmp_path, fake_env)
    assert [[p.name for p in r] for r in res] == [["les-0.omap", "les-1.omap"], ["mtbo-0.omap", "mtbo-1.omap"]]
    assert any(ln.startswith("[les] OOM: les-0.omap hotovo za") for ln in fake_env["lines"])
    assert any("špička paměti" in ln for ln in fake_env["lines"])


@pytest.mark.parametrize(
    ("join", "expected"),
    [
        (lambda: {"ortho": Path("/x/o.jpg")}, {"ortho": Path("/x/o.jpg")}),
        (lambda: {}, None),
        (lambda: (_ for _ in ()).throw(RuntimeError("join")), None),  # selhání joinu: soubor se zapíše přesto
    ],
)
def test_refs_file_written_always(tmp_path, fake_env, join, expected):
    _run(tmp_path, fake_env, built_refs=None, join_refs=join)
    assert fake_env["refs"] and all(r == expected for r in fake_env["refs"])
    assert not (tmp_path / dm.REFS_BUILT_NAME).exists()


@pytest.mark.parametrize(
    ("free_mb", "parallel", "n_tasks", "expect_pool"),
    [(20000.0, True, 2, True), (2000.0, True, 2, False), (None, True, 2, True), (20000.0, False, 2, False), (20000.0, True, 1, False)],
)
def test_parallel_gating(tmp_path, fake_env, monkeypatch, free_mb, parallel, n_tasks, expect_pool):
    monkeypatch.setattr(dm, "available_memory_mb", lambda: free_mb)
    used = []

    def factory(q):
        used.append(1)
        return _thread_factory(q)

    tasks = _tasks(tmp_path)[:n_tasks]
    res = dm.build_discipline_maps(tmp_path, tasks, built_refs={}, join_refs=None, log=fake_env["lines"].append,
                                   parallel=parallel, executor_factory=factory, queue_factory=queue.Queue)  # fmt: skip
    assert bool(used) is expect_pool
    assert len(res) == n_tasks and all(p for r in res for p in r)
    if free_mb is not None and free_mb < dm.MIN_FREE_MEMORY_MB and parallel:
        assert any("sériově" in ln for ln in fake_env["lines"])


def test_failed_map_is_issue_and_other_discipline_survives(tmp_path, fake_env):
    tasks = _tasks(tmp_path)
    tasks[0].calls[1].dest = tmp_path / "les-fail.omap"
    log = fake_env["lines"]
    res = dm.build_discipline_maps(tmp_path, tasks, built_refs={}, join_refs=None, log=log.append, parallel=True,
                                   executor_factory=_thread_factory, queue_factory=queue.Queue)  # fmt: skip
    assert res[0][1] is None and res[1][0] is not None
    assert any(ln.startswith("⚠ [les] OOM: les-1.omap nevytvořeno") for ln in log)


@pytest.mark.parametrize(("preset", "expected"), [("forest_10000", True), ("sprint_2_5m", False)])
def test_package_output_parallel_flag_not_for_sprint(tmp_path, monkeypatch, preset, expected):
    from app.pipeline import run_job

    kp = tmp_path / "work"
    (kp / "references").mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    seen: dict = {}

    def fake_build(_kp, tasks, **kw):
        seen.update(kw)
        return [[None for _ in t.calls] for t in tasks]

    monkeypatch.setattr(run_job, "build_discipline_maps", fake_build)
    monkeypatch.setattr(run_job, "fetch_ruian_buildings_for_bbox", lambda *a, **k: None)
    monkeypatch.setattr(run_job, "fetch_aopk_trees_for_bbox", lambda *a, **k: None)
    monkeypatch.setattr(run_job, "write_job_oom_preview", lambda *a, **k: None)
    monkeypatch.setattr(run_job, "build_oom_zip", lambda *a, **k: None)
    monkeypatch.setattr(run_job.app_settings, "PARALLEL_DISCIPLINE_MAPS", True)
    run_job._package_output(kp, out, None, {"bbox_wgs84": [14.35, 50.02, 14.36, 50.03]}, lambda *_a, **_k: None, preset_id=preset)
    assert seen["parallel"] is expected


# --- skutečný spawn: picklovatelnost a initializer potomka ---------------------------------------------


def _spawn_echo(task, refs):
    """Trivální funkce potomka: vrátí podklady a pošle řádek do fronty logu."""
    dm._CHILD_LOG_QUEUE.put(f"ahoj {task.tag}")
    return refs(), len(task.calls)


def test_spawn_pickles_task_and_refs_file(tmp_path):
    task = _tasks(tmp_path)[0]
    refs_path = tmp_path / dm.REFS_BUILT_NAME
    assert pickle.loads(pickle.dumps(_RefsFile(refs_path))).path == refs_path
    dm.write_refs_file(refs_path, {"ortho": tmp_path / "o.jpg"})
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    with ProcessPoolExecutor(max_workers=1, mp_context=ctx, initializer=dm._child_init, initargs=(q,)) as pool:
        refs, n = pool.submit(_spawn_echo, task, _RefsFile(refs_path)).result(timeout=120)
    assert refs == {"ortho": tmp_path / "o.jpg"} and n == 2
    assert q.get(timeout=10) == "ahoj les"
