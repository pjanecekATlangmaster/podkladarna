from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


from app.pipeline.fetch_openzu import CROP_BUFFER_M
from app.tool_env import gis_subprocess_env, which_tool


def find_tool(name: str) -> str:
    path = which_tool(name)
    if not path:
        raise RuntimeError(
            f"Nástroj '{name}' není v PATH. Na Windows nainstalujte OSGeo4W "
            f"(C:\\OSGeo4W\\bin\\{name}.exe) nebo spusťte plný stack: "
            "docker compose -f docker-compose.dev.yml up --build"
        )
    return path


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
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def merge_dmr_dmp(
    dmr_files: list[Path],
    dmp_files: list[Path],
    work_dir: Path,
    log: callable | None = None,
    crop_bounds: tuple[float, float, float, float] | None = None,
) -> Path:
    if not dmr_files:
        raise FileNotFoundError("Chybí alespoň jeden soubor DMR 5G (LAZ/LAS)")
    if not dmp_files:
        raise FileNotFoundError("Chybí alespoň jeden soubor DMP 1G (LAZ/LAS)")

    pdal = find_tool("pdal")
    work_dir.mkdir(parents=True, exist_ok=True)

    ground_parts: list[Path] = []
    for i, dmr in enumerate(dmr_files):
        out = work_dir / f"dmr_ground_{i}.laz"
        run_cmd(
            [
                pdal,
                "translate",
                str(dmr),
                str(out),
                "assign",
                "--filters.assign.assignment=Classification[:]=2",
            ],
            log=log,
        )
        ground_parts.append(out)

    veg_parts: list[Path] = []
    for i, dmp in enumerate(dmp_files):
        out = work_dir / f"dmp_veg_{i}.laz"
        run_cmd(
            [
                pdal,
                "translate",
                str(dmp),
                str(out),
                "range",
                "--filters.range.limits=Classification[5:6]",
            ],
            log=log,
        )
        veg_parts.append(out)

    ground = work_dir / "ground_merged.laz"
    veg = work_dir / "veg_merged.laz"
    merged = work_dir / "merged.laz"

    if len(ground_parts) == 1:
        ground = ground_parts[0]
    else:
        run_cmd([pdal, "merge", *[str(p) for p in ground_parts], str(ground)], log=log)

    if len(veg_parts) == 1:
        veg = veg_parts[0]
    else:
        run_cmd([pdal, "merge", *[str(p) for p in veg_parts], str(veg)], log=log)

    run_cmd([pdal, "merge", str(ground), str(veg), str(merged)], log=log)

    if crop_bounds:
        xmin, ymin, xmax, ymax = crop_bounds
        cropped = work_dir / "merged_crop.laz"
        if log:
            log(f"PDAL crop bbox 5514 + {CROP_BUFFER_M:.0f} m: [{xmin:.1f},{xmax:.1f}] x [{ymin:.1f},{ymax:.1f}]")
        run_cmd(
            [
                pdal,
                "translate",
                str(merged),
                str(cropped),
                "crop",
                f"--filters.crop.bounds=([{xmin},{xmax}],[{ymin},{ymax}])",
            ],
            log=log,
        )
        merged = cropped

    info = subprocess.run(
        [pdal, "info", str(merged), "--stats"],
        capture_output=True,
        text=True,
        check=False,
        env=gis_subprocess_env(pdal),
    )
    if log:
        tail = (info.stdout or info.stderr)[-600:]
        log(tail)
        log(f"OK merged.laz ({merged.stat().st_size / 1e6:.1f} MB)")

    return merged
