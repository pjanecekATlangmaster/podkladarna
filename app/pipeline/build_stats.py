"""Statistiky buildu jobu pro GUI: data podkladů (staženo / z cache), časy kroků a map, souhrn.

Job běží v samostatném procesu (``app.job_worker``), takže sběr je na úrovni modulu (jedna instance
na job). Zapisuje se do ``work/build_stats.json`` na konci jobu (i po chybě); na výsledek mapy nemá vliv.
Velikost podkladu = velikost uloženého souboru (stejně u stažení i u cache), u rastrů z WMS bajty odpovědí.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

BUILD_STATS_NAME = "build_stats.json"
STATS_VERSION = 1

_lock = threading.Lock()
_sources: dict[str, dict[str, int]] = {}
_seen: set[str] = set()
_maps: dict[str, dict[str, Any]] = {}
_info: dict[str, Any] = {}
_progress = None
_started_at: float | None = None


def reset() -> None:
    """Začátek jobu (a testy)."""
    global _progress, _started_at
    with _lock:
        _sources.clear()
        _seen.clear()
        _maps.clear()
        _info.clear()
        _progress = None
        _started_at = time.time()


def attach_progress(progress) -> None:
    """``JobProgress`` jobu – časy kroků se vezmou z něj při zápisu."""
    global _progress
    _progress = progress


def set_info(**fields) -> None:
    with _lock:
        _info.update({k: v for k, v in fields.items() if v is not None})


def note_bytes(source: str, nbytes: int, *, cached: bool, key: str | None = None) -> None:
    """Započítá podklad ``source``; ``key`` (např. cesta) zabrání dvojímu započtení téhož souboru."""
    try:
        n = max(0, int(nbytes))
    except (TypeError, ValueError):
        return
    with _lock:
        if key is not None:
            if key in _seen:
                return
            _seen.add(key)
        entry = _sources.setdefault(
            source, {"downloaded_bytes": 0, "cached_bytes": 0, "downloaded_files": 0, "cached_files": 0}
        )
        kind = "cached" if cached else "downloaded"
        entry[f"{kind}_bytes"] += n
        entry[f"{kind}_files"] += 1


def note_file(source: str, path: Path | str | None, *, cached: bool) -> None:
    """Započítá soubor podkladu podle velikosti na disku (chybějící soubor se ignoruje)."""
    if not path:
        return
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        return
    try:
        key = str(p.resolve())
    except OSError:
        key = str(p)
    note_bytes(source, size, cached=cached, key=f"{source}:{key}")


def note_files(source: str, paths, *, cached: bool) -> None:
    for p in paths or []:
        note_file(source, p, cached=cached)


def note_map(name: str, seconds: float, *, parallel: bool = False) -> None:
    """Čas sestavení mapy disciplíny (les / MTBO / sprint), v souběhu z rodiče."""
    with _lock:
        _maps[str(name)] = {"name": str(name), "seconds": round(float(seconds), 1), "parallel": bool(parallel)}


def _output_zip_bytes(job_dir: Path) -> int | None:
    from app.pipeline.package_oom import OUTPUT_ZIP_NAME

    path = job_dir / "output" / OUTPUT_ZIP_NAME
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def snapshot(job_dir: Path | None = None, *, status: str | None = None) -> dict[str, Any]:
    with _lock:
        sources = {k: dict(v) for k, v in _sources.items()}
        maps = list(_maps.values())
        info = dict(_info)
    progress = _progress
    steps: list[dict[str, Any]] = []
    total_s = None
    if progress is not None:
        steps = progress.timings()
        total_s = round(progress.elapsed_seconds(), 1)
    elif _started_at is not None:
        total_s = round(time.time() - _started_at, 1)
    data: dict[str, Any] = {
        "version": STATS_VERSION,
        "total_seconds": total_s,
        "steps": steps,
        "maps": maps,
        "sources": sources,
        "downloaded_bytes": sum(v["downloaded_bytes"] for v in sources.values()),
        "cached_bytes": sum(v["cached_bytes"] for v in sources.values()),
        **info,
    }
    if status:
        data["status"] = status
    if job_dir is not None:
        zip_bytes = _output_zip_bytes(job_dir)
        if zip_bytes is not None:
            data["output_zip_bytes"] = zip_bytes
    return data


def write(job_dir: Path, *, status: str | None = None) -> Path | None:
    """Zapíše ``work/build_stats.json``; chyba zápisu job neshodí (vrací None)."""
    try:
        data = snapshot(job_dir, status=status)
        path = Path(job_dir) / "work" / BUILD_STATS_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path
    except Exception:
        return None


def read(job_dir: Path) -> dict[str, Any] | None:
    path = Path(job_dir) / "work" / BUILD_STATS_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
