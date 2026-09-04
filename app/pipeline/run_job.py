from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.pipeline.contours_gdal import generate_job_contours
from app.pipeline.fetch_openzu import (
    crop_bounds_5514,
    fetch_lidar_for_bbox,
    query_sm5_union_bounds_5514,
)
from app.pipeline.fetch_zabaged import fetch_zabaged_for_bbox
from app.pipeline.ini_builder import load_presets, write_pullauta_ini
from app.pipeline.karttapullautin_dxf import (
    DXF_SKIP_AFTER_VECTORS,
    prune_heavy_intermediate_dxf,
)
from app.pipeline.osm_paths import prepare_osm_paths
from app.pipeline.package_oom import (
    OUTPUT_ZIP_NAME,
    build_oom_zip,
    oom_metadata,
    prepare_oom_map,
)
from app.pipeline.reference_layers import build_reference_layers
from app.pipeline.prepare_lidar import (
    crop_laz,
    ensure_contains_bounds,
    is_kp_heightmap_oob,
    kp_pad_crop_bounds,
    merge_dmr_dmp,
    run_cmd,
)
from app.pipeline.prepare_zabaged import clean_zabaged
from app.pipeline.vegetation_gdal import generate_job_vegetation
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
    reused_from = options.get("reused_from")
    merged_existing = None
    if reused_from:
        for name in ("merged_crop.laz", "merged_crop_retry.laz", "merged.laz"):
            candidate = lidar_work / name
            if candidate.exists() and candidate.stat().st_size > 1000:
                merged_existing = candidate
                break

    if bbox:
        west, south, east, north = bbox
        log("=== Fáze: stažená data (LiDAR) ===")
        dmr_files, dmp_files, _ = fetch_lidar_for_bbox((west, south, east, north), log)

        log("=== Fáze: stažená data (ZABAGED) ===")
        zabaged_src = fetch_zabaged_for_bbox((west, south, east, north), log)

    scalefactor = float(
        options.get("scalefactor") or load_presets()[preset_id]["scalefactor"]
    )
    if reused_from and merged_existing:
        log(
            f"Iterace z jobu {reused_from}: používám sloučený LAZ "
            f"({merged_existing.name}) – PDAL merge přeskakuji"
        )
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
            "Zkouším znovu s větším ořezem (celé listy SM5)…"
        )
        uncropped = work_dir / "lidar" / "merged.laz"
        src = uncropped if uncropped.exists() else merged
        # Nikdy nezmenšovat pod výběr uživatele (+ buffer) – jen rozšířit.
        wider = kp_pad_crop_bounds(crop, scalefactor, extra_pad_m=50.0)
        sheet_names = list(options.get("sm5_sheets") or [])
        try:
            sheet_bounds = (
                query_sm5_union_bounds_5514(sheet_names) if sheet_names else None
            )
        except Exception as sheet_exc:
            sheet_bounds = None
            if log:
                log(f"SM5 envelope: přeskočeno ({sheet_exc})")
        if sheet_bounds is not None:
            wider = ensure_contains_bounds(sheet_bounds, wider)
            if log:
                log(
                    "Ořez LAZ na sjednocení listů SM5 "
                    f"({', '.join(sheet_names) or '?'})"
                )
        wider = ensure_contains_bounds(wider, crop)
        merged = crop_laz(
            src, work_dir / "lidar" / "merged_crop_retry.laz", wider, log=log
        )
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        run_cmd([PULLAUTA_BIN, str(merged.resolve())], cwd=kp_cwd, log=log)

    if not (temp_dir / "vegetation.pgw").exists():
        raise RuntimeError("LiDAR nedokoncil temp/vegetation.pgw")

    prune_heavy_intermediate_dxf(temp_dir, log=log)

    log("=== Fáze: vrstevnice PDAL/GDAL ===")
    preset = load_presets()[preset_id]
    generate_job_contours(
        work_dir,
        merged,
        interval_m=float(
            options["contour_interval"]
            if options.get("contour_interval") is not None
            else preset["contour_interval"]
        ),
        formline=float(
            options["formline"]
            if options.get("formline") is not None
            else preset.get("formline") or 0
        ),
        scalefactor=scalefactor,
        crop_bounds=crop,
        log=log,
    )

    generate_job_vegetation(work_dir, log=log)

    if not has_zabaged or not zabaged_clean.is_file():
        raise RuntimeError(
            "ZABAGED (polohopis) není k dispozici – bez něj nelze dokončit mapu."
        )

    log("=== Fáze: Karttapullautin vektory ===")
    run_cmd([PULLAUTA_BIN, str(zabaged_clean.resolve())], cwd=kp_cwd, log=log)
    prune_heavy_intermediate_dxf(
        temp_dir, log=log, names=DXF_SKIP_AFTER_VECTORS
    )

    if bbox:
        log("=== Fáze: OSM pěšiny ===")
        try:
            prepare_osm_paths(
                work_dir,
                tuple(bbox),
                zabaged_clean if has_zabaged else None,
                log=log,
            )
        except Exception as exc:
            log(f"OSM pěšiny: přeskočeno ({exc})")

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
    built_refs: dict[str, Path] = {}
    bbox = options.get("bbox_wgs84")
    if bbox and (kp_cwd / "pullautus.png").is_file() and (kp_cwd / "pullautus.pgw").is_file():
        log("=== Fáze: referenční podklady pro OOM ===")
        try:
            built_refs = build_reference_layers(
                job_dir,
                tuple(bbox),
                kp_cwd / "pullautus.png",
                kp_cwd / "pullautus.pgw",
                reference_dir,
                log=log,
            )
        except Exception as exc:
            log(f"Referenční podklady: přeskočeno ({exc})")
        if built_refs:
            ref_layers = sorted(p.name for p in built_refs.values())
        elif reference_dir.is_dir():
            ref_layers = sorted(p.name for p in reference_dir.glob("*.png"))

    meta = oom_metadata(preset_id, preset, options, job_name, reference_layers=ref_layers or None)
    zabaged = zabaged_clean if zabaged_clean and zabaged_clean.exists() else None
    omap_path = None
    if bbox:
        vectorconf = Path(str(preset.get("vectorconf", "zabaged.txt"))).name
        indexcontours_m = options.get("indexcontours", preset.get("indexcontours"))
        if indexcontours_m is None and meta.get("contour_interval_m") is not None:
            indexcontours_m = 5 * float(meta["contour_interval_m"])
        omap_path = prepare_oom_map(
            kp_cwd,
            output_dir / "podkladarna.omap",
            map_name=job_name or preset_id,
            scale=meta["scale"],
            preset_id=preset_id,
            bbox_wgs84=tuple(bbox),
            built_refs=built_refs or None,
            zabaged_clean=zabaged,
            vectorconf_name=vectorconf,
            include_dxf=bool(options.get("output_dxf", True)),
            contour_interval_m=meta.get("contour_interval_m"),
            formline=0,
            indexcontours_m=indexcontours_m,
        )

    build_oom_zip(
        kp_cwd,
        zip_path,
        zabaged_clean=zabaged,
        metadata=meta,
        reference_dir=reference_dir if reference_dir and reference_dir.is_dir() else None,
        omap_path=omap_path,
        include_zabaged_archive=bool(options.get("output_zabaged_clean", False) and zabaged),
        include_png=bool(options.get("output_png", True)),
        include_dxf=bool(options.get("output_dxf", True)),
    )

    for name in ("pullautus.png", "pullautus.pgw"):
        src = kp_cwd / name
        if src.exists():
            shutil.copy2(src, output_dir / name)
    # Stejná struktura jako v ZIPu, ať jde otevřít i output/podkladarna.omap.
    if omap_path and omap_path.is_file():
        for folder, names in (
            ("basemap", ("pullautus.png", "pullautus.pgw")),
            ("relief", ("pullautus_depr.png", "pullautus_depr.pgw")),
        ):
            dest_dir = output_dir / folder
            for name in names:
                src = kp_cwd / name
                if src.is_file():
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest_dir / name)
        refs_src = kp_cwd / "references"
        if refs_src.is_dir():
            refs_dst = output_dir / "references"
            refs_dst.mkdir(parents=True, exist_ok=True)
            for path in refs_src.glob("*"):
                if path.is_file() and path.suffix.lower() in {".png", ".pgw"}:
                    shutil.copy2(path, refs_dst / path.name)

    log(f"Výstup: {zip_path.name} ({zip_path.stat().st_size / 1e6:.2f} MB)")
