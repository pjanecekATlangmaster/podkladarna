from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.pipeline.fetch_openzu import crop_bounds_5514, fetch_lidar_for_bbox
from app.pipeline.fetch_zabaged import fetch_zabaged_for_bbox
from app.pipeline.ini_builder import load_presets, write_pullauta_ini
from app.pipeline.package_oom import (
    OUTPUT_ZIP_NAME,
    build_oom_zip,
    oom_metadata,
    prepare_oom_map,
)
from app.pipeline.reference_layers import build_reference_layers
from app.pipeline.prepare_lidar import (
    crop_laz,
    is_kp_heightmap_oob,
    kp_safe_crop_bounds,
    merge_dmr_dmp,
    run_cmd,
)
from app.pipeline.prepare_zabaged import clean_zabaged
from app.settings import PULLAUTA_BIN


def run_job_pipeline(
    job_dir: Path,
    preset_id: str,
    options: dict,
    log: callable,
    job_name: str = "",
) -> None:
    work_dir = job_dir / "work"
    output_dir = job_dir / "output"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    bbox = options.get("bbox_wgs84")
    crop = None
    if bbox:
        west, south, east, north = bbox
        crop = crop_bounds_5514(west, south, east, north)

    dmr_files: list[Path] = []
    dmp_files: list[Path] = []
    zabaged_src: Path | None = None

    lidar_work = work_dir / "lidar"
    merged_existing = None
    for name in ("merged_crop.laz", "merged_crop_retry.laz", "merged.laz"):
        candidate = lidar_work / name
        if candidate.exists() and candidate.stat().st_size > 1000:
            merged_existing = candidate
            break

    if bbox:
        west, south, east, north = bbox
        if not merged_existing:
            log("=== Fáze: stažená data (LiDAR) ===")
            dmr_files, dmp_files, _ = fetch_lidar_for_bbox((west, south, east, north), log)
        else:
            log("Používám sloučený LAZ z předchozího běhu – openzu přeskakuji")

        log("=== Fáze: stažená data (ZABAGED) ===")
        zabaged_src = fetch_zabaged_for_bbox((west, south, east, north), log)

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
    has_zabaged = zabaged_src is not None and zabaged_src.is_file()
    if has_zabaged:
        log("=== Fáze: prepare ZABAGED ===")
        clean_zabaged(zabaged_src, zabaged_clean, log=log)

    log("=== Fáze: pullauta.ini ===")
    ini_path = write_pullauta_ini(work_dir, preset_id, options)
    log(f"INI: {ini_path.name}")
    for line in ini_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith(
            (
                "contour_interval=",
                "scalefactor=",
                "formline=",
                "indexcontours=",
                "buildingcolor=",
                "vectorconf=",
            )
        ):
            log(f"  {line}")

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

    if not has_zabaged or not zabaged_clean.is_file():
        raise RuntimeError(
            "ZABAGED (polohopis) není k dispozici – bez něj nelze dokončit mapu."
        )

    log("=== Fáze: Karttapullautin vektory ===")
    run_cmd([PULLAUTA_BIN, str(zabaged_clean.resolve())], cwd=kp_cwd, log=log)

    log("=== Fáze: baleni vystupu ===")
    _package_output(
        kp_cwd,
        output_dir,
        zabaged_clean if has_zabaged else None,
        options,
        log,
        preset_id=preset_id,
        job_name=job_name,
    )
    log("Hotovo.")


def _package_output(
    kp_cwd: Path,
    output_dir: Path,
    zabaged_clean: Path | None,
    options: dict,
    log: callable,
    *,
    preset_id: str,
    job_name: str = "",
) -> None:
    zip_path = output_dir / OUTPUT_ZIP_NAME
    if zip_path.exists():
        zip_path.unlink()

    presets = load_presets()
    preset = presets.get(preset_id, {})
    job_dir = kp_cwd.parent
    reference_dir = kp_cwd / "references"
    ref_layers: list[str] = []
    bbox = options.get("bbox_wgs84")
    if bbox and (kp_cwd / "pullautus.png").is_file() and (kp_cwd / "pullautus.pgw").is_file():
        log("=== Fáze: referenční podklady pro OOM ===")
        try:
            built = build_reference_layers(
                job_dir,
                tuple(bbox),
                kp_cwd / "pullautus.png",
                kp_cwd / "pullautus.pgw",
                reference_dir,
                log=log,
            )
            ref_layers = [p.name for p in built.values()]
        except Exception as exc:
            log(f"Referenční podklady: přeskočeno ({exc})")

    meta = oom_metadata(preset_id, preset, options, job_name, reference_layers=ref_layers or None)
    omap_path = None
    if bbox:
        omap_path = prepare_oom_map(
            kp_cwd,
            output_dir / "podkladarna.omap",
            map_name=job_name or preset_id,
            scale=meta["scale"],
            bbox_wgs84=tuple(bbox),
            reference_dir=reference_dir if ref_layers else None,
        )

    zabaged = zabaged_clean if zabaged_clean and zabaged_clean.exists() else None
    build_oom_zip(
        kp_cwd,
        zip_path,
        zabaged_clean=zabaged,
        metadata=meta,
        reference_dir=reference_dir if ref_layers else None,
        omap_path=omap_path,
        include_zabaged_archive=bool(options.get("output_zabaged_clean", False) and zabaged),
        include_png=bool(options.get("output_png", True)),
        include_dxf=bool(options.get("output_dxf", True)),
    )

    for name in ("pullautus.png", "pullautus.pgw"):
        src = kp_cwd / name
        if src.exists():
            shutil.copy2(src, output_dir / name)

    log(f"Výstup: {zip_path.name} ({zip_path.stat().st_size / 1e6:.2f} MB)")
