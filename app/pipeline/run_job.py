from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app import db
from app.pipeline.contours_gdal import generate_job_contours
from app.pipeline.fetch_aopk import fetch_aopk_trees_for_bbox
from app.pipeline.fetch_openzu import (
    crop_bounds_5514,
    fetch_lidar_for_bbox,
)
from app.pipeline.fetch_ruian import fetch_ruian_buildings_for_bbox
from app.pipeline.fetch_zabaged import fetch_zabaged_for_bbox
from app.pipeline.ini_builder import load_presets, write_pullauta_ini
from app.pipeline.karttapullautin_dxf import (
    DXF_SKIP_AFTER_VECTORS,
    prune_heavy_intermediate_dxf,
)
from app.pipeline.osm_paths import (
    ZABAGED_OMIT_FROM_KP,
    ZABAGED_OMIT_PATH_LAYERS,
    prepare_osm_paths,
    write_osm_kp_zip,
    write_osm_manual_shapefiles,
    write_zabaged_omitting_layers,
)
from app.pipeline.package_oom import (
    OUTPUT_ZIP_NAME,
    OOM_PATH_VARIANTS,
    build_oom_zip,
    oom_metadata,
    omap_variant_filename,
    prepare_oom_map,
    resolve_discipline_presets,
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

# Po pádu KP na okraji heightmapy jen mírně rozšířit ořez (nikdy celá SM5).
# Celé listy by zvětšily territory mimo ZABAGED a sprint by trval desítky minut.
_KP_OOB_EXTRA_PADS_M = (50.0, 150.0, 300.0, 600.0)


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
    if reused_from:
        log(
            f"=== Fáze: kopie LAZ z jobu {reused_from} "
            "(mimo request založení, ať UI neodpovídá pozdě) ==="
        )
        copied = db.copy_reusable_work(str(reused_from), job_dir.name)
        if copied:
            log(f"Zkopírováno {len(copied)} souborů (sloučený LAZ).")
        else:
            log("Varování: v předchozím jobu není použitelný LAZ ke kopírování.")

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
        # Early-crop už neukládá plný merged.laz – širší ořez znovu z listů SM5
        # (nebo fallback crop z existujícího LAZ, pokud listy nejsou k dispozici).
        last_exc: BaseException = exc
        recovered = False
        for extra_pad in _KP_OOB_EXTRA_PADS_M:
            log(
                "Karttapullautin spadl na okraji heightmapy (bug KP). "
                f"Zkouším znovu s ořezem +{extra_pad:g} m (bez rozšíření na SM5)…"
            )
            retry_name = f"merged_crop_retry_{int(extra_pad)}.laz"
            if dmr_files and dmp_files:
                merged = merge_dmr_dmp(
                    dmr_files,
                    dmp_files,
                    lidar_work,
                    log=log,
                    crop_bounds=crop,
                    scalefactor=scalefactor,
                    extra_pad_m=extra_pad,
                    output_name=retry_name,
                )
            else:
                wider = kp_pad_crop_bounds(crop, scalefactor, extra_pad_m=extra_pad)
                wider = ensure_contains_bounds(wider, crop)
                src = merged
                for name in ("merged.laz", "merged_crop.laz"):
                    candidate = lidar_work / name
                    if candidate.exists() and candidate.stat().st_size > 1000:
                        src = candidate
                        break
                merged = crop_laz(
                    src,
                    lidar_work / retry_name,
                    wider,
                    log=log,
                )
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            try:
                run_cmd([PULLAUTA_BIN, str(merged.resolve())], cwd=kp_cwd, log=log)
                recovered = True
                break
            except subprocess.CalledProcessError as retry_exc:
                last_exc = retry_exc
                if not is_kp_heightmap_oob(retry_exc):
                    raise
        if not recovered:
            raise last_exc

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

    osm_kp_zip: Path | None = None
    kp_zabaged = zabaged_clean
    if bbox:
        log("=== Fáze: OSM pěšiny a objekty (před KP PNG) ===")
        try:
            prepare_osm_paths(
                work_dir,
                tuple(bbox),
                zabaged_clean,
                include_benches=bool(options.get("kp_osm_benches")),
                include_lamps=bool(options.get("kp_osm_lamps")),
                include_playground_equipment=bool(
                    options.get("kp_osm_playground_equipment")
                ),
                osm_priority=bool(options.get("kp_osm_priority")),
                preset_id=preset_id,
                log=log,
            )
            osm_kp_zip = write_osm_kp_zip(work_dir, log=log)
            write_osm_manual_shapefiles(work_dir, log=log)
        except Exception as exc:
            log(f"OSM: přeskočeno ({exc})")
            osm_kp_zip = None

    log("=== Fáze: Karttapullautin vektory ===")
    # OSM cesty jsou hustší než ZABAGED – do KP bereme plné OSM a ZABAGED cesty vynecháme.
    # OstatniPlochaVSidlech do KP ne – parking|529 přemaluje silnice; v OOM zůstane podklad 501.
    omit_for_kp = set(ZABAGED_OMIT_FROM_KP)
    if osm_kp_zip and osm_kp_zip.is_file():
        omit_for_kp |= set(ZABAGED_OMIT_PATH_LAYERS)
    if omit_for_kp:
        kp_zabaged_filtered = work_dir / "zabaged_kp_filtered.zip"
        try:
            write_zabaged_omitting_layers(
                zabaged_clean, kp_zabaged_filtered, frozenset(omit_for_kp)
            )
            kp_zabaged = kp_zabaged_filtered
            if osm_kp_zip and osm_kp_zip.is_file():
                log("KP PNG: OSM cesty (ZABAGED cesty + Ostatní plocha vynechány)")
            else:
                log("KP PNG: Ostatní plocha vynechána (zůstává v OOM pod silnicemi)")
        except Exception as exc:
            log(f"KP PNG: filtrovaný ZABAGED selhal ({exc}) – beru plný ZABAGED")
            kp_zabaged = zabaged_clean
    kp_vector_cmd = [PULLAUTA_BIN, str(kp_zabaged.resolve())]
    if osm_kp_zip and osm_kp_zip.is_file():
        kp_vector_cmd.append(str(osm_kp_zip.resolve()))
    else:
        log("KP PNG: jen ZABAGED (bez OSM cest)")
    run_cmd(kp_vector_cmd, cwd=kp_cwd, log=log)
    # out2.dxf necháme do zabalení ZIPu (base/contours_kp.dxf), teprve potom smažeme.

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
    prune_heavy_intermediate_dxf(
        temp_dir, log=log, names=DXF_SKIP_AFTER_VECTORS
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

    want_zip = bool(options.get("output_zip", True))
    presets = load_presets()
    preset = presets.get(preset_id, {})
    job_dir = kp_cwd.parent
    reference_dir = kp_cwd / "references"
    ref_layers: list[str] = []
    built_refs: dict[str, Path] = {}
    bbox = options.get("bbox_wgs84")
    omap_path = None
    zabaged = zabaged_clean if zabaged_clean and zabaged_clean.exists() else None

    if want_zip:
        want_refs = bool(options.get("output_references", True))
        if (
            want_refs
            and bbox
            and (kp_cwd / "pullautus.png").is_file()
            and (kp_cwd / "pullautus.pgw").is_file()
        ):
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
        elif not want_refs:
            log("=== Fáze: referenční PNG přeskočeny (volba v GUI) ===")

        meta = oom_metadata(
            preset_id, preset, options, job_name, reference_layers=ref_layers or None
        )
        omap_paths: list[Path] = []
        ruian_path: Path | None = None
        aopk_path: Path | None = None
        if bbox:
            log("=== Fáze: RÚIAN budovy (zabaged/) + AOPK památné stromy ===")
            try:
                ruian_path = fetch_ruian_buildings_for_bbox(tuple(bbox), log=log)
            except Exception as exc:
                log(f"RÚIAN budovy: přeskočeno ({exc})")
                ruian_path = None
            try:
                aopk_path = fetch_aopk_trees_for_bbox(tuple(bbox), log=log)
            except Exception as exc:
                log(f"AOPK stromy: přeskočeno ({exc})")
                aopk_path = None
            indexcontours_m = options.get("indexcontours", preset.get("indexcontours"))
            if indexcontours_m is None and meta.get("contour_interval_m") is not None:
                indexcontours_m = 5 * float(meta["contour_interval_m"])
            courtyard_olive = bool(options.get("sprint_courtyard_olive", True))
            cliff_symbol = str(options.get("kp_cliff_symbol") or "earth_bank")
            include_dxf = bool(options.get("output_dxf", True))
            contour_interval_m = meta.get("contour_interval_m")
            from app.pipeline.fetch_zabaged import (
                ostatni_plocha_max_m2,
                resolve_ostatni_plocha,
            )

            ostatni_choice = resolve_ostatni_plocha(options)
            max_ostatni_m2 = ostatni_plocha_max_m2(ostatni_choice)
            log(
                f"Ostatní plocha v sídlech (auto .omap): {ostatni_choice}"
                + (
                    " (negenerovat)"
                    if max_ostatni_m2 is None
                    else f" (≤ {max_ostatni_m2:g} m²)"
                    if max_ostatni_m2 != float("inf")
                    else " (všechny)"
                )
            )

            for disc_tag, disc_preset_id, scale in resolve_discipline_presets(
                preset_id,
                presets,
                map_scale=options.get("map_scale"),
                contour_interval=options.get("contour_interval"),
            ):
                disc_preset = presets.get(disc_preset_id, {})
                vectorconf = Path(
                    str(disc_preset.get("vectorconf", "zabaged.txt"))
                ).name
                for path_tag, path_src in OOM_PATH_VARIANTS:
                    variant_name = omap_variant_filename(
                        disc_tag, path_tag, map_name=job_name or ""
                    )
                    log(f"OOM: {variant_name} ({disc_preset_id}, 1:{scale}, {path_src})")
                    omap_p = prepare_oom_map(
                        kp_cwd,
                        output_dir / variant_name,
                        map_name=job_name or disc_preset_id,
                        scale=scale,
                        preset_id=disc_preset_id,
                        bbox_wgs84=tuple(bbox),
                        built_refs=built_refs or None,
                        zabaged_clean=zabaged,
                        vectorconf_name=vectorconf,
                        include_dxf=include_dxf,
                        contour_interval_m=contour_interval_m,
                        formline=0,
                        indexcontours_m=indexcontours_m,
                        cliff_symbol=cliff_symbol,
                        courtyard_olive=courtyard_olive,
                        path_source=path_src,
                        aopk_trees=aopk_path,
                        max_ostatni_m2=max_ostatni_m2,
                    )
                    if omap_p:
                        omap_paths.append(omap_p)

        cliff_symbol = str(options.get("kp_cliff_symbol") or "earth_bank")
        build_oom_zip(
            kp_cwd,
            zip_path,
            zabaged_clean=zabaged,
            metadata=meta,
            reference_dir=(
                reference_dir
                if want_refs and reference_dir and reference_dir.is_dir()
                else None
            ),
            omap_paths=omap_paths,
            include_zabaged_archive=bool(
                options.get("output_zabaged_clean", False) and zabaged
            ),
            include_png=bool(options.get("output_png", True)),
            ruian_buildings=ruian_path,
            include_dxf=bool(options.get("output_dxf", True)),
            include_cliffs=cliff_symbol != "off",
        )
    else:
        log("=== Fáze: jen PNG náhled (ZIP/OOM přeskočeno) ===")

    for name in ("pullautus.png", "pullautus.pgw"):
        src = kp_cwd / name
        if src.exists():
            shutil.copy2(src, output_dir / name)
    # Stejná struktura jako v ZIPu, ať jde otevřít i output/*-{sprint,les,mtbo}.omap.
    if want_zip and omap_paths:
        for folder, names in (
            (
                "kp",
                (
                    "pullautus.png",
                    "pullautus.pgw",
                    "pullautus_depr.png",
                    "pullautus_depr.pgw",
                ),
            ),
        ):
            dest_dir = output_dir / folder
            for name in names:
                src = kp_cwd / name
                if src.is_file():
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest_dir / name)
        if bool(options.get("output_references", True)):
            refs_src = kp_cwd / "references"
            if refs_src.is_dir():
                refs_dst = output_dir / "references"
                refs_dst.mkdir(parents=True, exist_ok=True)
                for path in refs_src.glob("*"):
                    if path.is_file() and path.suffix.lower() in {".png", ".pgw"}:
                        shutil.copy2(path, refs_dst / path.name)

    if want_zip and zip_path.is_file():
        log(f"Výstup: {zip_path.name} ({zip_path.stat().st_size / 1e6:.2f} MB)")
    elif (output_dir / "pullautus.png").is_file():
        png = output_dir / "pullautus.png"
        log(f"Výstup: jen PNG náhled ({png.stat().st_size / 1e6:.2f} MB)")
    else:
        log("Výstup: žádný ZIP ani PNG")
