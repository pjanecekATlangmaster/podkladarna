from __future__ import annotations

import json
import shutil
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from app import db
from app.download_cache import (
    force_refresh_enabled,
    lidar_pair_paths,
    lidar_crop_cache_dir,
    persist_lidar_crop,
    surfaces_cache_dir,
    try_restore_lidar_crop,
)
from app.pipeline.contours_gdal import generate_job_contours, qa_contours_vs_shared_dem
from app.pipeline.dem_prep import SurfacesPrep, start_job_surfaces
from app.pipeline.fetch_aopk import fetch_aopk_trees_for_bbox
from app.pipeline.fetch_openzu import (
    crop_bounds_5514,
    fetch_lidar_for_bbox,
)
from app.pipeline.fetch_ruian import fetch_ruian_buildings_for_bbox
from app.pipeline.fetch_zabaged import fetch_zabaged_for_bbox
from app.pipeline.job_options import load_presets, resolve_vege_height
from app.pipeline.job_grid import DEFAULT_RESOLUTION_M, write_job_grid
from app.pipeline.osm_paths import (
    prepare_osm_paths,
    write_osm_manual_shapefiles,
)
from app.pipeline.oom_preview import (
    build_georef_previews_zip,
    oom_preview_enabled,
    output_georef_enabled,
    write_job_oom_preview,
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
from app.pipeline.preview import (
    compose_job_preview,
    ensure_georef_template,
    resolve_preview_png,
)
from app.pipeline.reference_layers import build_reference_layers
from app.pipeline.shade import build_job_shade
from app.pipeline.job_progress import JobProgress, plan_pipeline_steps
from app.pipeline.prepare_lidar import (
    log_step,
    merge_dmr_dmp,
    resolve_merge_crop_bounds,
)
from app.pipeline.prepare_zabaged import clean_zabaged
from app.pipeline.source_meta import collect_lidar_source_meta
from app.pipeline.cliffs_dem import generate_job_cliffs_dem
from app.pipeline.knolls_dem import generate_job_knolls
from app.pipeline.vegetation_chm import generate_job_vegetation_chm
from app.pipeline.vegetation_density import (
    DEFAULT_PARAMS as DEFAULT_DENSITY_PARAMS,
    generate_job_vegetation_density,
)


@dataclass
class ReferenceDownload:
    """Background stahování referenčních PNG (1 thread × celá sériová sada)."""

    future: Future
    reference_dir: Path
    _executor: ThreadPoolExecutor

    def result(self) -> dict[str, Path]:
        try:
            return self.future.result()
        finally:
            self._executor.shutdown(wait=False)


def _locked_log(log: callable, lock: threading.Lock) -> callable:
    def _log(msg: str) -> None:
        with lock:
            log(msg)

    return _log


def _prefixed_log(log: callable, prefix: str) -> callable:
    """Log z vlákna na pozadí s prefixem (řádky se prolínají s hlavním během)."""
    lock = threading.Lock()

    def _log(msg: str) -> None:
        with lock:
            log(f"{prefix}{msg}" if msg else msg)

    return _log


def _ref_prefixed_log(log: callable, lock: threading.Lock) -> callable:
    """Detailní řádky z backgroundu: prefix [ref] + atomický zápis."""

    def _log(msg: str) -> None:
        line = f"[ref] {msg}" if msg else msg
        with lock:
            log(line)

    return _log


def start_reference_download(
    job_dir: Path,
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    force_refresh: bool,
    log: callable,
) -> ReferenceDownload | None:
    """Spustí sériové ``build_reference_layers`` na pozadí (po ``write_job_grid``).

    Vrací handle pro join v ``_package_output``, nebo None když chybí georef šablona.
    ČÚZK/vrstvy uvnitř zůstávají sériové — paralelní je jen overlap s hlavním buildem.
    """
    georef = ensure_georef_template(work_dir, prefer_preview=False)
    if georef is None:
        return None
    template_png, template_pgw = georef
    reference_dir = work_dir / "references"
    log_lock = threading.Lock()
    ref_log = _ref_prefixed_log(log, log_lock)
    start_log = _locked_log(log, log_lock)
    start_log(
        "Referenční podklady: začínám stahování na pozadí "
        "(ČÚZK/OSM, sériově; paralelně s buildem)."
    )
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ref-dl")
    future = executor.submit(
        build_reference_layers,
        job_dir,
        tuple(bbox_wgs84),
        template_png,
        template_pgw,
        reference_dir,
        log=ref_log,
        force_refresh=force_refresh,
    )
    return ReferenceDownload(
        future=future,
        reference_dir=reference_dir,
        _executor=executor,
    )


def join_reference_download(
    ref_download: ReferenceDownload | None,
    *,
    log: callable,
    want_refs: bool,
    bbox,
    work_dir: Path,
    job_dir: Path,
    force_refresh: bool,
    progress: JobProgress | None = None,
) -> tuple[dict[str, Path], list[str]]:
    """Tvrdý join před prepare_oom_map. Nespuští download dvakrát, když future běží.

    Chyba refs = log + prázdné built_refs (job pokračuje). Fallback: sériová cesta,
    pokud background nebyl nastartován (chyběla mřížka / georef).
    ``JobProgress.begin``/``done`` jen zde (ne z backgroundu).
    """
    built_refs: dict[str, Path] = {}
    reference_dir = work_dir / "references"
    if not want_refs:
        log("Referenční PNG přeskočeny (volba v GUI)")
        return built_refs, []
    if not bbox:
        return built_refs, []

    if ref_download is None:
        georef = ensure_georef_template(work_dir)
        if georef is None:
            if progress is not None:
                progress.skip(
                    "referenční podklady",
                    reason="chybí georef šablona",
                )
            else:
                log(
                    "=== Fáze: referenční PNG přeskočeny "
                    "(chybí georef šablona preview/job_grid) ==="
                )
            return built_refs, []
        if progress is not None:
            progress.begin("referenční podklady")
        else:
            log("=== Fáze: referenční podklady pro OOM ===")
        template_png, template_pgw = georef
        try:
            built_refs = build_reference_layers(
                job_dir,
                tuple(bbox),
                template_png,
                template_pgw,
                reference_dir,
                log=log,
                force_refresh=force_refresh,
            )
        except Exception as exc:
            log(f"Referenční podklady: přeskočeno ({exc})")
            built_refs = {}
    else:
        reference_dir = ref_download.reference_dir
        if progress is not None:
            progress.begin("referenční podklady")
        else:
            log("=== Fáze: referenční podklady pro OOM ===")
        if not ref_download.future.done():
            log("Referenční podklady: čekám na dokončení stahování na pozadí…")
        try:
            built_refs = ref_download.result() or {}
            if built_refs:
                names = ", ".join(sorted(built_refs.keys()))
                log(f"Referenční podklady: staženo na pozadí — {names}")
            else:
                log("Referenční podklady: staženo na pozadí — nic")
        except Exception as exc:
            log(
                f"Referenční podklady: pozadí selhalo ({exc}); "
                "pokračuji bez nich."
            )
            built_refs = {}

    ref_layers: list[str] = []
    if built_refs:
        ref_layers = sorted(p.name for p in built_refs.values())
    elif reference_dir.is_dir():
        ref_layers = sorted(p.name for p in reference_dir.glob("*.png"))
    if progress is not None:
        progress.done()
    return built_refs, ref_layers


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
    sheet_names: list[str] = []
    zabaged_src: Path | None = None
    force_refresh = force_refresh_enabled(options)
    if force_refresh:
        log("Force refresh zapnut – AOI/underlay cache se přeskočí")

    want_zip, _ = resolve_want_zip(options)
    want_refs = bool(options.get("output_references", True))
    reused_from = options.get("reused_from")
    progress = JobProgress(
        log,
        plan_pipeline_steps(
            bbox=bbox,
            reused_from=reused_from,
            want_zip=want_zip,
            want_refs=want_refs,
        ),
    )

    lidar_work = work_dir / "lidar"
    if reused_from:
        progress.begin("kopie LAZ z předchozího jobu")
        log(
            f"Kopie LAZ z jobu {reused_from} "
            "(mimo request založení, ať UI neodpovídá pozdě)"
        )
        copied = db.copy_reusable_work(str(reused_from), job_dir.name)
        if copied:
            log(f"Zkopírováno {len(copied)} souborů (sloučený LAZ).")
        else:
            log("Varování: v předchozím jobu není použitelný LAZ ke kopírování.")
        progress.done()

    merged_existing = None
    if reused_from:
        pair = lidar_pair_paths(lidar_work)
        if pair is not None:
            merged_existing = pair[0]

    if bbox:
        west, south, east, north = bbox
        progress.begin("stažení LiDAR")
        dmr_files, dmp_files, sheet_names = fetch_lidar_for_bbox(
            (west, south, east, north),
            log,
            force_refresh=force_refresh,
        )
        progress.done()

        progress.begin("stažení ZABAGED")
        zabaged_src = fetch_zabaged_for_bbox(
            (west, south, east, north),
            log,
            force_refresh=force_refresh,
        )
        progress.done()

    scalefactor = float(
        options.get("scalefactor") or load_presets()[preset_id]["scalefactor"]
    )

    bbox_tuple = tuple(bbox) if bbox else None
    surfaces_cache: Path | None = None
    crop_cache: Path | None = None
    if bbox_tuple is not None:
        surfaces_cache = surfaces_cache_dir(
            bbox_tuple,
            resolution_m=DEFAULT_RESOLUTION_M,
            sheet_ids=sheet_names or options.get("sm5_sheets"),
            scalefactor=scalefactor,
        )
        crop_cache = lidar_crop_cache_dir(
            bbox_tuple,
            scalefactor=scalefactor,
            sheet_ids=sheet_names or options.get("sm5_sheets"),
        )

    # Kanonická mřížka dřív než DEM deriváty.
    grid_bounds = resolve_merge_crop_bounds(crop, scalefactor)
    ref_download: ReferenceDownload | None = None
    if grid_bounds is not None:
        progress.begin("georef mřížka")
        write_job_grid(
            work_dir,
            grid_bounds,
            resolution_m=DEFAULT_RESOLUTION_M,
            log=log,
        )
        progress.done()
        # Referenční PNG na pozadí (sériově ČÚZK) × overlap s těžkým buildem.
        if want_refs and bbox:
            ref_download = start_reference_download(
                job_dir,
                work_dir,
                tuple(bbox),
                force_refresh=force_refresh,
                log=log,
            )

    if reused_from and merged_existing:
        log(
            f"Iterace z jobu {reused_from}: používám sloučený LAZ "
            f"({merged_existing.name}) – PDAL merge přeskakuji"
        )
        merged = merged_existing
        progress.skip("prepare LiDAR", reason="reuse")
    else:
        cached_merged = None
        if crop_cache is not None and not force_refresh:
            cached_merged = try_restore_lidar_crop(
                crop_cache,
                lidar_work,
                force=force_refresh,
                log=log,
            )
        if cached_merged is not None:
            merged = cached_merged
            progress.skip("prepare LiDAR", reason="cache")
        else:
            progress.begin("prepare LiDAR")
            merged = merge_dmr_dmp(
                dmr_files,
                dmp_files,
                lidar_work,
                log=log,
                crop_bounds=crop,
                scalefactor=scalefactor,
                force_refresh=force_refresh,
            )
            if crop_cache is not None and bbox_tuple is not None:
                persist_lidar_crop(
                    crop_cache,
                    lidar_work,
                    bbox_wgs84=bbox_tuple,
                    sheet_ids=sheet_names or options.get("sm5_sheets"),
                    scalefactor=scalefactor,
                    log=log,
                )
            progress.done()

    surfaces: SurfacesPrep | None = None
    if grid_bounds is not None:
        progress.begin("DEM/DSM/CHM")
        try:
            # DEM hned; DSM/CHM z DMP na pozadí (překryv s vrstevnicemi a
            # průchodem vegetace) – join před čtením chm.tif.
            surfaces = start_job_surfaces(
                work_dir,
                grid_bounds,
                resolution_m=DEFAULT_RESOLUTION_M,
                cache_dir=surfaces_cache,
                force_refresh=force_refresh,
                log=log,
                surface_log=_prefixed_log(log, "[DSM] "),
            )
            if not surfaces.done():
                log("DSM/CHM: běží na pozadí souběžně s dalšími kroky.")
        except Exception as exc:
            # Nehard-fail: vegetace/srázy mají vlastní fallbacky.
            log(f"DEM/DSM/CHM prep: přeskočeno ({exc})")
        progress.done()

    def _wait_surfaces() -> None:
        """Dokončí DSM/CHM větev; chyba = stejný log jako dřív, job jede dál."""
        nonlocal surfaces
        if surfaces is None:
            return
        pending, surfaces = surfaces, None
        if not pending.done():
            log("DSM/CHM: čekám na dokončení výpočtu na pozadí…")
        try:
            pending.wait()
        except Exception as exc:
            log(f"DEM/DSM/CHM prep: přeskočeno ({exc})")

    lidar_sources = (
        collect_lidar_source_meta(sheet_names) if sheet_names else None
    )
    if lidar_sources:
        options = {**options, "_lidar_sources": lidar_sources}
        source_meta_path = work_dir / "source_meta.json"
        source_meta_path.write_text(
            json.dumps(lidar_sources, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if lidar_sources.get("dmp_degraded"):
            log(
                "VAROVÁNÍ: DMP 1G místo DMP OK u alespoň jednoho listu "
                f"(režim={lidar_sources.get('dmp_mode')}) – degradace je viditelná."
            )

    zabaged_clean = work_dir / "zabaged_clean.zip"
    has_zabaged = zabaged_src is not None and zabaged_src.is_file()
    if has_zabaged:
        progress.begin("prepare ZABAGED")
        clean_zabaged(zabaged_src, zabaged_clean, log=log)
        progress.done()
    elif bbox:
        progress.skip("prepare ZABAGED", reason="chybí zdroj")

    work_cwd = work_dir
    temp_dir = work_cwd / "temp"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)

    progress.begin("vrstevnice")
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
    progress.done()

    from app.pipeline.veg_size_filter import resolve_veg_size_profile

    veg_size_profile = resolve_veg_size_profile(
        options,
        preset_id=preset_id,
        map_scale=int(round(10000 * float(scalefactor)))
        if scalefactor
        else None,
    )
    if veg_size_profile != "default":
        log(f"Vegetace: filtr velikosti/tvaru = {veg_size_profile}")

    progress.begin("vegetace")
    vege_shp = None
    try:
        vege_shp = generate_job_vegetation_density(
            work_dir,
            params=replace(
                DEFAULT_DENSITY_PARAMS,
                green_high_m=resolve_vege_height(options),
            ),
            log=log,
            veg_size_profile=veg_size_profile,
            wait_chm=_wait_surfaces,
        )
    except Exception as exc:
        log(f"Vegetace (hustota bodů): přeskočeno ({exc})")
    # CHM fallback i další kroky (hillshade → AOI cache) potřebují hotové DSM/CHM.
    _wait_surfaces()
    if vege_shp is None:
        try:
            generate_job_vegetation_chm(
                work_dir, log=log, veg_size_profile=veg_size_profile
            )
        except Exception as exc:
            log(f"CHM vegetace: přeskočeno ({exc})")
    progress.done()

    progress.begin("srázy DEM")
    try:
        generate_job_cliffs_dem(work_dir, options=options, log=log)
    except Exception as exc:
        log(f"Srázy DEM: přeskočeno ({exc})")
    progress.done()

    progress.begin("knolly")
    try:
        generate_job_knolls(work_dir, options=options, log=log)
    except Exception as exc:
        log(f"Knolly DEM: přeskočeno ({exc})")
    progress.done()

    if not has_zabaged or not zabaged_clean.is_file():
        raise RuntimeError(
            "ZABAGED (polohopis) není k dispozici – bez něj nelze dokončit mapu."
        )

    if bbox:
        progress.begin("OSM pěšiny a objekty")
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
                all_footways_as_sidewalk=bool(
                    options.get("kp_osm_footway_as_sidewalk")
                ),
                preset_id=preset_id,
                force_refresh=force_refresh,
                log=log,
            )
            write_osm_manual_shapefiles(work_dir, log=log)
        except Exception as exc:
            log(f"OSM: přeskočeno ({exc})")
        progress.done()

    # Shade vždy (OOM/ZIP + ČÚZK reference). Náhled PNG z .omap po sestavení.
    progress.begin("hillshade")
    try:
        build_job_shade(
            work_dir,
            bounds_5514=grid_bounds,
            prefer_local=False,
            force=force_refresh,
            cache_dir=surfaces_cache,
            log=log,
        )
        qa_contours_vs_shared_dem(work_dir, log=log)
    except Exception as exc:
        log(f"Hillshade stack: přeskočeno ({exc})")
    compose_preview = bool(options.get("compose_preview", False))
    if compose_preview:
        try:
            tint = work_dir / "vegetation" / "chm_tint.png"
            compose_job_preview(
                work_dir,
                force=True,
                overlay_png=tint if tint.is_file() else None,
                overlay_opacity=0.30,
                bounds_5514=grid_bounds,
                log=log,
            )
        except Exception as exc:
            log(f"Náhled compose: přeskočeno ({exc})")
    elif oom_preview_enabled(options):
        log(
            "Náhled PNG: hillshade compose přeskočen; "
            "web/ZIP náhled vznikne z .omap po sestavení mapy. "
            "ČÚZK reference v ZIPu zůstávají."
        )
    else:
        log(
            "Náhled PNG: přeskočen – primární výstup je OOM/ZIP; "
            "ČÚZK reference v ZIPu zůstávají. Opt-in: compose_preview=1 / oom_preview=1."
        )
    progress.done()

    _package_output(
        work_cwd,
        output_dir,
        zabaged_clean if has_zabaged else None,
        options,
        log,
        preset_id=preset_id,
        job_name=job_name,
        progress=progress,
        ref_download=ref_download,
    )
    progress.finish_all()
    log("Hotovo.")


def resolve_want_zip(options: dict) -> tuple[bool, str | None]:
    """Primární výstup je .omap/ZIP. Jen-PNG bez OOM by nemělo na co ukázat."""
    want = bool(options.get("output_zip", True))
    if not want:
        return True, (
            "Režim jen-PNG se nepoužije — primární výstup je .omap/ZIP."
        )
    return want, None


def _package_output(
    kp_cwd: Path,
    output_dir: Path,
    zabaged_clean: Path | None,
    options: dict,
    log: callable,
    *,
    preset_id: str,
    job_name: str = "",
    progress: JobProgress | None = None,
    ref_download: ReferenceDownload | None = None,
) -> None:
    zip_path = output_dir / OUTPUT_ZIP_NAME
    if zip_path.exists():
        zip_path.unlink()

    want_zip, zip_note = resolve_want_zip(options)
    if zip_note:
        log(zip_note)
    presets = load_presets()
    preset = presets.get(preset_id, {})
    job_dir = kp_cwd.parent
    reference_dir = kp_cwd / "references"
    ref_layers: list[str] = []
    built_refs: dict[str, Path] = {}
    bbox = options.get("bbox_wgs84")
    zabaged = zabaged_clean if zabaged_clean and zabaged_clean.exists() else None

    if want_zip:
        want_refs = bool(options.get("output_references", True))
        built_refs, ref_layers = join_reference_download(
            ref_download,
            log=log,
            want_refs=want_refs,
            bbox=bbox,
            work_dir=kp_cwd,
            job_dir=job_dir,
            force_refresh=force_refresh_enabled(options),
            progress=progress,
        )
        if ref_download is not None:
            reference_dir = ref_download.reference_dir

        meta = oom_metadata(
            preset_id,
            preset,
            options,
            job_name,
            reference_layers=ref_layers or None,
            lidar_sources=options.get("_lidar_sources"),
        )
        meta["output_georef"] = output_georef_enabled(options)
        omap_paths: list[Path] = []
        ruian_path: Path | None = None
        aopk_path: Path | None = None
        if bbox:
            if progress is not None:
                progress.begin("RÚIAN a AOPK")
            else:
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
            if progress is not None:
                progress.done()

            if progress is not None:
                progress.begin("OOM / ZIP")
            indexcontours_m = options.get("indexcontours", preset.get("indexcontours"))
            if indexcontours_m is None and meta.get("contour_interval_m") is not None:
                indexcontours_m = 5 * float(meta["contour_interval_m"])
            courtyard_olive = bool(options.get("sprint_courtyard_olive", True))
            cliff_symbol = str(options.get("kp_cliff_symbol") or "auto")
            include_dxf = bool(options.get("output_dxf", True))
            contour_interval_m = meta.get("contour_interval_m")
            from app.pipeline.fetch_zabaged import (
                ostatni_plocha_max_m2,
                resolve_ostatni_plocha,
            )
            from app.pipeline.residual_paved import (
                residual_max_m2,
                resolve_residual_size,
            )

            ostatni_choice = resolve_ostatni_plocha(options)
            max_ostatni_m2 = ostatni_plocha_max_m2(ostatni_choice)
            ostatni_as_403 = bool(options.get("ostatni_plocha_as_403"))
            residual_paved = bool(options.get("sprint_residual_paved"))
            residual_size = resolve_residual_size(options)
            max_residual_m2 = residual_max_m2(residual_size)
            log(
                f"Ostatní plocha v sídlech (auto .omap): {ostatni_choice}"
                + (
                    " (negenerovat)"
                    if max_ostatni_m2 is None
                    else f" (≤ {max_ostatni_m2:g} m²)"
                    if max_ostatni_m2 != float("inf")
                    else " (všechny)"
                )
                + ("; značka 403" if ostatni_as_403 and max_ostatni_m2 is not None else "")
            )
            if residual_paved:
                size_note = (
                    "všechny husté"
                    if max_residual_m2 == float("inf")
                    else f"≤ {max_residual_m2:g} m²"
                )
                log(
                    f"OSM residential → zbytek zpevněné (501): zapnuto "
                    f"(auto .omap {size_note}; SHP všechna pásma + řídké)"
                )

            log_step(
                log,
                "Sestavuji mapu OOM (vrstevnice, zeleň a polohopis do .omap)",
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
                        ostatni_as_403=ostatni_as_403,
                        residual_paved=residual_paved,
                        max_residual_m2=max_residual_m2,
                        log=log,
                    )
                    if omap_p:
                        omap_paths.append(omap_p)
                        try:
                            from app.pipeline.oom_preview import convert_omap_to_ocd

                            convert_omap_to_ocd(omap_p, log=log)
                        except Exception as exc:
                            log(f"OOM OCD: přeskočeno ({variant_name}): {exc}")
                    else:
                        log(f"OOM: {variant_name} nevytvořeno (prepare_oom_map vrátil None)")

            try:
                # Web/georef náhled: vnitřní timeout + catch; selhání nesmí
                # blokovat ZIP/OCD (ostrý hang b15bc8d21895: bomb→fallback).
                write_job_oom_preview(
                    omap_paths,
                    kp_cwd,
                    output_dir,
                    options,
                    log=log,
                )
            except Exception as exc:
                log(f"OOM náhled: přeskočeno ({exc}) – pokračuji ZIP/OCD")

            if output_georef_enabled(options):
                try:
                    build_georef_previews_zip(output_dir, log=log)
                except Exception as exc:
                    log(f"OOM georef ZIP: přeskočeno ({exc})")

        if progress is not None and not bbox:
            progress.begin("OOM / ZIP")
        cliff_symbol = str(options.get("kp_cliff_symbol") or "auto")
        log_step(
            log,
            "Balím výstupní ZIP (mapy, vektory, referenční podklady – u velkých "
            "výřezů i několik minut)",
        )
        georef_dir = (
            output_dir / "preview"
            if output_georef_enabled(options)
            else None
        )
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
            georef_preview_dir=georef_dir,
        )
    else:
        if progress is not None:
            progress.begin("balení výstupu")
        else:
            log("=== Fáze: jen PNG náhled (ZIP/OOM přeskočeno) ===")

    for name in ("preview.png", "preview.pgw"):
        src = kp_cwd / name
        if src.exists():
            shutil.copy2(src, output_dir / name)
    shade_src = kp_cwd / "shade"
    if shade_src.is_dir():
        shade_dst = output_dir / "shade"
        for name in ("hillshade.png", "hillshade.pgw"):
            src = shade_src / name
            if src.is_file():
                shade_dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, shade_dst / name)
    # Stejná struktura jako v ZIPu, ať jde otevřít i output/*-{sprint,les,mtbo}.omap.
    if want_zip and omap_paths:
        if bool(options.get("output_references", True)):
            refs_src = kp_cwd / "references"
            if refs_src.is_dir():
                refs_dst = output_dir / "references"
                refs_dst.mkdir(parents=True, exist_ok=True)
                for path in refs_src.glob("*"):
                    if path.is_file() and path.suffix.lower() in {".png", ".pgw"}:
                        shutil.copy2(path, refs_dst / path.name)
        # Vegetace / srázy / skály (použité vs vyhozené) – stejné jako v ZIPu.
        try:
            from app.pipeline.uzitecne_vectors import (
                copy_uzitecne_to_output,
                finalize_uzitecne_vectors,
            )

            finalize_uzitecne_vectors(kp_cwd, log=log)
            n_uz = copy_uzitecne_to_output(kp_cwd, output_dir)
            if n_uz and log:
                log(f"Výstup: uzitecne/ ({n_uz} souborů)")
        except Exception as exc:
            if log:
                log(f"uzitecne/: přeskočeno ({exc})")

    if want_zip and zip_path.is_file():
        log(f"Výstup: {zip_path.name} ({zip_path.stat().st_size / 1e6:.2f} MB)")
    elif resolve_preview_png(output_dir) is not None:
        png = resolve_preview_png(output_dir)
        assert png is not None
        log(f"Výstup: jen PNG náhled ({png.stat().st_size / 1e6:.2f} MB)")
    else:
        log("Výstup: žádný ZIP ani PNG")
    if progress is not None:
        progress.finish_all()
