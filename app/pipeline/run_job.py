from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from app.pipeline.ini_builder import write_pullauta_ini
from app.pipeline.prepare_lidar import merge_dmr_dmp
from app.pipeline.prepare_zabaged import clean_zabaged
from app.pipeline.prepare_lidar import run_cmd
from app.settings import PULLAUTA_BIN


def _collect_glob(folder: Path, patterns: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for pat in patterns:
        files.extend(sorted(folder.glob(pat)))
    return files


def run_job_pipeline(
    job_dir: Path,
    preset_id: str,
    options: dict,
    log: callable,
) -> None:
    input_dir = job_dir / "input"
    work_dir = job_dir / "work"
    output_dir = job_dir / "output"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    dmr_files = _collect_glob(input_dir / "dmr", ("*.laz", "*.las", "*.LAZ", "*.LAS"))
    dmp_files = _collect_glob(input_dir / "dmp", ("*.laz", "*.las", "*.LAZ", "*.LAS"))
    zabaged_src = input_dir / "zabaged" / "Zabaged_full.zip"
    if not zabaged_src.exists():
        zips = list((input_dir / "zabaged").glob("*.zip"))
        zabaged_src = zips[0] if zips else zabaged_src

    log("=== Fáze: prepare LiDAR ===")
    merged = merge_dmr_dmp(dmr_files, dmp_files, work_dir / "lidar", log=log)

    zabaged_clean = work_dir / "zabaged_clean.zip"
    has_zabaged = zabaged_src.exists()
    if has_zabaged:
        log("=== Fáze: prepare ZABAGED ===")
        clean_zabaged(zabaged_src, zabaged_clean, log=log)

    log("=== Fáze: pullauta.ini ===")
    ini_path = write_pullauta_ini(work_dir, preset_id, options)
    log(f"INI: {ini_path.name}")

    kp_cwd = work_dir
    temp_dir = kp_cwd / "temp"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)

    log("=== Fáze: Karttapullautin LiDAR ===")
    run_cmd([PULLAUTA_BIN, str(merged.resolve())], cwd=kp_cwd, log=log)

    if not (temp_dir / "vegetation.pgw").exists():
        raise RuntimeError("LiDAR nedokoncil temp/vegetation.pgw")

    if options.get("run_vectors", True) and has_zabaged:
        log("=== Fáze: Karttapullautin vektory ===")
        run_cmd([PULLAUTA_BIN, str(zabaged_clean.resolve())], cwd=kp_cwd, log=log)

    log("=== Fáze: baleni vystupu ===")
    _package_output(kp_cwd, output_dir, zabaged_clean if has_zabaged else None, options, log)
    log("Hotovo.")


def _package_output(
    kp_cwd: Path,
    output_dir: Path,
    zabaged_clean: Path | None,
    options: dict,
    log: callable,
) -> None:
    zip_path = output_dir / "podkladarna_output.zip"
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("pullautus.png", "pullautus.pgw", "pullautus_depr.png", "pullautus_depr.pgw"):
            p = kp_cwd / name
            if options.get("output_png", True) and p.exists():
                zf.write(p, name)

        temp = kp_cwd / "temp"
        if options.get("output_dxf", True) and temp.exists():
            for p in sorted(temp.glob("*.dxf")):
                zf.write(p, f"temp/{p.name}")

        if options.get("output_zabaged_clean", True) and zabaged_clean and zabaged_clean.exists():
            zf.write(zabaged_clean, "zabaged_clean.zip")

    for name in ("pullautus.png", "pullautus.pgw"):
        src = kp_cwd / name
        if src.exists():
            shutil.copy2(src, output_dir / name)

    log(f"Vystup: {zip_path.name} ({zip_path.stat().st_size / 1e6:.2f} MB)")
