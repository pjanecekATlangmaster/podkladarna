from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

from app.pipeline.fetch_openzu import crop_bounds_5514, fetch_lidar_for_bbox
from app.pipeline.fetch_zabaged import fetch_zabaged_for_bbox
from app.pipeline.ini_builder import load_presets, write_pullauta_ini
from app.pipeline.prepare_lidar import (
    crop_laz,
    is_kp_heightmap_oob,
    kp_safe_crop_bounds,
    merge_dmr_dmp,
    run_cmd,
)
from app.pipeline.prepare_zabaged import clean_zabaged
from app.settings import PULLAUTA_BIN


def _collect_glob(folder: Path, patterns: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    seen: set[str] = set()
    for pat in patterns:
        for path in sorted(folder.glob(pat)):
            key = str(path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
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

    bbox = options.get("bbox_wgs84")
    crop = None
    if bbox:
        west, south, east, north = bbox
        crop = crop_bounds_5514(west, south, east, north)

    dmr_files = _collect_glob(input_dir / "dmr", ("*.laz", "*.las", "*.LAZ", "*.LAS"))
    dmp_files = _collect_glob(input_dir / "dmp", ("*.laz", "*.las", "*.LAZ", "*.LAS"))
    zabaged_src = input_dir / "zabaged" / "Zabaged_full.zip"
    if not zabaged_src.exists():
        zips = list((input_dir / "zabaged").glob("*.zip"))
        zabaged_src = zips[0] if zips else zabaged_src

    lidar_work = work_dir / "lidar"
    merged_existing = None
    for name in ("merged_crop.laz", "merged_crop_retry.laz", "merged.laz"):
        candidate = lidar_work / name
        if candidate.exists() and candidate.stat().st_size > 1000:
            merged_existing = candidate
            break

    if bbox:
        west, south, east, north = bbox
        if merged_existing or (dmr_files and dmp_files):
            log("LiDAR už je v jobu (iterace) – stahování openzu přeskakuji")
        else:
            log("=== Fáze: stahování LiDAR (openzu) ===")
            fetch_lidar_for_bbox((west, south, east, north), input_dir / "dmr", input_dir / "dmp", log)
            dmr_files = _collect_glob(input_dir / "dmr", ("*.laz", "*.las", "*.LAZ", "*.LAS"))
            dmp_files = _collect_glob(input_dir / "dmp", ("*.laz", "*.las", "*.LAZ", "*.LAS"))

        if not zabaged_src.exists():
            log("=== Fáze: stahování ZABAGED (ArcGIS) ===")
            zabaged_src = input_dir / "zabaged" / "Zabaged_ags.zip"
            fetch_zabaged_for_bbox((west, south, east, north), zabaged_src, log)
        elif options.get("reused_from"):
            log(f"ZABAGED z jobu {options['reused_from']} – stahování přeskakuji")

    scalefactor = float(
        options.get("scalefactor") or load_presets()[preset_id]["scalefactor"]
    )
    if merged_existing:
        log(f"Používám už sloučený LAZ ({merged_existing.name}) – PDAL merge přeskakuji")
        merged = merged_existing
    else:
        log("=== Fáze: prepare LiDAR ===")
        merged = merge_dmr_dmp(
            dmr_files,
            dmp_files,
            lidar_work,
            log=log,
            crop_bounds=crop,
            scalefactor=scalefactor,
        )

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
    try:
        run_cmd([PULLAUTA_BIN, str(merged.resolve())], cwd=kp_cwd, log=log)
    except subprocess.CalledProcessError as exc:
        if not crop or not is_kp_heightmap_oob(exc):
            raise
        log(
            "Karttapullautin spadl na okraji heightmapy (bug KP, index mimo pole). "
            "Zkouším znovu s menším ořezem…"
        )
        uncropped = work_dir / "lidar" / "merged.laz"
        src = uncropped if uncropped.exists() else merged
        tight = kp_safe_crop_bounds(crop, scalefactor, extra_inset_m=2.0)
        merged = crop_laz(
            src, work_dir / "lidar" / "merged_crop_retry.laz", tight, log
        )
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
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

        if options.get("output_zabaged_clean", False) and zabaged_clean and zabaged_clean.exists():
            zf.write(zabaged_clean, "zabaged_clean.zip")

    for name in ("pullautus.png", "pullautus.pgw"):
        src = kp_cwd / name
        if src.exists():
            shutil.copy2(src, output_dir / name)

    log(f"Vystup: {zip_path.name} ({zip_path.stat().st_size / 1e6:.2f} MB)")
