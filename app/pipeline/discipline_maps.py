"""Sestavení map disciplín (les, MTBO) jednoho jobu: sériově, nebo souběžně ve dvou procesech (spawn).

Sériová i souběžná cesta jdou přes ``build_discipline_maps_serial`` (jedna logika). Potomek dostává jen
picklovatelné argumenty; log jde přes frontu do rodiče, podklady (``built_refs``) přes soubor
``_refs_built.json``, který rodič zapíše po dokončení joinu (viz ``_RefsFile``).
"""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import Path

from app.fsutil import atomic_write_text
from app.pipeline.job_progress import ISSUE_LOG_PREFIX, format_elapsed, log_issue
from app.pipeline.package_oom import prepare_oom_map

REFS_BUILT_NAME = "_refs_built.json"
# Pod touto volnou pamětí (MB) se mapy staví sériově (špička jedné mapy je ~1–2,5 GB, plus Mapper při OCD).
MIN_FREE_MEMORY_MB = 6 * 1024
_REFS_WAIT_MAX_S = 6 * 3600


@dataclass
class MapCall:
    """Jedna mapa (varianta disciplíny): jméno souboru, hlavička logu, cíl a kwargs pro ``prepare_oom_map``."""

    variant_name: str
    certain: bool
    header: str
    dest: Path
    kwargs: dict = field(default_factory=dict)


@dataclass
class DisciplineTask:
    tag: str
    calls: list[MapCall]


class _RefsFile:
    """``built_refs`` pro potomka: čeká na soubor od rodiče a vrátí jméno → cesta (None, když je prázdný)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def __call__(self) -> dict[str, Path] | None:
        deadline = time.monotonic() + _REFS_WAIT_MAX_S
        while not self.path.is_file():
            if time.monotonic() > deadline:
                return None
            time.sleep(1.0)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return {k: Path(v) for k, v in raw.items()} or None


def write_refs_file(path: Path, refs: dict[str, Path] | None) -> None:
    atomic_write_text(path, json.dumps({k: str(v) for k, v in (refs or {}).items()}))


def build_discipline_maps_serial(
    kp_cwd: Path,
    task: DisciplineTask,
    built_refs,
    log,
    prepare: Callable | None = None,
) -> list[Path | None]:
    """Sestaví mapy disciplíny po řadě (+ OCD); vrací cesty podle ``task.calls`` (None = nevytvořeno)."""
    prepare = prepare or prepare_oom_map
    out: list[Path | None] = []
    for call in task.calls:
        log(call.header)
        t_map = time.monotonic()
        omap_p = prepare(kp_cwd, call.dest, built_refs=built_refs or None, log=log, **call.kwargs)
        if omap_p:
            log(f"OOM: {call.variant_name} hotovo za {format_elapsed(time.monotonic() - t_map)}")
            t_ocd = time.monotonic()
            try:
                from app.pipeline.oom_preview import convert_omap_to_ocd

                convert_omap_to_ocd(omap_p, log=log)
            except Exception as exc:
                log_issue(log, f"OOM OCD: přeskočeno ({call.variant_name}): {exc}")
            log(f"OOM OCD: {call.variant_name} za {format_elapsed(time.monotonic() - t_ocd)}")
        else:
            log_issue(log, f"OOM: {call.variant_name} nevytvořeno")
        out.append(Path(omap_p) if omap_p else None)
    return out


# --- souběh -----------------------------------------------------------------------------------------

_CHILD_LOG_QUEUE = None


def set_child_log_queue(q) -> None:
    global _CHILD_LOG_QUEUE
    _CHILD_LOG_QUEUE = q


def _child_init(log_queue) -> None:
    """Initializer potomka: totéž nastavení GDAL / PROJ jako v procesu jobu."""
    set_child_log_queue(log_queue)
    import app.settings  # noqa: F401  # .env a apply_local_gis_env()

    from app.proj_env import ensure_proj_data

    ensure_proj_data()
    try:
        from osgeo import gdal, ogr

        gdal.UseExceptions()
        ogr.UseExceptions()
    except ImportError:
        pass


def _peak_rss_kb() -> int:
    try:
        import resource

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except Exception:
        return 0


def _tag_line(tag: str, msg: str) -> str:
    if msg.startswith(ISSUE_LOG_PREFIX):
        return f"{ISSUE_LOG_PREFIX}[{tag}] {msg[len(ISSUE_LOG_PREFIX):]}"
    return f"[{tag}] {msg}"


def _discipline_child(kp_cwd: Path, task: DisciplineTask, built_refs) -> tuple[list[str | None], float, int]:
    """Tělo potomka (úroveň modulu kvůli picklu): mapy disciplíny, čas a špička paměti (kB)."""
    q = _CHILD_LOG_QUEUE

    def log(msg: str) -> None:
        if q is not None:
            q.put(_tag_line(task.tag, str(msg)))

    t0 = time.monotonic()
    paths = build_discipline_maps_serial(kp_cwd, task, built_refs, log)
    return [str(p) if p else None for p in paths], time.monotonic() - t0, _peak_rss_kb()


def available_memory_mb() -> float | None:
    """Volná paměť: min(MemAvailable, limit cgroup v2 minus využití bez snadno uvolnitelné cache); None = neznámo."""
    vals: list[float] = []
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                vals.append(int(line.split()[1]) / 1024)
                break
    except (OSError, ValueError, IndexError):
        pass
    try:
        cg = Path("/sys/fs/cgroup")
        limit = (cg / "memory.max").read_text().strip()
        if limit != "max":
            used = int((cg / "memory.current").read_text())
            try:
                for line in (cg / "memory.stat").read_text().splitlines():
                    if line.startswith("inactive_file "):
                        used -= int(line.split()[1])
                        break
            except (OSError, ValueError):
                pass
            vals.append((int(limit) - used) / 1048576)
    except (OSError, ValueError):
        pass
    return min(vals) if vals else None


def _spawn_pool(log_queue) -> Executor:
    ctx = multiprocessing.get_context("spawn")
    return ProcessPoolExecutor(max_workers=2, mp_context=ctx, initializer=_child_init, initargs=(log_queue,))


def build_discipline_maps(
    kp_cwd: Path,
    tasks: list[DisciplineTask],
    *,
    built_refs,
    join_refs: Callable[[], dict | None] | None,
    log,
    parallel: bool,
    prepare: Callable | None = None,
    executor_factory: Callable[[object], Executor] | None = None,
    queue_factory: Callable[[], object] | None = None,
) -> list[list[Path | None]]:
    """Mapy všech disciplín v pořadí ``tasks``. ``parallel`` + ≥ 2 disciplíny + dost paměti = jeden proces na disciplínu.

    ``built_refs``: dict, nebo callable (odložený join) pro sériovou cestu; ``join_refs``: odložený join
    (jen když je ``built_refs`` callable), v souběhu ho spouští rodič a potomci čekají na ``_refs_built.json``."""
    if parallel and len(tasks) >= 2:
        free = available_memory_mb()
        if free is not None and free < MIN_FREE_MEMORY_MB:
            log(f"OOM: volná paměť {free:.0f} MB < {MIN_FREE_MEMORY_MB} MB, mapy disciplín se staví sériově.")
        else:
            return _build_parallel(
                kp_cwd, tasks, built_refs, join_refs, log, prepare, executor_factory, queue_factory
            )
    return [build_discipline_maps_serial(kp_cwd, t, built_refs, log, prepare) for t in tasks]


def _build_parallel(kp_cwd, tasks, built_refs, join_refs, log, prepare, executor_factory, queue_factory):
    log(f"OOM: mapy disciplín ({', '.join(t.tag for t in tasks)}) se staví souběžně v samostatných procesech.")
    refs_file = kp_cwd / REFS_BUILT_NAME
    refs_file.unlink(missing_ok=True)
    child_refs = _RefsFile(refs_file) if join_refs is not None else built_refs
    log_queue = queue_factory() if queue_factory else multiprocessing.get_context("spawn").Queue()
    stop = threading.Event()

    def pump() -> None:
        while True:
            try:
                log(log_queue.get(timeout=0.5))
            except queue.Empty:
                if stop.is_set():
                    return
            except (EOFError, OSError):
                return

    def join_thread() -> None:
        refs: dict | None = {}
        try:
            refs = join_refs()
        except Exception as exc:  # potomek nesmí uvíznout: prázdný soubor = mapa bez šablon
            log_issue(log, f"OOM: podklady pro šablony se nepodařilo spojit ({exc})")
        finally:
            write_refs_file(refs_file, refs)

    pump_t = threading.Thread(target=pump, daemon=True)
    pump_t.start()
    join_t = threading.Thread(target=join_thread, daemon=True) if join_refs is not None else None
    if join_t:
        join_t.start()
    results: list[list[Path | None] | None] = [None] * len(tasks)
    broken: list[int] = []
    try:
        executor = (executor_factory or _spawn_pool)(log_queue)
        with executor:
            futures = [executor.submit(_discipline_child, kp_cwd, t, child_refs) for t in tasks]
            for i, (task, fut) in enumerate(zip(tasks, futures)):
                try:
                    paths, elapsed, rss_kb = fut.result()
                except BrokenProcessPool:
                    broken.append(i)
                except Exception as exc:
                    results[i] = [None] * len(task.calls)
                    log_issue(log, f"OOM: {', '.join(c.variant_name for c in task.calls)} nevytvořeno ({exc})")
                else:
                    results[i] = [Path(p) if p else None for p in paths]
                    log(
                        f"OOM: {task.tag} hotovo za {format_elapsed(elapsed)}, "
                        f"špička paměti {rss_kb / 1024:.0f} MB"
                    )
    finally:
        if join_t:
            join_t.join()
        stop.set()
        pump_t.join(timeout=10)
        refs_file.unlink(missing_ok=True)
    if broken:
        log_issue(log, "OOM: proces mapy spadl, chybějící mapy se dostaví sériově.")
        for i in broken:
            results[i] = build_discipline_maps_serial(kp_cwd, tasks[i], built_refs, log, prepare)
    return [r or [] for r in results]
