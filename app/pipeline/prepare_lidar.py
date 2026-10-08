from __future__ import annotations

import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.download_cache import (
    LEGACY_MERGED_LAZ_NAMES,
    SHEET_CROP_GROUND,
    SHEET_CROP_VEG,
    link_or_copy,
    persist_sheet_crop,
    try_lookup_sheet_crop,
)
from app.tool_env import gis_subprocess_env, which_tool

# Menší než tohle = prázdný / nepoužitelný LAZ (stejný práh jako reuse v run_job).
MIN_LAZ_BYTES = 1000


def kp_grid_scale_m(scalefactor: float) -> float:
    """Buňka heightmapy v metrech: scale = 2 * scalefactor."""
    return 2.0 * float(scalefactor)


def expand_crop_bounds(
    bounds: tuple[float, float, float, float],
    pad_m: float,
) -> tuple[float, float, float, float]:
    """Rozšíří bbox o pad_m na všechny strany (nikdy nezmenšuje)."""
    xmin, ymin, xmax, ymax = bounds
    pad = max(0.0, float(pad_m))
    return xmin - pad, ymin - pad, xmax + pad, ymax + pad


def kp_pad_crop_bounds(
    bounds: tuple[float, float, float, float],
    scalefactor: float,
    extra_pad_m: float = 0.0,
) -> tuple[float, float, float, float]:
    """Rozšíří ořez ven o ~1 buňku KP (+ extra), ať okraj heightmapy leží mimo výběr.

    Nikdy nezmenšuje – výřez uživatele musí zůstat uvnitř.
    """
    pad = kp_grid_scale_m(scalefactor) + 0.05 + max(0.0, float(extra_pad_m))
    return expand_crop_bounds(bounds, pad)


def resolve_merge_crop_bounds(
    crop_bounds: tuple[float, float, float, float] | None,
    scalefactor: float | None,
    *,
    extra_pad_m: float = 0.0,
) -> tuple[float, float, float, float] | None:
    """Finální ořez pro prepare (user bbox + KP pad + volitelný OOB extra)."""
    if crop_bounds is None:
        return None
    bounds = crop_bounds
    if scalefactor is not None:
        bounds = kp_pad_crop_bounds(bounds, scalefactor, extra_pad_m=extra_pad_m)
    elif extra_pad_m:
        bounds = expand_crop_bounds(bounds, extra_pad_m)
    return bounds


def _crop_filter_bounds(bounds: tuple[float, float, float, float]) -> str:
    xmin, ymin, xmax, ymax = bounds
    return f"--filters.crop.bounds=([{xmin},{xmax}],[{ymin},{ymax}])"


def find_tool(name: str) -> str:
    path = which_tool(name)
    if not path:
        raise RuntimeError(
            f"Nástroj '{name}' není v PATH. Na Windows nainstalujte OSGeo4W "
            f"(C:\\OSGeo4W\\bin\\{name}.exe) nebo spusťte plný stack: "
            "docker compose -f docker-compose.dev.yml up --build"
        )
    return path


def log_step(log, message: str) -> None:
    """Krátká česká hláška do job logu těsně před spuštěním nástroje."""
    if log is None or not message:
        return
    text = str(message).strip()
    if not text.endswith(("…", "...", ".", "!", "?")):
        text += "…"
    log(text)


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    log: callable | None = None,
) -> subprocess.CompletedProcess:
    line = "> " + " ".join(cmd)
    if log:
        log(line)
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=False,
        text=True,
        capture_output=True,
        env=gis_subprocess_env(cmd[0] if cmd else None),
    )
    if result.stdout and log:
        for ln in result.stdout.strip().splitlines()[-5:]:
            log(ln)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "Unknown error").strip()
        if log:
            log(err)
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return result


def _laz_usable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= MIN_LAZ_BYTES
    except OSError:
        return False


def _translate_sheet(
    pdal: str,
    src: Path,
    dest: Path,
    *,
    stages: list[str],
    stage_opts: list[str],
    crop: tuple[float, float, float, float] | None,
    recipe: str | None = None,
    force_refresh: bool = False,
    log: callable | None = None,
) -> Path | None:
    """PDAL translate; při cropu nejdřív ořez, pak filtry. Prázdný list → None.

    Stejný list + bounds + filtr bere z AOI sheet-crop cache (bez PDAL).
    Nadmnožina v cache → PDAL jen z malého ořezu. Jinak ``--stream`` z plného listu.
    """
    if dest.exists():
        dest.unlink()

    read_src = src
    cache_kind: str | None = None
    if crop is not None and recipe and not force_refresh:
        hit = try_lookup_sheet_crop(src, crop, recipe, force=force_refresh)
        if hit is not None:
            cache_kind, hit_path, _hit_bounds = hit
            if cache_kind == "exact":
                log_step(
                    log,
                    f"Cache zásah listu {src.parent.name}/{src.name} ({recipe}) "
                    "– stejný ořez+filtr, přeskakuji PDAL",
                )
                link_or_copy(hit_path, dest)
                if not _laz_usable(dest):
                    if dest.exists():
                        dest.unlink(missing_ok=True)
                    if log:
                        log(f"  skip (prázdný cache): {src.name}")
                    return None
                return dest
            # superset: dořež z už vyfiltrovaného menšího LAZ
            read_src = hit_path
            log_step(
                log,
                f"Cache nadmnožina listu {src.parent.name}/{src.name} ({recipe}) "
                "– PDAL ořez jen z menšího LAZ (ne z plného listu)",
            )
        else:
            log_step(
                log,
                f"PDAL ořez listu {src.parent.name}/{src.name} ({recipe}) běží – "
                "cache miss (jiný výřez/list/filtr nebo prázdná cache)",
            )
    elif crop is not None:
        log_step(
            log,
            f"PDAL ořez listu {src.parent.name}/{src.name} běží"
            + (" – force_refresh" if force_refresh else ""),
        )

    cmd = [pdal, "translate", str(read_src), str(dest)]
    if crop is not None:
        cmd.append("crop")
    # Při ořezu z cache nadmnožiny už je filtr (range/assign) hotový – stačí crop.
    if cache_kind != "superset":
        cmd.extend(stages)
    if crop is not None:
        cmd.append(_crop_filter_bounds(crop))
    if cache_kind != "superset":
        cmd.extend(stage_opts)
    # crop+range/assign jsou streamovatelné; u DMPOK (~360 MB) ~3× rychlejší než nostream.
    if crop is not None:
        cmd.append("--stream")
    try:
        run_cmd(cmd, log=log)
    except subprocess.CalledProcessError:
        if crop is not None and not _laz_usable(dest):
            if dest.exists():
                dest.unlink(missing_ok=True)
            if log:
                log(f"  skip (mimo ořez / prázdný): {src.name}")
            return None
        raise
    if not _laz_usable(dest):
        if dest.exists():
            dest.unlink(missing_ok=True)
        if log:
            log(f"  skip (prázdný): {src.name}")
        return None
    if crop is not None and recipe:
        persist_sheet_crop(src, dest, crop, recipe, log=log)
    return dest


def _merge_parts(
    pdal: str,
    parts: list[Path],
    dest: Path,
    *,
    log: callable | None = None,
) -> Path:
    """Sloučí ořezy listů proudově (konstantní paměť).

    ``pdal merge`` drží celé mračno v RAM (~90 B/bod → velký výřez spadl na
    OOM). Pipeline s více readery + ``writers.las`` v ``--stream`` dá stejné
    body i hlavičku (stejné výchozí volby writeru jako ``pdal merge``).
    """
    if len(parts) == 1:
        # Bez kopírování – jediný list je rovnou výsledek.
        return parts[0]
    if dest.exists():
        dest.unlink()
    pipeline = {
        "pipeline": [
            *[str(p) for p in parts],
            {"type": "writers.las", "filename": str(dest)},
        ]
    }
    pipe_path = dest.with_name(dest.stem + "_pipeline.json")
    pipe_path.write_text(json.dumps(pipeline), encoding="utf-8")
    try:
        run_cmd([pdal, "pipeline", str(pipe_path), "--stream"], log=log)
    finally:
        pipe_path.unlink(missing_ok=True)
    return dest


def _laz_point_count(pdal: str, path: Path) -> int | None:
    """Počet bodů z hlavičky (``--summary`` nečte body, na rozdíl od ``--stats``)."""
    info = subprocess.run(
        [pdal, "info", str(path), "--summary"],
        capture_output=True,
        text=True,
        check=False,
        env=gis_subprocess_env(pdal),
    )
    try:
        return int(json.loads(info.stdout)["summary"]["num_points"])
    except (ValueError, KeyError, TypeError):
        return None


def _cgroup_cpu_quota() -> float | None:
    """Limit CPU kontejneru (docker ``cpus:``) z cgroup v2 / v1, jinak None."""
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            return float(quota) / float(period)
        return None
    except (OSError, ValueError):
        pass
    try:
        quota_us = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period_us = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if quota_us > 0 and period_us > 0:
            return quota_us / period_us
    except (OSError, ValueError):
        pass
    return None


def available_cpus() -> int:
    """Logická jádra, která proces smí použít (affinity / cpuset + cgroup limit)."""
    try:
        n = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        n = os.cpu_count() or 1
    quota = _cgroup_cpu_quota()
    if quota:
        n = min(n, int(quota + 0.5))
    return max(1, n)


def lidar_workers() -> int:
    """Souběžné PDAL ořezy listů (proudové → paměť zanedbatelná).

    ``PODKLADARNA_LIDAR_WORKERS`` přebije; jinak jádra − 1 (zbytek pro web
    a souběžné kroky), aspoň 2 a nejvýš 8 (pak už brzdí disk).
    R1600 (2 jádra / 4 vlákna) → 3.
    """
    raw = os.environ.get("PODKLADARNA_LIDAR_WORKERS", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(2, min(8, available_cpus() - 1))


def merge_dmr_dmp(
    dmr_files: list[Path],
    dmp_files: list[Path],
    work_dir: Path,
    log: callable | None = None,
    crop_bounds: tuple[float, float, float, float] | None = None,
    scalefactor: float | None = None,
    *,
    extra_pad_m: float = 0.0,
    force_refresh: bool = False,
) -> Path:
    """Připraví dvojici DMR ground + DMP vegetace; vrací ground LAZ.

    Je-li ``crop_bounds``, ořezává **každý list před merge** (stejný finální bbox
    jako dřív: user + buffer + KP pad). Tím se nečtou celé SM5 listy.
    Výstup: ``ground_merged`` / ``veg_merged`` (víc listů) nebo ořez jediného
    listu – viz ``download_cache.lidar_pair_paths``. Společný ``merged_crop``
    (vstup KP pullauta) se už nevytváří.
    """
    if not dmr_files:
        raise FileNotFoundError("Chybí alespoň jeden soubor DMR 5G (LAZ/LAS)")
    if not dmp_files:
        raise FileNotFoundError(
            "Chybí alespoň jeden soubor modelu povrchu (DMP OK / DMP 1G, LAZ/LAS)"
        )

    pdal = find_tool("pdal")
    work_dir.mkdir(parents=True, exist_ok=True)

    crop = resolve_merge_crop_bounds(
        crop_bounds, scalefactor, extra_pad_m=extra_pad_m
    )
    if crop is not None:
        xmin, ymin, xmax, ymax = crop
        if log:
            log(
                "PDAL early-crop bbox 5514: "
                f"[{xmin:.1f},{xmax:.1f}] x [{ymin:.1f},{ymax:.1f}]"
                + (f" (extra_pad={extra_pad_m:g} m)" if extra_pad_m else "")
            )

    log_step(
        log,
        "Ořezávám a třídím LiDAR DMR5G a DMP (mračno bodů pro terén a vegetaci)",
    )
    # Staré výstupy (reuse kopie / KP merged_crop) nesmí přebít nový ořez.
    for name in (
        "ground_merged.laz",
        "veg_merged.laz",
        *LEGACY_MERGED_LAZ_NAMES,
        *(p.name for p in work_dir.glob("dmr_ground_*.laz")),
        *(p.name for p in work_dir.glob("dmp_veg_*.laz")),
    ):
        (work_dir / name).unlink(missing_ok=True)

    log_lock = threading.Lock()

    def _safe_log(msg: str) -> None:
        if log:
            with log_lock:
                log(msg)

    tasks: list[tuple[str, int, dict]] = []
    for i, dmr in enumerate(dmr_files):
        tasks.append(
            (
                "ground",
                i,
                dict(
                    src=dmr,
                    dest=work_dir / f"dmr_ground_{i}.laz",
                    stages=["assign"],
                    stage_opts=["--filters.assign.assignment=Classification[:]=2"],
                    recipe=SHEET_CROP_GROUND if crop is not None else None,
                ),
            )
        )
    for i, dmp in enumerate(dmp_files):
        tasks.append(
            (
                "veg",
                i,
                dict(
                    src=dmp,
                    dest=work_dir / f"dmp_veg_{i}.laz",
                    stages=["range"],
                    stage_opts=["--filters.range.limits=Classification[5:6]"],
                    recipe=SHEET_CROP_VEG if crop is not None else None,
                ),
            )
        )

    def _run_task(task: tuple[str, int, dict]) -> Path | None:
        kind, i, spec = task
        if kind == "veg":
            _safe_log(f"DMP zdroj {i + 1}/{len(dmp_files)}: {spec['src'].name}")
        return _translate_sheet(
            pdal,
            spec["src"],
            spec["dest"],
            stages=spec["stages"],
            stage_opts=spec["stage_opts"],
            crop=crop,
            recipe=spec["recipe"],
            force_refresh=force_refresh,
            log=_safe_log if log else None,
        )

    # Ořezy listů jsou proudové (CPU = dekomprese LAZ) → počet podle jader;
    # pořadí výsledků zůstává dle listů.
    workers = min(lidar_workers(), len(tasks)) or 1
    if log and len(tasks) > 1:
        log(f"PDAL ořezy listů: {len(tasks)} úloh, souběžně {workers} (CPU {available_cpus()})")
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pdal-crop") as ex:
            results = list(ex.map(_run_task, tasks))
    else:
        results = [_run_task(t) for t in tasks]

    ground_parts = [
        part for (kind, _i, _s), part in zip(tasks, results) if kind == "ground" and part
    ]
    veg_parts = [
        part for (kind, _i, _s), part in zip(tasks, results) if kind == "veg" and part
    ]

    if not ground_parts:
        raise RuntimeError(
            "Po ořezu nezůstaly žádné DMR (ground) body – výřez mimo listy?"
        )
    if not veg_parts:
        raise RuntimeError(
            "Po ořezu/filtru Classification[5:6] nezůstaly žádné DMP body"
        )

    ground = _merge_parts(pdal, ground_parts, work_dir / "ground_merged.laz", log=log)
    veg = _merge_parts(pdal, veg_parts, work_dir / "veg_merged.laz", log=log)

    if log:
        for path in (ground, veg):
            n = _laz_point_count(pdal, path)
            count = f"{n:,} bodů, ".replace(",", " ") if n is not None else ""
            log(f"OK {path.name} ({count}{path.stat().st_size / 1e6:.1f} MB)")

    return ground
