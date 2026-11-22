from __future__ import annotations

import json
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from app import db
from app import settings as app_settings
from app.pipeline import build_stats
from app.pipeline.zip_texts_en import cs_intl_site_url, zip_lang
from app.disk_space import ensure_job_space
from app.download_cache import (
    force_refresh_enabled,
    lidar_pair_paths,
    lidar_crop_cache_dir,
    persist_lidar_crop,
    prune_lidar_cache,
    surfaces_cache_dir,
    try_restore_lidar_crop,
)
from app.pipeline.contours_gdal import generate_job_contours, qa_contours_vs_shared_dem
from app.pipeline.country_profile import (
    ELEVATION_CZ,
    elevation_source,
    job_country,
    job_country_code,
    process_uses_cz_sources,
    set_process_country,
    technical_map_source,
    uses_cz_sources,
)
from app.pipeline.dem_prep import SurfacesPrep, start_job_surfaces
from app.pipeline.fetch_aopk import fetch_aopk_trees_for_bbox
from app.pipeline.fetch_openzu import (
    crop_bounds_5514,
    LidarDownload,
    start_lidar_download,
)
from app.pipeline.fetch_ruian import fetch_ruian_buildings_for_bbox
from app.pipeline.fetch_zabaged import fetch_zabaged_for_bbox
from app.pipeline.job_options import (
    load_presets,
    resolve_formline_mode,
    resolve_map_types,
    resolve_vege_height,
    resolve_osm_basic_objects,
    resolve_osm_objects,
    resolve_use_dtm,
    resolve_veg_smooth,
    resolve_contour_smooth,
)
from app.pipeline.job_grid import DEFAULT_RESOLUTION_M, write_job_grid
from app.pipeline import dtm_summary
from app.pipeline.dtm_import import clear_job_memo, start_dtm_prefetch
from app.pipeline.technical_maps import foreign as foreign_technical_map
from app.pipeline.residual_paved import start_residual_osm_prefetch
from app.pipeline.osm_paths import (
    prepare_osm_paths,
    start_osm_prefetch,
    write_osm_manual_shapefiles,
)
from app.pipeline.oom_preview import (
    build_georef_previews_zip,
    output_georef_enabled,
    write_job_oom_preview,
)
from app.pipeline.clean_map import CLEAN_MAPS, full_variant_name
from app.pipeline.package_oom import (
    OUTPUT_ZIP_NAME,
    BUILD_CERTAIN_MAPS,
    CERTAIN_MAP_NAME_SUFFIX,
    OOM_PATH_VARIANTS,
    oom_path_source,
    build_oom_zip,
    oom_metadata,
    omap_variant_filename,
    prepare_oom_map,
    resolve_discipline_presets,
)
from app.pipeline.discipline_maps import DisciplineTask, MapCall, build_discipline_maps
from app.pipeline.preview import (
    compose_job_preview,
    ensure_georef_template,
    resolve_preview_png,
)
from app.pipeline.reference_layers import (
    ALWAYS_REFERENCE_KEYS,
    add_sentinel2_reference,
    build_reference_layers,
    list_reference_rasters,
)
from app.pipeline.shade import build_job_shade
from app.pipeline.zip_images import withheld_log_line, withheld_sources, withhold_images
from app.pipeline.job_progress import (
    IssueCollector,
    DTM_STEP,
    TECHNICAL_MAP_STEPS,
    omitted_steps,
    PREVIEW_STEP,
    background_task,
    ZIP_STEP,
    JobProgress,
    format_elapsed,
    indented_log,
    log_issue,
    plan_pipeline_steps,
    SOURCE_ELEVATION,
    SOURCE_SURFACE,
    confirm_source,
    reset_confirmed_sources,
    source_confirmed,
)
from app.pipeline.prepare_lidar import (
    CMD_LOG_PREFIX,
    log_step,
    merge_dmr_dmp,
    resolve_merge_crop_bounds,
)
from app.pipeline.prepare_zabaged import clean_zabaged
from app.pipeline.source_meta import collect_lidar_source_meta
from app.pipeline.cliffs_dem import generate_job_cliffs_dem
from app.pipeline.knolls_dem import generate_job_knolls
from app.pipeline.boulders_dem import generate_job_boulders
from app.pipeline.vegetation_chm import (
    generate_job_vegetation_chm,
    generate_vegetation_from_osm,
    regenerate_leafoff_vegetation,
)
from app.pipeline.vegetation_density import (
    DEFAULT_PARAMS as DEFAULT_DENSITY_PARAMS,
    generate_job_vegetation_density,
)


# Štítky řádků z vláken na pozadí (``[podklady] …``, ``[DSM/CHM] …``).
REF_LOG_LABEL = "podklady"
SURFACE_LOG_LABEL = "DSM/CHM"
OSM_LOG_LABEL = "OSM"
DTM_LOG_LABEL = "DTM"
LIDAR_LOG_LABEL = "LiDAR"
VECTOR_LOG_LABEL = "vektory"


@dataclass
class ReferenceDownload:
    """Background stahování referenčních PNG (1 thread × celá sériová sada)."""

    future: Future
    reference_dir: Path
    _executor: ThreadPoolExecutor
    # Hillshade pro job (work/shade) se stahuje na pozadí jako první krok téhož
    # sériového vlákna; ``shade_done`` se nastaví po jeho dokončení (i neúspěchu).
    shade_done: threading.Event | None = None
    shade_ok: bool = False

    def result(self) -> dict[str, Path]:
        try:
            return self.future.result()
        finally:
            self._executor.shutdown(wait=False)


@dataclass
class VectorPrefetch:
    """Vektorová data stažená na pozadí od začátku jobu (ZABAGED, RÚIAN, AOPK, ulice z katastru).

    ZABAGED běží souběžně se stahováním LiDARu, zbytek sériově ve druhém vlákně. Kroky
    jen převezmou výsledek (``zabaged()``, ``ruian()``, …); chyba = stejná jako při
    stahování v hlavním vlákně (ZABAGED výjimku vyhodí, ostatní vrací None).
    """

    futures: dict[str, Future]

    def _wait(self, key: str, log: callable | None, label: str):
        future = self.futures.get(key)
        if future is None:
            return None
        if not future.done() and log is not None:
            log(f"Čekám na {label} z pozadí…")
        return future.result()

    def zabaged(self, log: callable | None = None) -> Path:
        return self._wait("zabaged", log, "ZABAGED")

    def ruian(self, log: callable | None = None) -> Path | None:
        return self._wait("ruian", log, "RÚIAN budovy")

    def aopk(self, log: callable | None = None) -> Path | None:
        return self._wait("aopk", log, "AOPK stromy")

    def streets(self, log: callable | None = None) -> None:
        self._wait("streets", log, "parcely ulic z katastru")



def technical_map_line(tech_map: str, dtm_on: bool) -> str:
    """Řádek logu o kresbě z technické mapy země (DTM ČÚZK, BGT …)."""
    spec = foreign_technical_map(tech_map)
    if spec is not None:
        return (
            f"Kresba z {spec.short}: ano – {spec.short} ({spec.content}) se stahuje na pozadí, do mapy jde při sestavení."
            if dtm_on
            else f"Kresba z {spec.short}: ne – mapa se kreslí z OSM."
        )
    if tech_map == "cz_dtm":
        return (
            "Kresba z DTM: ano – data DTM (ploty, budovy, chodníky, silnice) se připravují na pozadí, "
            "do mapy jdou při sestavení."
            if dtm_on
            else "Kresba z DTM: ne – mapa se kreslí z OSM a ZABAGED."
        )
    return "Kresba: z OSM (technická mapa pro toto území není)."


# Popis výškových zdrojů země do logu (``elevation`` v ``configs/countries.yaml``).
ELEVATION_LABELS = {"user_upload": "vlastních dat zadavatele", "nl_ahn": "AHN (PDOK)", "sk_ugkk": "DMR 5.0 (ÚGKK SR)", "sk_ugkk_local": "DMR 5.0 a DMP 1.0 (ÚGKK SR)", "ch_swisstopo": "swissALTI3D a swissSURFACE3D (swisstopo)",
    "at_bev": "BEV ALS (1 m)",
    "de_berlin": "DGM1 a DOM1 Berlín (1 m)",
    "pl_gugik": "NMT a NMPT GUGiK (1 m)",
    "gb_ea": "LIDAR Composite DTM a DSM Environment Agency (1 m)",
    "gb_sct": "LiDAR DTM a DSM skotské vlády (Scottish Remote Sensing Portal, 0,5–1 m)",
    "gb_wls": "LiDAR DTM a DSM velšské vlády (DataMapWales, 1 m)",
    "es_nav": "mračna LiDAR 2024 vlády Navarry (5 bodů/m², terén a povrch 1 m)",
    "es_pv": "mračna LiDAR 2017 baskické vlády (geoEuskadi, terén a povrch 1 m)",
    "de_by": "DGM1 a DOM20 LDBV Bayern (1 m, povrch z letních snímků)",
    "de_st": "DGM1 a DOM1 LVermGeo Sachsen-Anhalt (1 m, ALS)",
    "de_mv": "DGM1 a DOM1 LAiV Mecklenburg-Vorpommern (1 m)",
    "de_ni": "DGM1 a DOM1 LGLN Niedersachsen (1 m, ALS)",
    "de_bb": "DGM1 a bDOM1 LGB Brandenburg (1 m, povrch ze snímků)",
    "de_rp": "DGM1 a DOM1 LVermGeo Rheinland-Pfalz (1 m, ALS)",
    "de_bw": "DGM1 a nDOM1 LGL Baden-Württemberg (1 m, povrch ze snímků)",
    "de_sn": "DGM1 a DOM1 GeoSN Sachsen (1 m)",
    "de_he": "DGM1 a DOM1 HVBG Hessen (1 m)",
    "de_sl": "DGM1 a DOM1 LVGL Saarland (1 m)",
    "de_hh": "DGM1 a bDOM LGV Hamburg (1 m, povrch ze snímků)",
    "de_hb": "DGM1 a DOM1 GeoInformation Bremen (1 m)",
    "de_sh": "DGM1 a bDOM20 LVermGeo Schleswig-Holstein (1 m)",
    "de_nrw": "DGM1, DOM1 a nDOM50 Geobasis NRW (1 m, porost = menší ze zimního lidaru a letních snímků)",
    "fr_lidarhd": "LiDAR HD MNT a MNS (IGN, 0,5 m)",
    "ee_maaamet": "DTM a DSM Maa- ja Ruumiamet (1 m)",
    "it_trentino": "LiDAR DTM a DSM Provincia di Trento (0,5 m, 2014)",
    "be_dhmv": "DHMV II DTM a DSM Digitaal Vlaanderen (1 m, 2013–2015)",
    "dk_dhm": "DHM terén a povrch Klimadatastyrelsen (Datafordeler, 1 m)",
    "no_hoyde": "Høydemodell DTM a DOM Kartverket (1 m)",
    "pt_dgt": "LiDAR MDT a MDS DGT (0,5 m, 2024–2025)",
    "si_arso": "klasifikovaného mračna LiDAR ARSO (2011–2015)",
    "us_3dep": "mračna LiDAR USGS 3DEP (QL 0 – QL 2)",
    "fi_mml": "Korkeusmalli MML (2 m) a výška porostu Metsäkeskus (1 m)",
    "lu_act": "LiDAR 2024 MNT a MNS ACT Luxembourg (0,5 m)",
}


def country_sources_line(options: dict) -> str:
    """Řádek logu se zdroji země výřezu mimo Česko (bez výčtu nepoužitých českých zdrojů)."""
    country = job_country(options)
    name = country.name("cs") if country else job_country_code(options)
    tech = technical_map_source(options)
    spec = foreign_technical_map(tech)
    drawing = f"OSM a {spec.short}" if spec is not None else "OSM"
    elevation = elevation_source(options)
    heights = ELEVATION_LABELS.get(elevation, elevation)
    return f"Země výřezu: {name} – kresba z {drawing}, výšky z {heights}."

def start_vector_prefetch(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    force_refresh: bool,
    with_streets: bool,
    log: callable,
) -> VectorPrefetch:
    """Spustí stahování vektorových dat na pozadí (viz ``VectorPrefetch``)."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vec-dl")
    bg_log = background_log(log, VECTOR_LOG_LABEL)
    futures: dict[str, Future] = {
        "zabaged": executor.submit(
            fetch_zabaged_for_bbox, tuple(bbox_wgs84), bg_log, force_refresh=force_refresh
        )
    }
    # Odeslané úlohy doběhnou i po shutdown; vlákna pak skončí sama (i když job spadne dřív).
    executor.shutdown(wait=False)
    # Jedno vlákno sériově: ČÚZK služby nezahlcovat souběžnými dotazy.
    serial = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vec-dl-s")

    def _safe(fn, label):
        def _run():
            try:
                return fn()
            except Exception as exc:  # krok si případně stáhne sám
                bg_log(f"{label}: přeskočeno ({exc})")
                return None

        return _run

    futures["ruian"] = serial.submit(
        _safe(lambda: fetch_ruian_buildings_for_bbox(tuple(bbox_wgs84), log=bg_log), "RÚIAN budovy")
    )
    futures["aopk"] = serial.submit(
        _safe(lambda: fetch_aopk_trees_for_bbox(tuple(bbox_wgs84), log=bg_log), "AOPK stromy")
    )
    if with_streets:
        from app.pipeline.street_parcels import prefetch_street_inputs

        futures["streets"] = serial.submit(
            _safe(lambda: prefetch_street_inputs(work_dir, tuple(bbox_wgs84), log=bg_log), "Ulice z katastru")
        )
    serial.shutdown(wait=False)
    bg_log("Start stahování na pozadí (ZABAGED, RÚIAN, AOPK" + (", parcely ulic)" if with_streets else ")"))
    return VectorPrefetch(futures=futures)


OPEN_PAVED_LOG_LABEL = "Zpevněné plochy"


def start_open_paved_prefetch(work_dir: Path, bbox_wgs84, *, log: callable) -> threading.Thread | None:
    """Vlákno na pozadí: vstupy ``open_paved`` do cache jobu (sestavení mapy na ně počká, chyba nic nezastaví)."""
    from app.pipeline import open_paved

    if not open_paved.OPEN_PAVED:
        return None
    bg_log = background_log(log, OPEN_PAVED_LOG_LABEL)

    def _run() -> None:
        try:
            open_paved.prefetch(work_dir, bbox_wgs84, log=bg_log)
        except Exception as exc:  # sestavení mapy si vstupy případně stáhne samo
            bg_log(f"přeskočeno ({exc})")

    thread = threading.Thread(target=_run, name="open-paved", daemon=True)
    thread.start()
    return thread


def job_has_sprint(preset_id: str, options: dict) -> bool:
    """Job kreslí sprint (podle disciplín z měřítka; sprint má vlastní job)."""
    return any(
        str(disc_preset).startswith("sprint")
        for _tag, disc_preset, _scale in resolve_discipline_presets(
            preset_id,
            load_presets(),
            map_scale=options.get("map_scale"),
            contour_interval=options.get("contour_interval"),
            map_types=resolve_map_types(options),
        )
    )


def job_wants_sentinel(preset_id: str, options: dict) -> bool:
    """Snímky Sentinel-2 do podkladů: jen job s jinou mapou než sprint (Petr 2026-11-20: ve sprintu nepomáhá,
    pixel 10 m je v 1:4000 2,5 mm). Kresba sprintu mimo Česko bere letní NDVI dál (``open_paved``)."""
    if not options.get("output_sentinel"):
        return False
    presets = resolve_discipline_presets(
        preset_id,
        load_presets(),
        map_scale=options.get("map_scale"),
        contour_interval=options.get("contour_interval"),
        map_types=resolve_map_types(options),
    )
    return any(not str(disc_preset).startswith("sprint") for _tag, disc_preset, _scale in presets)


def vegetation_density_params(preset_id: str, options: dict):
    """Parametry vegetace z hustoty bodů pro job.

    Veto 401 pod korunou je ověřené jen na lese / MTBO; sprint beze změny.
    """
    return replace(
        DEFAULT_DENSITY_PARAMS,
        green_high_m=resolve_vege_height(options),
        canopy_veto_chm_m=0.0
        if job_has_sprint(preset_id, options)
        else DEFAULT_DENSITY_PARAMS.canopy_veto_chm_m,
        ortho_rough_open=not job_has_sprint(preset_id, options),
        young_stand_green=not job_has_sprint(preset_id, options),
    )


def down_source_lines(services: list[dict], *, dtm_needed: bool) -> list[tuple[bool, str]]:
    """Řádky logu o zdrojích mimo provoz na startu jobu: ``(chyba jobu?, text)``.

    Výpadek DTM je chyba jobu (⚠, „hotovo s upozorněním“) jen u sprintu kresleného z DTM; les a MTBO se bez DTM
    kreslí plnohodnotně z OSM a ZABAGED, tam je to jen neutrální řádek (Petr 2026-11-13).
    """
    out: list[tuple[bool, str]] = []
    for svc in services:
        state = "má plánovanou odstávku" if svc.get("planned") else "při poslední kontrole neodpovídal"
        if svc.get("needs") == "dtm" and not dtm_needed:
            out.append((False, f"Zdroj {svc['label']} {state} ({svc['detail']}); mapa se kreslí z OSM a ZABAGED."))
        else:
            out.append((True, f"Zdroj {svc['label']} {state} ({svc['detail']}): {svc['impact']}"))
    return out


def pending_source_issue_note(text: str) -> str:
    """Řádek na startu jobu u zdroje, jehož data job umí potvrdit: chyba jobu jen tehdy, když stažení opravdu selže."""
    return f"{text} Ověří se při stahování."


def resolve_source_issues(pending: list[tuple[str | None, str, str]], log: callable, raw_log: callable) -> None:
    """Konec jobu: varování sondy zdrojů ze startu podle skutečného průběhu.

    ``pending`` = ``(role, label, text)``. Zdroj, jehož data job získal (``job_progress.confirm_source``, i přes
    záložní zrcadlo nebo z cache), je jen informace; jinak chyba jobu jako dřív (job a934d7c44276, Overpass 502).
    """
    for role, label, text in pending:
        if source_confirmed(role):
            raw_log(f"Zdroj {label} byl při kontrole na startu dočasně nedostupný, data se nakonec stáhla (záložní zrcadlo, opakovaný pokus nebo mezipaměť).")
        else:
            log_issue(log, text)


def background_log(log: callable, label: str) -> callable:
    """Log vlákna na pozadí: ``[label] …`` od kraje řádku, jen souhrnné hlášky.

    Řádky se prolínají s krokem hlavního vlákna, proto se příkazy nástrojů
    (``> gdal…``) a prázdné řádky zahazují – na pozadí zajímá jen co a s jakým
    výsledkem se stahuje / počítá.
    """
    lock = threading.Lock()
    prefix = f"[{label}] "

    def _log(msg: str) -> None:
        if not msg or str(msg).startswith(CMD_LOG_PREFIX):
            return
        with lock:
            log(prefix + str(msg).strip())

    return _log


# Přesah předstaženého ortofota kolem mřížky mapy (mřížka vegetace z DEM se liší o zlomky metru).
ORTHO_PREFETCH_PAD_M = 10.0


def ortho_prefetch_bounds(grid_bounds, preset_id: str, options: dict):
    """Obálka předstažení ortofota pro louku / 403 (les a MTBO), jinak ``None`` (sprint)."""
    if grid_bounds is None or not vegetation_density_params(preset_id, options).ortho_rough_open:
        return None
    from app.pipeline.ortho_rough import ORTHO_ROUGH_OPEN

    if not ORTHO_ROUGH_OPEN:
        return None
    xmin, ymin, xmax, ymax = (float(v) for v in grid_bounds)
    p = ORTHO_PREFETCH_PAD_M
    return (xmin - p, ymin - p, xmax + p, ymax + p)


def start_reference_download(
    job_dir: Path,
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    force_refresh: bool,
    log: callable,
    shade_bounds: tuple[float, float, float, float] | None = None,
    shade_cache_dir: Path | None = None,
    with_references: bool = True,
    ortho_prefetch_bounds: tuple[float, float, float, float] | None = None,
    sprint_ortho: bool = False,
    sentinel: bool = False,
    zip_ortho: bool = False,
) -> ReferenceDownload | None:
    """Spustí sériové ``build_reference_layers`` na pozadí (po ``write_job_grid``).

    Vrací handle pro join v ``_package_output``, nebo None když chybí georef šablona.
    ČÚZK/vrstvy uvnitř zůstávají sériové — paralelní je jen overlap s hlavním buildem.
    Když je zadané ``shade_bounds``, stáhne se v témže vlákně nejdřív hillshade
    jobu (``work/shade``) a referenční hillshade z něj vznikne bez dalšího stahování.
    ``with_references=False`` (podklady vypnuté ve formuláři): jen hillshade jobu a
    ``ALWAYS_REFERENCE_KEYS`` (vektorový katastr, ortofoto sprintu mimo podklady) – žádný PNG podklad.
    ``ortho_prefetch_bounds`` (les a MTBO): po stínování ortofoto 0,5 m pro louku / 403
    (``ortho_rough``), ať ho vegetace nestahuje souběžně s podkladem.
    ``sentinel``: po podkladech snímky Sentinel-2 do ``references`` (i s vypnutými podklady).
    ``zip_ortho``: ortofoto 0,125 m do podkladů a ZIPu (zapnuté referenční podklady).
    """
    georef = ensure_georef_template(work_dir, prefer_preview=False)
    if georef is None:
        return None
    template_png, template_pgw = georef
    reference_dir = work_dir / "references"
    ref_log = background_log(log, REF_LOG_LABEL)
    if with_references:
        ref_log(
            "Start stahování na pozadí (stínování, ortofoto, OSM, ZTM, katastr); "
            "výsledek se převezme v kroku „referenční podklady“."
        )
    else:
        ref_log(
            "Start stahování na pozadí (stínování, vektorový katastr; "
            "referenční PNG podklady vypnuté)."
        )
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ref-dl")
    shade_done = threading.Event() if shade_bounds is not None else None
    handle = ReferenceDownload(
        future=None,  # type: ignore[arg-type]  # doplněno níže
        reference_dir=reference_dir,
        _executor=executor,
        shade_done=shade_done,
    )

    def _run() -> dict[str, Path]:
        extra: dict = {}
        if shade_bounds is not None:
            try:
                with background_task(ref_log, "Stínování reliéfu (DMR 5G)") as task:
                    shade_png = build_job_shade(
                        work_dir,
                        bounds_5514=shade_bounds,
                        prefer_local=False,
                        force=force_refresh,
                        cache_dir=shade_cache_dir,
                        log=ref_log,
                    )
                    if shade_png is None:
                        task.state = "nedostupné"
                if shade_png is not None:
                    handle.shade_ok = True
                    extra["job_shade"] = shade_png
            except Exception as exc:
                ref_log(f"Stínování reliéfu: selhalo ({exc})")
            finally:
                shade_done.set()
        if ortho_prefetch_bounds is not None:
            try:
                from app.pipeline.ortho_rough import ORTHO_MPP
                from app.pipeline.orthophoto import prefetch_orthophoto

                with background_task(ref_log, "Ortofoto pro vegetaci"):
                    prefetch_orthophoto(work_dir, ortho_prefetch_bounds, ORTHO_MPP, log=ref_log)
            except Exception as exc:  # vegetace si ho případně stáhne sama
                ref_log(f"Ortofoto pro vegetaci: přeskočeno ({exc})")
        try:
            built = build_reference_layers(
                job_dir,
                tuple(bbox_wgs84),
                template_png,
                template_pgw,
                reference_dir,
                log=ref_log,
                force_refresh=force_refresh,
                only=None if with_references else ALWAYS_REFERENCE_KEYS,
                sprint_ortho=sprint_ortho,
                zip_ortho=zip_ortho,
                **extra,
            )
        except Exception as exc:
            ref_log(f"Selhalo ({exc})")
            raise
        if sentinel:
            built = add_sentinel2_reference(dict(built or {}), tuple(bbox_wgs84), reference_dir, log=ref_log)
        ref_log(f"Hotovo ({len(built or {})} vrstev)")
        return built

    handle.future = executor.submit(_run)
    return handle


def _online_only_keys(built_refs: dict[str, Path] | None = None) -> frozenset[str]:
    """Online šablony vedle stažených rastrů: vrstvy bez staženého protějšku a OSM, když osm.png (vlastní
    render, osm_render) nevznikl nebo ho licence do ZIPu nepouští (zip_images)."""
    from app.pipeline.online_templates import ONLINE_ONLY_KEYS
    from app.pipeline.zip_images import is_withheld

    has_osm = built_refs is not None and "osm" in built_refs
    return ONLINE_ONLY_KEYS | ({"osm"} if is_withheld("osm") or not has_osm else set())


def _with_osm_render(
    built_refs: dict[str, Path], bbox, work_dir: Path, reference_dir: Path, log: callable
) -> dict[str, Path]:
    """osm.png z dat OSM (osm_render) místo online šablony z dlaždic, pokud ho podklady ještě nemají
    (mimo Česko vždy, v Česku bez šablony na pozadí); bez georef šablony nic."""
    from app.pipeline.reference_layers import _fetch_osm_png
    from app.pipeline.zip_images import is_withheld

    if "osm" in built_refs:
        return built_refs
    georef = ensure_georef_template(work_dir) if bbox else None
    if georef is None or is_withheld("osm"):
        return built_refs
    template_png, template_pgw = georef
    out = dict(built_refs)
    osm_png = reference_dir / "osm.png"
    reference_dir.mkdir(parents=True, exist_ok=True)
    _fetch_osm_png(out, tuple(bbox), template_png, template_pgw, osm_png, osm_png.with_suffix(".pgw"), log)
    return out


def _drop_online_osm(built_refs: dict[str, Path]) -> dict[str, Path]:
    """S vlastním osm.png online šablonu OSM z dlaždic do .omap nedávat (stejná vrstva dvakrát)."""
    if "osm" not in built_refs:
        return built_refs
    out = {}
    for key, path in built_refs.items():
        if key.startswith("online_osm"):
            Path(path).unlink(missing_ok=True)
            continue
        out[key] = path
    folder = Path(built_refs["osm"]).parent
    tms = folder / "osm_dlazdice.xml"
    if tms.is_file() and not any(
        p.suffix == ".vrt" and "osm_dlazdice.xml" in p.read_text(encoding="utf-8", errors="ignore")
        for p in map(Path, out.values()) if p.is_file()
    ):
        tms.unlink(missing_ok=True)
    return out


def _with_online_templates(
    built_refs: dict[str, Path], bbox, reference_dir: Path, log: callable, *, only_online: bool = False,
    download: bool = False,
) -> dict[str, Path]:
    """Online šablony podkladů (GDAL WMS ``.xml``) místo stažených rastrů (Petr 2026-11-08, výchozí).

    ``only_online``: se staženými rastry jen vrstvy bez staženého protějšku (archivní ortofoto).
    ``download``: mimo Česko se zapnutými staženými podklady šablony rovnou stáhnout jako rastry.
    """
    if not bbox:
        return built_refs
    from app.pipeline.country_profile import process_country
    from app.pipeline.online_templates import write_online_templates

    country = process_country()
    try:
        online = write_online_templates(
            crop_bounds_5514(*bbox),
            reference_dir,
            only=_online_only_keys(built_refs) if only_online else None,
            country=country.code if country else "CZ",
            technical_area=country.technical_map_covers(bbox) if country else True,
        )
    except Exception as exc:
        log_issue(log, f"Online šablony podkladů: přeskočeno ({exc})")
        return built_refs
    if download and online:
        from app.pipeline.online_templates import download_online_templates

        before = len(online)
        log("Stahuji podklady místních služeb (zapnuté „Stáhnout referenční šablony předem“)…")
        online = download_online_templates(online, country.code if country else "CZ", log=log)
        rasters = sum(1 for p in online.values() if p.suffix.lower() in {".jpg", ".png"})
        log(f"Podklady staženy předem: {rasters} rastrů, online zůstalo {len(online) - rasters} šablon (z {before})")
        return {**built_refs, **online}
    log(f"Online šablony podkladů: {len(online)} (stáhnou se až v Mapperu)")
    return {**built_refs, **online}


def _with_vegetation_height(
    built_refs: dict[str, Path], work_dir: Path, reference_dir: Path, log: callable
) -> dict[str, Path]:
    """Obarvená výška vegetace z ``chm.tif`` jobu (jen se staženými podklady, Petr 2026-11-12)."""
    from app.pipeline.vegetation_chm import chm_path
    from app.pipeline.vegetation_height_png import REF_KEY, write_vegetation_height_png

    chm = chm_path(work_dir)
    if chm is None:
        return built_refs
    try:
        png = write_vegetation_height_png(chm, reference_dir, log=log)
    except Exception as exc:
        log_issue(log, f"Výška vegetace: přeskočeno ({exc})")
        return built_refs
    return {**built_refs, REF_KEY: png} if png else built_refs


def _with_terrain_shades(
    built_refs: dict[str, Path], work_dir: Path, reference_dir: Path, log: callable
) -> dict[str, Path]:
    """Mimo Česko: stínování terénu (DTM) a povrchu (DSM) z výšek jobu místo hillshade DMR 5G a náhledu DMP OK."""
    from app.pipeline.terrain_shade_png import write_terrain_shades
    from app.pipeline.vegetation_chm import DEM_DIR_NAME

    return {**built_refs, **write_terrain_shades(work_dir / DEM_DIR_NAME, reference_dir, log=log)}


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
    sprint_ortho: bool = False,
    sentinel: bool = False,
    zip_ortho: bool = False,
) -> tuple[dict[str, Path], list[str]]:
    """Tvrdý join před prepare_oom_map. Nespuští download dvakrát, když future běží.

    Chyba refs = log + prázdné built_refs (job pokračuje). Fallback: sériová cesta,
    pokud background nebyl nastartován (chyběla mřížka / georef).
    ``JobProgress.begin``/``done`` jen zde (ne z backgroundu).
    """
    built_refs: dict[str, Path] = {}
    reference_dir = work_dir / "references"
    if bbox and not process_uses_cz_sources():
        # Mimo Česko: online šablony místních služeb a OSM, výška vegetace z vlastního CHM, Sentinel-2.
        if progress is not None:
            progress.begin("referenční podklady")
        built_refs = _with_vegetation_height(built_refs, work_dir, reference_dir, log)
        built_refs = _with_terrain_shades(built_refs, work_dir, reference_dir, log)
        if sentinel:
            add_sentinel2_reference(built_refs, tuple(bbox), reference_dir, log=log)
        built_refs = _with_osm_render(built_refs, bbox, work_dir, reference_dir, log)
        built_refs = _with_online_templates(built_refs, bbox, reference_dir, log, download=want_refs)
        built_refs = _drop_online_osm(built_refs)
        built_refs = withhold_images(built_refs, log=log)
        if progress is not None:
            progress.done()
        return built_refs, sorted(p.name for p in built_refs.values())
    if not want_refs and ref_download is None:
        if progress is not None:
            progress.skip("referenční podklady", reason="online šablony")
        else:
            log("Referenční podklady: online šablony")
        built_refs = _with_osm_render(built_refs, bbox, work_dir, reference_dir, log)
        built_refs = _drop_online_osm(_with_online_templates(built_refs, bbox, reference_dir, log))
        return built_refs, sorted(p.name for p in built_refs.values())
    if not want_refs:
        log("Referenční podklady: online šablony (GDAL WMS), stažený jen vektorový katastr")
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
                log("Referenční podklady: přeskočeno (chybí georef šablona)")
            return built_refs, []
        if progress is not None:
            progress.begin("referenční podklady")
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
                sprint_ortho=sprint_ortho,
                zip_ortho=zip_ortho,
            )
        except Exception as exc:
            log_issue(log, f"Referenční podklady: přeskočeno ({exc})")
            built_refs = {}
        if sentinel:
            add_sentinel2_reference(built_refs, tuple(bbox), reference_dir, log=log)
    else:
        reference_dir = ref_download.reference_dir
        if progress is not None:
            progress.begin("referenční podklady")
        if not ref_download.future.done():
            log("Čekám na dokončení stahování na pozadí…")
        try:
            built_refs = ref_download.result() or {}
            if built_refs:
                names = ", ".join(sorted(built_refs.keys()))
                log(f"Staženo na pozadí: {names}")
            else:
                log("Staženo na pozadí: nic")
        except Exception as exc:
            log_issue(log, f"Referenční podklady: stahování na pozadí selhalo ({exc})")
            built_refs = {}

    if not want_refs:
        built_refs = _drop_online_osm(_with_online_templates(built_refs, bbox, reference_dir, log))
    else:
        built_refs = _with_vegetation_height(built_refs, work_dir, reference_dir, log)
        built_refs = _with_online_templates(built_refs, bbox, reference_dir, log, only_online=True)
    built_refs = withhold_images(built_refs, log=log)
    ref_layers: list[str] = []
    if built_refs:
        ref_layers = sorted(p.name for p in built_refs.values())
    elif reference_dir.is_dir():
        ref_layers = sorted(p.name for p in list_reference_rasters(reference_dir))
    if progress is not None:
        progress.done()
    return built_refs, ref_layers


def run_job_pipeline(
    job_dir: Path,
    preset_id: str,
    options: dict,
    log: callable,
    job_name: str = "",
) -> list[str]:
    """Celý běh jobu. Vrací neopravené chyby (prázdný seznam = vše v pořádku)."""
    clear_job_memo()
    build_stats.reset()
    work_dir = job_dir / "work"
    output_dir = job_dir / "output"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Hlavičky kroků a vlákna na pozadí píšou od kraje (raw_log), detaily
    # hlavního vlákna jsou odsazené pod hlavičkou aktuálního kroku. Sběr chyb
    # (``⚠``) sedí pod oběma, takže vidí i řádky z pozadí.
    issues = IssueCollector(log)
    raw_log = issues
    log = indented_log(raw_log)
    reset_confirmed_sources()
    pending_source_issues: list[tuple[str | None, str, str]] = []

    bbox = options.get("bbox_wgs84")
    crop = None
    if bbox:
        west, south, east, north = bbox
        crop = crop_bounds_5514(west, south, east, north)
        bx0, by0, bx1, by1 = crop_bounds_5514(west, south, east, north, buffer_m=0.0)
        build_stats.set_info(area_km2=round((bx1 - bx0) * (by1 - by0) / 1e6, 2))
        # Před prvním stahováním: dost místa na disku (jinak úklid cache, jinak jasná chyba).
        ensure_job_space((bx1 - bx0) * (by1 - by0) / 1e6, log=log)

    dmr_files: list[Path | Future] = []
    dmp_files: list[Path | Future] = []
    sheet_names: list[str] = []
    lidar_download: LidarDownload | None = None
    zabaged_src: Path | None = None
    osm_objects = resolve_osm_objects(options)
    # Základní sada (lavičky, rozcestníky, pomníky, památné stromy…) – ve sprintu výchozí.
    osm_basic = resolve_osm_basic_objects(options, preset_id)
    force_refresh = force_refresh_enabled(options)
    # Země jobu (zahraniční build): mimo Česko bez českých zdrojů, výšky ze zdroje země.
    cz_sources = uses_cz_sources(options)
    elevation = elevation_source(options)
    tech_map = technical_map_source(options)
    set_process_country(options)
    raster_elevation = bool(bbox) and elevation != ELEVATION_CZ

    want_zip, _ = resolve_want_zip(options)
    want_refs = bool(options.get("output_references", True))
    reused_from = options.get("reused_from")
    progress = JobProgress(
        raw_log,
        plan_pipeline_steps(
            bbox=bbox,
            reused_from=reused_from,
            want_zip=want_zip,
            want_refs=True,  # krok podkladů běží vždy (vektorový katastr, ortofoto sprintu)
            cz_sources=cz_sources,
            raster_elevation=raster_elevation,
            technical_map=tech_map,
        ),
        omitted=omitted_steps(cz_sources=cz_sources, raster_elevation=raster_elevation, technical_map=tech_map),
    )
    progress.announce_plan()
    build_stats.attach_progress(progress)
    build_stats.set_info(app_version=app_settings.APP_VERSION, preset_id=preset_id, force_refresh=force_refresh)
    if force_refresh:
        raw_log("Vynucené obnovení: cache (LiDAR, ZABAGED, podklady) se nepoužije.")
    if not cz_sources:
        raw_log(country_sources_line(options))
        from app import data_notes

        for line in data_notes.text_lines(job_country(options), "cs", sprint=job_has_sprint(preset_id, options)):
            raw_log(f"Data výřezu: {line}")
    # Bez DTM se les a MTBO kreslí plnohodnotně z OSM a ZABAGED: chybějící DTM je chyba jobu jen u sprintu z DTM.
    try:
        dtm_needed = bool(
            cz_sources
            and options.get("dtm_fences", True) is not False
            and resolve_use_dtm(options, "sprint")
            and job_has_sprint(preset_id, options)
        )
    except Exception:
        dtm_needed = True
    try:  # stav zdrojů z poslední kontroly webu (app.source_status); job běží dál beze změny
        from app import source_status

        use_dtm_any = tech_map in TECHNICAL_MAP_STEPS and (
            resolve_use_dtm(options, "sprint") or resolve_use_dtm(options, "forest")
        )
        # Zdroje země výřezu (mimo Česko místní servery, OSM a Sentinel; české se nepoužijí).
        services = source_status.down_services(use_dtm=use_dtm_any, country=job_country_code(options))
        for svc, (is_issue, text) in zip(services, down_source_lines(services, dtm_needed=dtm_needed)):
            role = source_status.probe_role(svc.get("key"))
            if is_issue and role:
                # Až konec jobu rozhodne, zda je to chyba: OSM / výšky se často stáhnou přes zrcadlo nebo opakovaný pokus.
                pending_source_issues.append((role, svc["label"], text))
                log(pending_source_issue_note(text))
            elif is_issue:
                log_issue(log, text)
            else:
                raw_log(text)
    except Exception:
        pass
    # Sonda zdrojů výřezu z webu (app.area_probe při založení jobu): kde výšky nebo technická mapa výřez nepokrývají.
    from app.area_probe import log_lines as probe_log_lines

    for text in probe_log_lines(options.get("area_probe")):
        raw_log(text)

    # OSM (Overpass) trvá desítky sekund a při výpadku zrcadel minuty – stahuje
    # se na pozadí hned, souběžně s LiDAR/ZABAGED, a krok OSM jen převezme výsledek.
    osm_prefetch = None
    dtm_prefetch = None
    vector_prefetch: VectorPrefetch | None = None
    if bbox:
        try:
            osm_prefetch = start_osm_prefetch(
                tuple(bbox),
                include_benches=osm_objects or osm_basic,
                include_lamps=osm_objects,
                include_playground_equipment=osm_objects or osm_basic,
                osm_priority=osm_objects or osm_basic,
                force_refresh=force_refresh,
                log=background_log(raw_log, OSM_LOG_LABEL),
            )
        except Exception as exc:
            log_issue(log, f"OSM: stahování na pozadí se nespustilo ({exc})")
        # Zbytek zpevněné (501, jen sprint): vlastní velký OSM dump, stahuje se
        # sériově po hlavním OSM dotazu a build ho pak vezme z cache.
        try:
            if options.get("sprint_residual_paved") and job_has_sprint(
                preset_id, options
            ):
                start_residual_osm_prefetch(
                    work_dir,
                    tuple(bbox),
                    after=osm_prefetch.future if osm_prefetch else None,
                    log=background_log(raw_log, OSM_LOG_LABEL),
                )
        except Exception as exc:
            log_issue(log, f"OSM 501: stahování na pozadí se nespustilo ({exc})")
        # DTM kraje (zaměřené ploty, budovy, chodníky, silnice …; sprint, les i MTBO): příprava na pozadí,
        # výsledek se vezme při sestavení mapy (každý preset si z něj bere, co dává smysl).
        try:
            # Technická mapa země: v Česku DTM, jinde obdoba z ``technical_maps`` (BGT, AV); bez ní jen OSM.
            dtm_maps = {
                tag: bool(
                    tech_map in TECHNICAL_MAP_STEPS
                    and resolve_use_dtm(options, disc_preset)
                    and options.get("dtm_fences", True)
                )
                for tag, disc_preset, _scale in resolve_discipline_presets(
                    preset_id,
                    load_presets(),
                    map_scale=options.get("map_scale"),
                    contour_interval=options.get("contour_interval"),
                    map_types=resolve_map_types(options),
                )
            }
            dtm_used = any(dtm_maps.values())
            dtm_on = bool(options.get("dtm_fences", True) and dtm_used)
            if tech_map == "cz_dtm":  # souhrn DTM (obce, kraje, úplnost ploch) je jen pro Česko
                dtm_summary_data = dtm_summary.initial_summary(
                    dtm_maps,
                    dtm_on,
                    # „automaticky“ = les/MTBO bez výslovné volby (GUI auto nic neposílá) a DTM nevypnuté
                    auto=options.get("dtm_forest") is None and options.get("dtm_fences", True) is not False,
                )
                dtm_summary.write_summary(work_dir, dtm_summary_data)
                options = {**options, "_dtm_summary": dtm_summary_data}
            build_stats.set_info(dtm=dtm_on)
            # DTM běží souběžně na pozadí; krok „DTM kraje“ v plánu je až čekání na něj před sestavením map.
            raw_log(technical_map_line(tech_map, dtm_on))
            if dtm_on:
                dtm_prefetch = start_dtm_prefetch(
                    work_dir,
                    tuple(bbox),
                    log=background_log(raw_log, getattr(foreign_technical_map(tech_map), "short", DTM_LOG_LABEL)),
                    force=force_refresh,
                    source=tech_map,
                )
        except Exception as exc:
            if dtm_needed:
                log_issue(log, f"DTM: příprava na pozadí se nespustila ({exc})")
            else:
                raw_log(f"DTM: příprava na pozadí se nespustila ({exc}); mapa se kreslí z OSM a ZABAGED.")
        # ZABAGED (souběžně s LiDARem), RÚIAN, AOPK a u sprintu katastr + ortofoto na pozadí.
        try:
            vector_prefetch = None if not cz_sources else start_vector_prefetch(
                work_dir,
                tuple(bbox),
                force_refresh=force_refresh,
                with_streets=job_has_sprint(preset_id, options),
                log=raw_log,
            )
        except Exception as exc:
            log_issue(log, f"Vektory: stahování na pozadí se nespustilo ({exc})")
        # Sprint mimo Česko: letní NDVI a ortofoto země pro zpevněnou 401 (``open_paved``) na pozadí.
        if not cz_sources and job_has_sprint(preset_id, options):
            start_open_paved_prefetch(work_dir, tuple(bbox), log=raw_log)

    lidar_work = work_dir / "lidar"
    if reused_from:
        progress.begin("kopie LAZ z předchozího jobu")
        log("Připravuji data LiDAR…")
        copied = db.copy_reusable_work(str(reused_from), job_dir.name)
        if copied:
            log(f"Zkopírováno {len(copied)} souborů (sloučený LAZ).")
        else:
            log("Varování: data LiDAR se nepodařilo použít.")
        progress.done()

    merged_existing = None
    if reused_from:
        pair = lidar_pair_paths(lidar_work)
        if pair is not None:
            merged_existing = pair[0]

    if bbox and raster_elevation:
        # Mimo Česko: výšky jako rastry terénu a povrchu (``elevation_raster``) až na mřížce mapy, bez LAZ.
        progress.skip("stažení LiDAR", reason=f"výšky ze zdroje země ({elevation})")
        progress.skip("stažení ZABAGED", reason="mimo Česko")
    elif bbox:
        west, south, east, north = bbox
        progress.begin("stažení LiDAR")
        # Listy se stahují na pozadí; ořez listu (příprava LiDAR) začne hned po stažení jeho souboru.
        lidar_download = start_lidar_download(
            (west, south, east, north),
            background_log(raw_log, LIDAR_LOG_LABEL),
            force_refresh=force_refresh,
        )
        sheet_names = lidar_download.names
        build_stats.set_info(lidar_sheets=len(sheet_names))
        dmr_files, dmp_files = lidar_download.dmr, lidar_download.dmp
        progress.done()

        progress.begin("stažení ZABAGED")
        if vector_prefetch is not None:
            zabaged_src = vector_prefetch.zabaged(log)
        else:
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
    if bbox_tuple is not None and not raster_elevation:
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
        progress.begin("mřížka mapy")
        write_job_grid(
            work_dir,
            grid_bounds,
            resolution_m=DEFAULT_RESOLUTION_M,
            log=log,
        )
        progress.done()
        # Referenční PNG na pozadí (sériově ČÚZK) × overlap s těžkým buildem;
        # bez podkladů se na pozadí stahuje aspoň stínování. Mimo Česko nic (stínování z vlastního DEM).
        if bbox and cz_sources:
            ref_download = start_reference_download(
                job_dir,
                work_dir,
                tuple(bbox),
                force_refresh=force_refresh,
                log=raw_log,
                shade_bounds=grid_bounds,
                shade_cache_dir=surfaces_cache,
                with_references=want_refs,
                ortho_prefetch_bounds=ortho_prefetch_bounds(grid_bounds, preset_id, options),
                sprint_ortho=job_has_sprint(preset_id, options),
                sentinel=job_wants_sentinel(preset_id, options),
                zip_ortho=want_refs,
            )

    if reused_from and merged_existing:
        merged = merged_existing
        progress.reuse("příprava LiDAR")
    elif raster_elevation:
        merged = lidar_work / "ground_merged.laz"  # neexistuje; vrstevnice berou dem_filled z rastru
        progress.skip("příprava LiDAR", reason="výšky z rastru")
    else:
        cached_merged = None
        progress.begin("příprava LiDAR")
        if crop_cache is not None and not force_refresh:
            cached_merged = try_restore_lidar_crop(
                crop_cache,
                lidar_work,
                force=force_refresh,
                log=log,
            )
        if cached_merged is not None:
            merged = cached_merged
            progress.done()
        else:
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
    if bbox and not raster_elevation:
        # Strop cache LiDARu (``LIDAR_CACHE_MAX_GB``): smaže nejdéle nepoužité listy, ne listy tohoto jobu.
        try:
            prune_lidar_cache(
                keep_sheets=set(sheet_names or options.get("sm5_sheets") or ()),
                keep_dirs=tuple(p for p in (crop_cache,) if p is not None),
                log=log,
            )
        except Exception as exc:  # úklid cache nesmí shodit job
            log(f"Úklid cache LiDARu selhal: {exc}")

    surfaces: SurfacesPrep | None = None
    no_surface = False  # zdroj výšek bez modelu povrchu: vegetace z OSM (``generate_vegetation_from_osm``)
    if grid_bounds is not None and raster_elevation:
        progress.begin("výškové modely")
        try:
            from app.pipeline.elevation_raster import prepare_raster_surfaces

            prepare_raster_surfaces(
                work_dir,
                grid_bounds,
                source=elevation,
                resolution_m=DEFAULT_RESOLUTION_M,
                force_refresh=force_refresh,
                log=log,
            )
        except Exception as exc:
            # Bez terénu nejsou vrstevnice ani srázy: job skončí srozumitelnou chybou, ne pádem PDAL ve vrstevnicích.
            raise RuntimeError(f"Výšková data ({elevation}) nejsou k dispozici: {exc}") from exc
        from app.pipeline.elevation_raster import surface_missing

        no_surface = surface_missing(work_dir)
        if not no_surface:  # terén i povrch stažené: varování sondy výšek ze startu není chyba jobu
            confirm_source(SOURCE_ELEVATION)
            confirm_source(SOURCE_SURFACE)
        try:  # rastry jsou už v work/dem; cache dlaždic (Berlín) pod stropem ``LIDAR_CACHE_MAX_GB``
            prune_lidar_cache(log=log)
        except Exception as exc:  # úklid cache nesmí shodit job
            log(f"Úklid cache výšek selhal: {exc}")
        progress.done()
    elif grid_bounds is not None:
        progress.begin("výškové modely")
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
                surface_log=background_log(raw_log, SURFACE_LOG_LABEL),
            )
            if not surfaces.done():
                log("DSM/CHM (výška porostu) se počítá na pozadí.")
        except Exception as exc:
            # Nehard-fail: vegetace/srázy mají vlastní fallbacky.
            log_issue(log, f"Výškové modely: přeskočeno ({exc})")
        progress.done()

    def _wait_surfaces() -> None:
        """Dokončí DSM/CHM větev; chyba = stejný log jako dřív, job jede dál."""
        nonlocal surfaces
        if surfaces is None:
            return
        pending, surfaces = surfaces, None
        if not pending.done():
            log("Čekám na DSM/CHM z pozadí…")
        try:
            pending.wait()
        except Exception as exc:
            log_issue(log, f"DSM/CHM: přeskočeno ({exc})")

    if lidar_download is not None:
        lidar_download.wait()  # i při ořezu z cache: metadata listů (DMP OK / 1G) až po stažení všech
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
            # Trvalé omezení dat listu (ne selhání) – stav jobu neovlivní.
            log(
                "VAROVÁNÍ: DMP 1G místo DMP OK u alespoň jednoho listu "
                f"(režim={lidar_sources.get('dmp_mode')}) – vegetace je méně přesná."
            )
        if lidar_sources.get("dmp_leafless"):
            flown = ", ".join(
                f"{x.get('mapnom')} {x.get('acquired_on')}"
                for x in lidar_sources.get("dmp_leafless_sheets") or []
            )
            log(
                f"VAROVÁNÍ: DMP OK nalétaný v bezlistém období ({flown}) – "
                "v listnatém lese chybí výška stromů, 403 a porosty tam mohou být chybné."
            )

    zabaged_clean = work_dir / "zabaged_clean.zip"
    has_zabaged = zabaged_src is not None and zabaged_src.is_file()
    if has_zabaged:
        progress.begin("příprava ZABAGED")
        clean_zabaged(zabaged_src, zabaged_clean, log=log)
        progress.done()
    elif bbox:
        progress.skip("příprava ZABAGED", reason="chybí zdroj" if cz_sources else "mimo Česko")

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
        formlines=resolve_formline_mode(options),
        contour_smooth=resolve_contour_smooth(options),
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
    progress.begin("vegetace")
    if veg_size_profile != "default":
        log(f"Filtr velikosti/tvaru: {veg_size_profile}")
    vege_shp = None
    veg_buildings: Path | None = None
    # Zeleň ze zrnitosti porostu jen mimo Česko a jen bez sprintu (``veg_texture``); Česko beze změny.
    veg_texture = bool(bbox) and not cz_sources and not job_has_sprint(preset_id, options)
    # Zimní výšky bez listí (``green: leafoff``): zrnitost až po OSM jen ve stálezeleném porostu; nízký porost v lese bílý.
    veg_country = job_country(options)
    # Vlastní data zadavatele nejsou národní zdroj země: zeleň jako u běžného laseru (bez leafoff / understory).
    veg_green = "laser" if options.get("user_sources") else (veg_country.green if veg_country is not None else None)
    veg_leafoff = bool(bbox) and not cz_sources and veg_green == "leafoff"
    leafoff_texture = bool(veg_texture) and veg_leafoff
    if veg_leafoff or veg_green == "plain":
        veg_texture = False  # plain: sever, zrnitost zelenala řídký les (Nuuksio 406 32 %, reálná mapa převážně bílá)
    elif veg_texture and veg_green == "understory":
        # horský les (Trentino), Nizozemsko (přísnější práh ``green_share``): podíl nízkého patra místo zrnitosti
        share = veg_country.green_share if veg_country is not None else 0.0
        veg_texture = f"understory:{share:g}" if share else "understory"
    if bbox and cz_sources:
        # RÚIAN budovy (cache; stejný dotaz později v „RÚIAN a AOPK“) – maska zástavby.
        try:
            veg_buildings = (
                vector_prefetch.ruian(log) if vector_prefetch is not None else None
            ) or fetch_ruian_buildings_for_bbox(tuple(bbox), log=log)
        except Exception as exc:
            log(f"RÚIAN budovy pro vegetaci: přeskočeno ({exc})")
    try:
        vege_shp = generate_job_vegetation_density(
            work_dir,
            params=vegetation_density_params(preset_id, options),
            log=log,
            veg_size_profile=veg_size_profile,
            wait_chm=_wait_surfaces,
            buildings_geojson=veg_buildings,
            texture=veg_texture,
        )
    except Exception as exc:
        log(f"Vegetace (hustota bodů): přeskočeno ({exc})")
    # CHM fallback i další kroky (hillshade → AOI cache) potřebují hotové DSM/CHM.
    _wait_surfaces()
    if vege_shp is None:
        try:
            vege_shp = generate_job_vegetation_chm(
                work_dir, log=log, veg_size_profile=veg_size_profile, texture=veg_texture
            )
        except Exception as exc:
            log(f"CHM vegetace: přeskočeno ({exc})")
        if vege_shp is None and no_surface:
            log("Vegetace: bez modelu povrchu – vznikne z přírodního krytu OSM po přípravě OSM.")
        elif vege_shp is None:
            log_issue(log, "Vegetace: nevznikla (hustota bodů ani CHM)")
    progress.done()

    progress.begin("srázy")
    try:
        generate_job_cliffs_dem(work_dir, options=options, sprint=job_has_sprint(preset_id, options), log=log)
    except Exception as exc:
        log_issue(log, f"Srázy: přeskočeno ({exc})")
    progress.done()

    progress.begin("kupky a ďolíky")
    try:
        generate_job_knolls(work_dir, options=options, log=log)
    except Exception as exc:
        log_issue(log, f"Kupky a ďolíky: přeskočeno ({exc})")
    try:
        generate_job_boulders(work_dir, options=options, log=log)
    except Exception as exc:
        log_issue(log, f"Balvany: přeskočeno ({exc})")
    progress.done()

    if cz_sources and (not has_zabaged or not zabaged_clean.is_file()):
        raise RuntimeError(
            "ZABAGED (polohopis) není k dispozici – bez něj nelze dokončit mapu."
        )

    if bbox:
        progress.begin("OSM pěšiny a objekty")
        try:
            prepare_osm_paths(
                work_dir,
                tuple(bbox),
                zabaged_clean if has_zabaged else None,
                include_benches=osm_objects or osm_basic,
                include_lamps=osm_objects,
                include_playground_equipment=osm_objects or osm_basic,
                osm_priority=osm_objects or osm_basic,
                objects_basic_only=osm_basic and not osm_objects,
                all_footways_as_sidewalk=bool(
                    options.get("kp_osm_footway_as_sidewalk")
                ),
                preset_id=preset_id,
                force_refresh=force_refresh,
                log=log,
                prefetched=osm_prefetch,
            )
        except Exception as exc:
            log_issue(log, f"OSM: přeskočeno ({exc})")
        # Terénní prvky z národní topografické databáze (Finsko Maastotietokanta) před SHP vrstvami.
        if veg_country is not None and veg_country.terrain_vectors == "fi_mtk":
            try:
                from app.pipeline.mtk_fi import prepare as prepare_mtk

                prepare_mtk(work_dir, tuple(bbox), log=log)
            except Exception as exc:
                log_issue(log, f"Maastotietokanta: přeskočeno ({exc})")
        # Přílivová zóna u moře z DTM (Česko moře nemá).
        if not cz_sources:
            try:
                from app.pipeline.coast_tidal import apply_tidal_zone

                apply_tidal_zone(work_dir, log=log)
            except Exception as exc:
                log(f"Přílivová zóna: přeskočeno ({exc})")
        try:
            write_osm_manual_shapefiles(work_dir, log=log)
        except Exception as exc:
            log_issue(log, f"OSM: přeskočeno ({exc})")
        if no_surface:
            try:
                generate_vegetation_from_osm(work_dir, log=log)
            except Exception as exc:
                log_issue(log, f"Vegetace z OSM: přeskočeno ({exc})")
        elif veg_leafoff:
            try:
                # Zrnitost jen ve stálezeleném porostu ze zimního snímku (les a MTBO mimo Česko, ne sprint).
                regenerate_leafoff_vegetation(work_dir, log=log, texture=leafoff_texture)
            except Exception as exc:
                log(f"Vegetace: les z OSM pro zimní výšky přeskočen ({exc})")
        progress.done()

    # Shade vždy (OOM/ZIP + ČÚZK reference). Náhled PNG z .omap po sestavení.
    progress.begin("stínování reliéfu")
    shade_force = force_refresh
    if ref_download is not None and ref_download.shade_done is not None:
        if not ref_download.shade_done.is_set():
            log("Čekám na stínování z pozadí…")
        ref_download.shade_done.wait()
        if ref_download.shade_ok:
            shade_force = False  # už stažený v tomto běhu, nestahovat znovu
            log("Stínování převzato ze stahování na pozadí.")
    try:
        build_job_shade(
            work_dir,
            bounds_5514=grid_bounds,
            prefer_local=not cz_sources,  # mimo Česko jen z vlastního DEM (WMS ČÚZK tam nic nemá)
            force=shade_force,
            cache_dir=surfaces_cache,
            log=log,
        )
        qa_contours_vs_shared_dem(work_dir, log=log)
    except Exception as exc:
        log_issue(log, f"Stínování reliéfu: přeskočeno ({exc})")
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
            log_issue(log, f"Náhled compose: přeskočeno ({exc})")
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
        dtm_prefetch=dtm_prefetch,
        vector_prefetch=vector_prefetch,
    )
    clear_job_memo()  # plochy DTM drží jen sestavení map tohoto jobu
    progress.finish_all()
    resolve_source_issues(pending_source_issues, log, raw_log)
    issues.report()
    if not cz_sources and not options.get("user_sources"):
        # Petr 2026-11-22: stáří a druh dat zopakovat i na konci (web ho ukáže u hotového jobu z ``data_notes.job_lines``).
        from app import data_notes

        for line in data_notes.text_lines(job_country(options), "cs", sprint=job_has_sprint(preset_id, options)):
            raw_log(f"Upozornění k datům: {line}")
    raw_log("")
    if issues.items:
        raw_log(f"Hotovo za {progress.elapsed()}, s chybami ({len(issues.items)}).")
    else:
        raw_log(f"Hotovo za {progress.elapsed()}.")
    return list(issues.items)


def _fix_ocd_templates(omap_paths: list[Path], reference_dirs: list[Path | None], *, log) -> None:
    """Best-effort oprava šablon v sourozencích ``.ocd`` (``ocd_templates``); chyba nesmí shodit job."""
    from app.pipeline.ocd_templates import fix_ocd_templates

    dirs = [d for d in reference_dirs if d is not None and Path(d).is_dir()]
    for omap_p in omap_paths:
        ocd_p = Path(omap_p).with_suffix(".ocd")
        if not ocd_p.is_file():
            continue
        try:
            fix_ocd_templates(ocd_p, omap_p, reference_dirs=dirs, log=log)
        except Exception as exc:
            log_issue(log, f"OOM OCD: šablony neopraveny ({ocd_p.name}): {exc}")


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
    dtm_prefetch=None,
    vector_prefetch: VectorPrefetch | None = None,
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
        refs_joined: dict[str, tuple[dict[str, Path], list[str]]] = {}

        def _join_refs(join_progress: JobProgress | None = None) -> dict[str, Path]:
            """Join podkladů jen jednou (hned, nebo až před zápisem první mapy)."""
            if "refs" not in refs_joined:
                refs_joined["refs"] = join_reference_download(
                    ref_download,
                    log=log,
                    want_refs=want_refs,
                    bbox=bbox,
                    work_dir=kp_cwd,
                    job_dir=job_dir,
                    force_refresh=force_refresh_enabled(options),
                    progress=join_progress,
                    sprint_ortho=job_has_sprint(preset_id, options),
                    sentinel=job_wants_sentinel(preset_id, options),
                    zip_ortho=want_refs,
                )
            return refs_joined["refs"][0]

        def _metadata() -> dict:
            ref_layers = refs_joined["refs"][1] if "refs" in refs_joined else []
            meta = oom_metadata(
                preset_id,
                preset,
                options,
                job_name,
                reference_layers=ref_layers or None,
                lidar_sources=options.get("_lidar_sources"),
            )
            meta["output_georef"] = output_georef_enabled(options)
            if want_refs and withheld_sources(meta.get("country") or "CZ"):
                # obrázky zdrojů, které licence do ZIPu nepouští (vysvětlení v references/, zip_images)
                meta["withheld_images"] = withheld_sources(meta.get("country") or "CZ")
            if options.get("_dtm_summary"):
                meta["dtm"] = options["_dtm_summary"]
            return meta

        # Les a MTBO potřebují z podkladů jen seznam šablon .omap → sestavení na stahování (ortofoto ~10 min)
        # nečeká a join je až před zápisem mapy. Sprint čte dlaždice ortofota (maska zeleně ulic), čeká hned.
        defer_refs = bool(bbox) and ref_download is not None and not job_has_sprint(preset_id, options)
        if defer_refs:
            if progress is not None:
                progress.begin("referenční podklady")
            log("Stahování podkladů běží dál na pozadí; šablony se doplní před zápisem mapy.")
            if progress is not None:
                progress.done()
            built_refs = lambda: _join_refs() or None  # noqa: E731  # vyhodnotí až _prepare_oom_map
        else:
            built_refs = _join_refs(progress)
        if ref_download is not None:
            reference_dir = ref_download.reference_dir

        meta = _metadata()
        omap_paths: list[Path] = []
        certain_paths: list[Path] = []
        ruian_path: Path | None = None
        aopk_path: Path | None = None
        if bbox and not uses_cz_sources(options) and progress is not None:
            progress.skip("RÚIAN a AOPK", reason="mimo Česko")
        if bbox:
            if progress is not None and uses_cz_sources(options):
                progress.begin("RÚIAN a AOPK")
            try:
                ruian_path = (
                    (vector_prefetch.ruian(log) if vector_prefetch is not None else None)
                    or fetch_ruian_buildings_for_bbox(tuple(bbox), log=log)
                    if uses_cz_sources(options)
                    else None
                )
            except Exception as exc:
                log_issue(log, f"RÚIAN budovy: přeskočeno ({exc})")
                ruian_path = None
            try:
                aopk_path = (
                    (vector_prefetch.aopk(log) if vector_prefetch is not None else None)
                    or fetch_aopk_trees_for_bbox(tuple(bbox), log=log)
                    if uses_cz_sources(options)
                    else None
                )
            except Exception as exc:
                log_issue(log, f"AOPK stromy: přeskočeno ({exc})")
                aopk_path = None
            if progress is not None and uses_cz_sources(options):
                progress.done()

            indexcontours_m = options.get("indexcontours", preset.get("indexcontours"))
            if indexcontours_m is None and meta.get("contour_interval_m") is not None:
                indexcontours_m = 5 * float(meta["contour_interval_m"])
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
            log_ostatni = log if uses_cz_sources(options) else (lambda _msg: None)  # ZABAGED jen v Česku
            log_ostatni(
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

            dtm_step = TECHNICAL_MAP_STEPS.get(technical_map_source(options), DTM_STEP)
            if progress is not None and dtm_prefetch is None:
                progress.skip(dtm_step, reason="kresba bez DTM" if dtm_step == DTM_STEP else "vypnuto")
            if dtm_prefetch is not None:
                if progress is not None:
                    progress.begin(dtm_step)
                t_dtm_wait = time.monotonic()
                if not dtm_prefetch.future.done():
                    log(
                        "Čekám na přípravu DTM (ploty) z pozadí…"
                        if dtm_step == DTM_STEP
                        else f"Čekám na {dtm_step} z pozadí…"
                    )
                dtm_out = dtm_prefetch.result()  # chyby řeší vlákno samo (job pokračuje bez DTM)
                if progress is not None and (dtm_out is None or dtm_prefetch.failed):
                    # Krok se nesmí tvářit jako hotový s daty: web ho ukáže jako nedostupný.
                    progress.done(
                        unavailable="kresba z OSM a ZABAGED" if uses_cz_sources(options) else "kresba z OSM"
                    )
                if options.get("_dtm_summary"):
                    dtm_final = dtm_summary.finalize_summary(
                        options["_dtm_summary"], kp_cwd, failed=dtm_prefetch.failed
                    )
                    options = {**options, "_dtm_summary": dtm_final}
                    meta["dtm"] = dtm_final
                    dtm_summary.write_summary(kp_cwd, dtm_final)
                    for line in dtm_summary.log_lines(dtm_final):
                        log(line)
                build_stats.note_background("dtm", wait_seconds=time.monotonic() - t_dtm_wait)
            if vector_prefetch is not None:
                vector_prefetch.streets(log)  # sprint: parcely ulic už v cache jobu
            log_step(
                log,
                "Sestavuji mapu OOM (vrstevnice, zeleň a polohopis do .omap)",
            )
            tasks: list[DisciplineTask] = []
            disc_list = resolve_discipline_presets(
                preset_id,
                presets,
                map_scale=options.get("map_scale"),
                contour_interval=options.get("contour_interval"),
                map_types=resolve_map_types(options),
            )
            if resolve_map_types(options) and len(disc_list) == 1:
                only = "MTBO" if disc_list[0][0] == "mtbo" else "les"
                log(f"OOM: typ mapy – jen {only} (vybráno v GUI)")
            for disc_tag, disc_preset_id, scale in disc_list:
                disc_preset = presets.get(disc_preset_id, {})
                vectorconf = Path(
                    str(disc_preset.get("vectorconf", "zabaged.txt"))
                ).name
                path_src = oom_path_source(options, disc_preset_id)
                calls: list[MapCall] = []
                for (path_tag, _), certain in (
                    (v, c)
                    for v in OOM_PATH_VARIANTS
                    for c in ((False, True) if BUILD_CERTAIN_MAPS else (False,))
                ):
                    variant_name = omap_variant_filename(
                        disc_tag, path_tag, map_name=job_name or "",
                        certain_only=certain, lang=zip_lang(options),
                    )
                    # Les a MTBO: hlavní jméno nese čistá verze, plná mapa má příponu ``_full`` (clean_map).
                    clean_dest = (
                        output_dir / variant_name
                        if CLEAN_MAPS and not certain and disc_tag != "sprint"
                        else None
                    )
                    calls.append(
                        MapCall(
                            variant_name=variant_name,
                            certain=certain,
                            header=(
                                f"OOM: {variant_name} ({disc_preset_id}, 1:{scale}, {path_src}"
                                + (", jen čistá data)" if certain else ")")
                            ),
                            dest=output_dir / (
                                full_variant_name(variant_name) if clean_dest else variant_name
                            ),
                            kwargs=dict(
                                map_name=(job_name or disc_preset_id)
                                + (CERTAIN_MAP_NAME_SUFFIX if certain else ""),
                                scale=scale,
                                preset_id=disc_preset_id,
                                bbox_wgs84=tuple(bbox),
                                zabaged_clean=zabaged,
                                vectorconf_name=vectorconf,
                                include_dxf=include_dxf,
                                contour_interval_m=contour_interval_m,
                                formline=0,
                                indexcontours_m=indexcontours_m,
                                cliff_symbol=cliff_symbol,
                                contour_formlines=resolve_formline_mode(options),
                                path_source=path_src,
                                aopk_trees=aopk_path,
                                max_ostatni_m2=max_ostatni_m2,
                                ostatni_as_403=ostatni_as_403,
                                residual_paved=residual_paved,
                                veg_smooth=resolve_veg_smooth(options),
                                use_dtm=resolve_use_dtm(options, disc_preset_id),
                                max_residual_m2=max_residual_m2,
                                certain_only=certain,
                                clean_dest=clean_dest,
                            ),
                        )
                    )
                tasks.append(DisciplineTask(disc_tag, calls))
            # Souběh les ‖ MTBO jen mimo sprint (sprint čte sdílené cache jobu a Overpass prefetch v procesu jobu).
            results = build_discipline_maps(
                kp_cwd,
                tasks,
                built_refs=built_refs,
                join_refs=_join_refs if defer_refs else None,
                log=log,
                parallel=app_settings.PARALLEL_DISCIPLINE_MAPS and not job_has_sprint(preset_id, options),
                prepare=prepare_oom_map,
                progress=progress,
            )
            for task, paths in zip(tasks, results):
                for call, omap_p in zip(task.calls, paths):
                    if omap_p:
                        # Náhled jen z plné mapy; „čistá“ jde do ZIPu a OCD navíc.
                        (certain_paths if call.certain else omap_paths).append(omap_p)
                        clean_p = call.kwargs.get("clean_dest")
                        if clean_p and Path(clean_p).is_file():
                            certain_paths.append(Path(clean_p))  # čistá verze: ZIP a OCD, náhled z plné

            if progress is not None:
                progress.begin(PREVIEW_STEP)
            if defer_refs:
                built_refs = _join_refs()  # i když žádná mapa nevznikla: podklady jdou do ZIPu
                meta = _metadata()
            # Mapper CLI convert zapsal šablony bez pohledu a bez načtených podkladů → opravit v .ocd.
            _fix_ocd_templates(
                [*omap_paths, *certain_paths],
                [reference_dir, kp_cwd / "references"],
                log=log,
            )
            t_prev = time.monotonic()
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
                log_issue(log, f"OOM náhled: přeskočeno ({exc})")
            log(f"OOM náhled: za {format_elapsed(time.monotonic() - t_prev)}")

            if output_georef_enabled(options):
                try:
                    build_georef_previews_zip(
                        output_dir, log=log, lang=zip_lang(options), cs_intl_url=cs_intl_site_url(options)
                    )
                except Exception as exc:
                    log_issue(log, f"OOM georef ZIP: přeskočeno ({exc})")

        if progress is not None:
            progress.begin(ZIP_STEP)
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
        t_zip = time.monotonic()
        if meta.get("withheld_images"):
            log(f"Podklady: {withheld_log_line(meta['withheld_images'], zip_lang(options))}")
        build_oom_zip(
            kp_cwd,
            zip_path,
            zabaged_clean=zabaged,
            metadata=meta,
            reference_dir=(
                reference_dir
                if (want_refs or built_refs) and reference_dir and reference_dir.is_dir()
                else None
            ),
            omap_paths=[*omap_paths, *certain_paths],
            include_zabaged_archive=bool(
                options.get("output_zabaged_clean", False) and zabaged
            ),
            ruian_buildings=ruian_path,
            include_dxf=bool(options.get("output_dxf", True)),
            include_cliffs=cliff_symbol != "off",
            georef_preview_dir=georef_dir,
        )
        log(f"ZIP: zabaleno za {format_elapsed(time.monotonic() - t_zip)}")
    else:
        if progress is not None:
            progress.begin("balení výstupu")

    for name in ("preview.png", "preview.pgw"):
        src = kp_cwd / name
        if src.exists():
            shutil.copy2(src, output_dir / name)
    # Rozbalené kopie podkladů vedle ZIPu (shade/, references/, uzitecne/) slouží jen k otevření .omap přímo
    # z output/ při vývoji; web stahuje ZIP. Ostrá A je vypíná (OUTPUT_UNPACKED_COPIES=0, Petr 2026-11-19).
    unpacked = bool(app_settings.OUTPUT_UNPACKED_COPIES)
    shade_src = kp_cwd / "shade"
    if unpacked and shade_src.is_dir():
        shade_dst = output_dir / "shade"
        for name in ("hillshade.png", "hillshade.pgw"):
            src = shade_src / name
            if src.is_file():
                shade_dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, shade_dst / name)
    # Stejná struktura jako v ZIPu, ať jde otevřít i output/*-{sprint,les,mtbo}.omap.
    if unpacked and want_zip and omap_paths:
        if bool(options.get("output_references", True)) or built_refs:
            refs_src = kp_cwd / "references"
            if refs_src.is_dir():
                from app.pipeline.zip_names import localize_name

                refs_dst = output_dir / "references"
                refs_dst.mkdir(parents=True, exist_ok=True)
                for path in refs_src.glob("*"):
                    if path.is_file() and path.suffix.lower() in {
                        ".png",
                        ".pgw",
                        ".jpg",
                        ".jgw",
                        ".gpkg",
                        ".dxf",
                        ".xml",
                        ".vrt",
                    } and not path.name.lower().endswith(".aux.xml"):
                        # anglický výstup: názvy jako v ZIPu a v šablonách .omap (zip_names)
                        shutil.copy2(path, refs_dst / localize_name(path.name, zip_lang(options)))
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
                log_issue(log, f"uzitecne/: přeskočeno ({exc})")

    if want_zip and zip_path.is_file():
        log(f"Výstup: {zip_path.name} ({zip_path.stat().st_size / 1e6:.2f} MB)")
    elif resolve_preview_png(output_dir) is not None:
        png = resolve_preview_png(output_dir)
        assert png is not None
        log(f"Výstup: jen PNG náhled ({png.stat().st_size / 1e6:.2f} MB)")
    else:
        log_issue(log, "Výstup: žádný ZIP ani PNG")
    if progress is not None:
        progress.finish_all()
