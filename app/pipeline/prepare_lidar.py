from __future__ import annotations

import subprocess
from pathlib import Path

from app.tool_env import gis_subprocess_env, which_tool

# Menší než tohle = prázdný / nepoužitelný LAZ (stejný práh jako reuse v run_job).
MIN_LAZ_BYTES = 1000


def kp_grid_scale_m(scalefactor: float) -> float:
    """Karttapullautin: scale = 2 * scalefactor (buňka heightmapy v metrech)."""
    return 2.0 * float(scalefactor)


def expand_crop_bounds(
    bounds: tuple[float, float, float, float],
    pad_m: float,
) -> tuple[float, float, float, float]:
    """Rozšíří bbox o pad_m na všechny strany (nikdy nezmenšuje)."""
    xmin, ymin, xmax, ymax = bounds
    pad = max(0.0, float(pad_m))
    return xmin - pad, ymin - pad, xmax + pad, ymax + pad


def ensure_contains_bounds(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Vrátí envelope, který pokrývá outer i inner."""
    return (
        min(outer[0], inner[0]),
        min(outer[1], inner[1]),
        max(outer[2], inner[2]),
        max(outer[3], inner[3]),
    )


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


# Zpětná kompatibilita: dřívější inset by zmenšoval výběr – teď jen pad ven.
def kp_safe_crop_bounds(
    bounds: tuple[float, float, float, float],
    scalefactor: float,
    extra_inset_m: float = 0.0,
) -> tuple[float, float, float, float]:
    return kp_pad_crop_bounds(bounds, scalefactor, extra_pad_m=extra_inset_m)


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


def is_kp_heightmap_oob(exc: BaseException) -> bool:
    parts = [
        str(exc),
        str(getattr(exc, "stderr", "") or ""),
        str(getattr(exc, "stdout", "") or ""),
    ]
    text = "\n".join(parts).lower()
    return "index out of bounds" in text


def _crop_filter_bounds(bounds: tuple[float, float, float, float]) -> str:
    xmin, ymin, xmax, ymax = bounds
    return f"--filters.crop.bounds=([{xmin},{xmax}],[{ymin},{ymax}])"


def crop_laz(
    src: Path,
    dest: Path,
    bounds: tuple[float, float, float, float],
    log: callable | None = None,
) -> Path:
    xmin, ymin, xmax, ymax = bounds
    if log:
        log(
            f"PDAL crop bbox 5514: [{xmin:.1f},{xmax:.1f}] x [{ymin:.1f},{ymax:.1f}]"
        )
    run_cmd(
        [
            find_tool("pdal"),
            "translate",
            str(src),
            str(dest),
            "crop",
            _crop_filter_bounds(bounds),
        ],
        log=log,
    )
    return dest


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
    log: callable | None = None,
) -> Path | None:
    """PDAL translate; při cropu nejdřív ořez, pak filtry. Prázdný list → None."""
    if dest.exists():
        dest.unlink()
    cmd = [pdal, "translate", str(src), str(dest)]
    if crop is not None:
        cmd.append("crop")
    cmd.extend(stages)
    if crop is not None:
        cmd.append(_crop_filter_bounds(crop))
    cmd.extend(stage_opts)
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
    return dest


def _merge_parts(
    pdal: str,
    parts: list[Path],
    dest: Path,
    *,
    log: callable | None = None,
) -> Path:
    if len(parts) == 1:
        # Bez kopírování – jediný list použijeme přímo ve finálním merge.
        return parts[0]
    if dest.exists():
        dest.unlink()
    run_cmd([pdal, "merge", *[str(p) for p in parts], str(dest)], log=log)
    return dest


def merge_dmr_dmp(
    dmr_files: list[Path],
    dmp_files: list[Path],
    work_dir: Path,
    log: callable | None = None,
    crop_bounds: tuple[float, float, float, float] | None = None,
    scalefactor: float | None = None,
    *,
    extra_pad_m: float = 0.0,
    output_name: str | None = None,
) -> Path:
    """Sloučí DMR (ground) + DMP (vegetace).

    Je-li ``crop_bounds``, ořezává **každý list před merge** (stejný finální bbox
    jako dřív: user + buffer + KP pad). Tím se nečtou celé SM5 listy.
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

    ground_parts: list[Path] = []
    for i, dmr in enumerate(dmr_files):
        out = work_dir / f"dmr_ground_{i}.laz"
        part = _translate_sheet(
            pdal,
            dmr,
            out,
            stages=["assign"],
            stage_opts=["--filters.assign.assignment=Classification[:]=2"],
            crop=crop,
            log=log,
        )
        if part is not None:
            ground_parts.append(part)

    veg_parts: list[Path] = []
    for i, dmp in enumerate(dmp_files):
        if log:
            log(f"DMP zdroj {i + 1}/{len(dmp_files)}: {dmp.name}")
        out = work_dir / f"dmp_veg_{i}.laz"
        part = _translate_sheet(
            pdal,
            dmp,
            out,
            stages=["range"],
            stage_opts=["--filters.range.limits=Classification[5:6]"],
            crop=crop,
            log=log,
        )
        if part is not None:
            veg_parts.append(part)

    if not ground_parts:
        raise RuntimeError(
            "Po ořezu nezůstaly žádné DMR (ground) body – výřez mimo listy?"
        )
    if not veg_parts:
        raise RuntimeError(
            "Po ořezu/filtru Classification[5:6] nezůstaly žádné DMP body"
        )

    ground = work_dir / "ground_merged.laz"
    veg = work_dir / "veg_merged.laz"
    if output_name:
        merged = work_dir / output_name
    elif crop is not None:
        merged = work_dir / "merged_crop.laz"
    else:
        merged = work_dir / "merged.laz"

    ground = _merge_parts(pdal, ground_parts, ground, log=log)
    veg = _merge_parts(pdal, veg_parts, veg, log=log)
    if merged.exists():
        merged.unlink()
    run_cmd([pdal, "merge", str(ground), str(veg), str(merged)], log=log)

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
        log(f"OK {merged.name} ({merged.stat().st_size / 1e6:.1f} MB)")

    return merged
