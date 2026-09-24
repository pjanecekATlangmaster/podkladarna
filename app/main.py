from __future__ import annotations

import logging
import mimetypes
import traceback
import urllib.parse
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db, worker
from app.cleanup import maybe_purge_old_jobs, purge_old_jobs, start_cleanup_scheduler
from app.client_ip import client_ip
from app.guide_text import WEB_ABOUT_HTML
from app.rate_limit import check_create_job
from app.pipeline.fetch_openzu import (
    FetchError,
    MAX_BBOX_AREA_KM2,
    MAX_BBOX_KM,
    MAX_SHEETS,
    REF_PNG_ESTIMATE_MINUTES,
    bbox_area_km2,
    bbox_exceeds_limit,
    bbox_size_km,
    estimate_note,
    estimate_minutes,
    parse_bbox,
    query_sm5_sheets,
)
from app.pipeline.ini_builder import (
    KP_CLIFF_SENSITIVITY,
    KP_VEGE_HEIGHT_CHOICES,
    load_presets,
    resolve_cliff_sensitivity,
    resolve_vege_height,
)
from app.pipeline.package_oom import (
    CONTOURS_BY_SCALE,
    DEFAULT_CONTOUR_BY_SCALE,
    MAP_SCALES,
    resolve_omap_job,
)
from app.settings import CLEANUP_INTERVAL_HOURS, DEFAULT_OPTIONS, DOWNLOADS_DIR, JOBS_DIR, MAX_QUEUE_SIZE, APP_VERSION
from app.tiles import TileError, fetch_tile
from app.tool_env import tool_status
from app.whats_new import whats_new_payload

logger = logging.getLogger("podkladarna")


def _upload_files(form, key: str) -> list[UploadFile]:
    """Vrátí nahrané soubory pro dané pole (robustní vůči TestClient i prohlížeči)."""
    files: list[UploadFile] = []
    for field_name, value in form.multi_items():
        if field_name != key:
            continue
        if not hasattr(value, "read"):
            continue
        if not (getattr(value, "filename", None) or "").strip():
            continue
        # filename může být None u některých klientů – řeší se při ukládání
        files.append(value)
    return files


def _form_str(form, key: str, default: str = "") -> str:
    val = form.get(key)
    if val is None:
        return default
    return str(val)

app = FastAPI(title="Podkladarna", version=APP_VERSION)

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
# Minimální image (conda) často nezná .svg → nginx nosniff logo nenačte.
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("image/png", ".png")


@app.on_event("startup")
def startup() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()
    interrupted = worker.recover_after_restart()
    if interrupted:
        logger.info("Recovered %s interrupted job(s): %s", len(interrupted), interrupted)
    removed = purge_old_jobs()
    if removed:
        logger.info("Startup cleanup: removed %s old job(s)", removed)
    start_cleanup_scheduler(CLEANUP_INTERVAL_HOURS)
    tools = tool_status()
    missing = [name for name, path in tools.items() if not path]
    logger.info("Nástroje: %s", tools)
    if missing:
        logger.warning(
            "Chybí %s – pipeline na tomto stroji nepoběží. "
            "Windows: OSGeo4W + pullauta.exe, nebo docker compose -f docker-compose.dev.yml up",
            ", ".join(missing),
        )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError):
    logger.warning("Validation error: %s", exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception):
    logger.exception("Unhandled error")
    return JSONResponse(status_code=500, content={"detail": str(exc)})


WEB_DIR = STATIC_DIR.parent


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    try:
        removed = maybe_purge_old_jobs()
        if removed:
            logger.info("Pageview cleanup: removed %s old job(s)", removed)
    except Exception:
        logger.exception("Pageview cleanup failed")
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("<!-- PODKLADARNA_ABOUT -->", WEB_ABOUT_HTML)
    # Cloudflare cachuje /static/* až 4 h – query podle verze vynutí nový JS/CSS po deployi.
    v = urllib.parse.quote(APP_VERSION, safe="")
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={v}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={v}"')
    return HTMLResponse(html)


@app.get("/licence", response_class=HTMLResponse)
def licence_page() -> HTMLResponse:
    html = (WEB_DIR / "licence.html").read_text(encoding="utf-8")
    html = html.replace("{{APP_VERSION}}", APP_VERSION)
    return HTMLResponse(html)


@app.get("/api/presets")
def api_presets():
    presets = load_presets()
    return {
        k: {"id": k, "label": v.get("label", k), **v}
        for k, v in presets.items()
    }


@app.get("/api/map_options")
def api_map_options():
    """Měřítka a povolené ekvidistance pro GUI (bez „typu mapy“)."""
    return {
        "scales": list(MAP_SCALES),
        "contours_by_scale": {
            str(scale): list(vals) for scale, vals in CONTOURS_BY_SCALE.items()
        },
        "default_contour_by_scale": {
            str(scale): val for scale, val in DEFAULT_CONTOUR_BY_SCALE.items()
        },
    }


@app.get("/api/whats_new")
def api_whats_new():
    """Stáří posledního buildu + větší změny (box nad formulářem)."""
    return whats_new_payload()


@app.get("/api/sheets")
def api_sheets(bbox: str):
    """bbox=west,south,east,north (WGS84) → listy SM5 + odhad času."""
    try:
        west, south, east, north = parse_bbox(bbox)
    except FetchError as exc:
        raise HTTPException(400, str(exc)) from exc
    width_km, height_km = bbox_size_km(west, south, east, north)
    area_km2 = bbox_area_km2(west, south, east, north)
    if bbox_exceeds_limit(west, south, east, north):
        return {
            "sheets": [],
            "count": 0,
            "max_sheets": MAX_SHEETS,
            "width_km": round(width_km, 2),
            "height_km": round(height_km, 2),
            "area_km2": round(area_km2, 1),
            "max_km": MAX_BBOX_KM,
            "max_area_km2": MAX_BBOX_AREA_KM2,
            "too_large": True,
            "too_large_reason": "size",
            "estimate_minutes": None,
            "estimate_minutes_with_refs": None,
            "label": f"Výřez {width_km:.1f} × {height_km:.1f} km ({area_km2:.0f} km²)",
            "hint": (
                f"Výřez {width_km:.1f} × {height_km:.1f} km ({area_km2:.0f} km²) je moc velký "
                f"(max {MAX_BBOX_AREA_KM2:.0f} km², např. {MAX_BBOX_KM:.0f}×{MAX_BBOX_KM:.0f} "
                f"nebo 8×4,5 km). Zmenšete ho."
            ),
        }
    try:
        sheets = query_sm5_sheets(west, south, east, north)
    except FetchError as exc:
        raise HTTPException(400, str(exc)) from exc
    names = [s["mapnom"] for s in sheets]
    sheets_too_big = len(sheets) > MAX_SHEETS
    hint = (
        f"{', '.join(names)} ({len(sheets)}). Zmenšete výřez (max {MAX_SHEETS} listů SM5)."
        if sheets_too_big
        else None
    )
    est = estimate_minutes(names) if sheets and not sheets_too_big else None
    est_refs = (
        estimate_minutes(names, include_references=True)
        if sheets and not sheets_too_big
        else None
    )
    return {
        "sheets": sheets,
        "count": len(sheets),
        "max_sheets": MAX_SHEETS,
        "width_km": round(width_km, 2),
        "height_km": round(height_km, 2),
        "area_km2": round(area_km2, 1),
        "max_km": MAX_BBOX_KM,
        "max_area_km2": MAX_BBOX_AREA_KM2,
        "ref_png_estimate_minutes": REF_PNG_ESTIMATE_MINUTES,
        "too_large": sheets_too_big,
        "too_large_reason": "sheets" if sheets_too_big else None,
        "estimate_minutes": est,
        "estimate_minutes_with_refs": est_refs,
        "estimate_note": estimate_note(names) if sheets and not sheets_too_big else None,
        "label": (
            f"Protíná listy: {', '.join(names)} ({len(sheets)})"
            if sheets
            else "Výřez neprotíná žádný list SM5"
        ),
        "hint": hint,
    }


@app.get("/api/jobs")
def api_list_jobs():
    jobs = db.list_jobs()
    for job in jobs:
        pos = worker.queue_position(job["id"])
        if pos is not None:
            job["queue_position"] = pos
    return {"jobs": jobs, **worker.queue_snapshot()}


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str):
    try:
        return db.get_job(job_id)
    except KeyError:
        raise HTTPException(404, "Job nenalezen")


@app.get("/api/jobs/{job_id}/log")
def api_job_log(job_id: str, after: int = 0):
    try:
        db.get_job(job_id)
    except KeyError:
        raise HTTPException(404, "Job nenalezen")
    return {"lines": db.get_logs(job_id, after)}


@app.get("/api/jobs/{job_id}/download/oom")
def api_download_oom(job_id: str):
    """Zpětná kompatibilita – stejný balíček jako /download."""
    return api_download(job_id)


def _output_zip_path(job_id: str) -> Path | None:
    out = JOBS_DIR / job_id / "output"
    for name in ("podkladarna_output.zip", "podkladarna_oom.zip"):
        path = out / name
        if path.is_file():
            return path
    return None


@app.get("/api/jobs/{job_id}/download")
def api_download(job_id: str):
    zip_path = _output_zip_path(job_id)
    if not zip_path:
        raise HTTPException(404, "Vystup jeste neni pripraven")
    try:
        job = db.get_job(job_id)
        filename = db.zip_download_filename(job_id, job["name"])
    except KeyError:
        filename = db.zip_download_filename(job_id, job_id)
    return FileResponse(zip_path, filename=filename)


@app.get("/api/jobs/{job_id}/preview.png")
def api_preview(job_id: str):
    png = JOBS_DIR / job_id / "output" / "pullautus.png"
    if not png.exists():
        raise HTTPException(404, "Nahled neni k dispozici")
    return FileResponse(png)


@app.get("/api/health")
def api_health():
    import shutil

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(JOBS_DIR)
    tools = tool_status()
    return {
        "ok": True,
        "version": APP_VERSION,
        "data_dir": str(JOBS_DIR.parent),
        "downloads_dir": str(DOWNLOADS_DIR),
        "disk_free_gb": round(usage.free / 1e9, 2),
        "busy": worker.is_busy(),
        "tools": tools,
        "pipeline_ready": all(tools.values()),
    }


@app.post("/api/jobs")
async def api_create_job(request: Request):
    logger.info("POST /api/jobs – cteni multipart form")
    try:
        form = await request.form()
    except Exception as exc:
        logger.exception("Multipart form parse failed")
        raise HTTPException(400, f"Nepodarilo se precist upload: {exc}") from exc

    name = _form_str(form, "name").strip()
    if not name:
        raise HTTPException(400, "Chybi nazev jobu")

    presets = load_presets()
    map_scale_raw = _form_str(form, "map_scale").strip()
    contour_raw = _form_str(form, "contour_interval").strip()
    preset_id = _form_str(form, "preset_id").strip()
    try:
        if map_scale_raw:
            resolved = resolve_omap_job(
                map_scale_raw,
                contour_raw or None,
                presets=presets,
            )
        elif preset_id:
            # Zpětná kompatibilita starých klientů / iterace z jobu s preset_id.
            resolved = resolve_omap_job(
                None,
                contour_raw or None,
                presets=presets,
                preset_id_fallback=preset_id,
            )
        else:
            raise HTTPException(400, "Chybí měřítko mapy.")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    preset_id = resolved["preset_id"]
    if preset_id not in presets:
        raise HTTPException(400, f"Neznamy preset: {preset_id}")

    if worker.is_busy() and not worker.can_accept_job():
        raise HTTPException(
            503,
            f"Fronta je plná (max {MAX_QUEUE_SIZE} čekajících jobů). "
            "Počkejte na dokončení běžících generování.",
        )

    bbox_raw = _form_str(form, "bbox").strip()
    if not bbox_raw:
        raise HTTPException(400, "Chybí výřez na mapě (bbox).")
    try:
        bbox = parse_bbox(bbox_raw)
    except FetchError as exc:
        raise HTTPException(400, str(exc)) from exc
    width_km, height_km = bbox_size_km(*bbox)
    if bbox_exceeds_limit(*bbox):
        area = bbox_area_km2(*bbox)
        raise HTTPException(
            400,
            f"Výřez je moc velký ({width_km:.1f} × {height_km:.1f} km, "
            f"{area:.0f} km²; max {MAX_BBOX_AREA_KM2:.0f} km²).",
        )
    # Listy SM5: preferuj výsledek z /api/sheets (UI už ověřilo) – ušetří další ArcGIS call.
    sheets: list[dict] = []
    sheets_raw = _form_str(form, "sm5_sheets").strip()
    if sheets_raw:
        seen_noms: set[str] = set()
        for part in sheets_raw.replace(";", ",").split(","):
            mapnom = part.strip().upper()
            if not mapnom or mapnom in seen_noms:
                continue
            if not all(c.isalnum() or c in "-_" for c in mapnom):
                continue
            seen_noms.add(mapnom)
            sheets.append({"mapnom": mapnom, "name": mapnom})
    if not sheets:
        try:
            sheets = query_sm5_sheets(*bbox)
        except FetchError as exc:
            raise HTTPException(400, str(exc)) from exc
    if not sheets:
        raise HTTPException(400, "Výřez neprotíná žádný list SM5.")
    if len(sheets) > MAX_SHEETS:
        names = ", ".join(s["mapnom"] for s in sheets)
        raise HTTPException(
            400,
            f"Výřez je moc velký ({len(sheets)} listů SM5, max {MAX_SHEETS}): {names}",
        )

    dmr_uploads = _upload_files(form, "dmr_files")
    dmp_uploads = _upload_files(form, "dmp_files")
    if dmr_uploads or dmp_uploads or _upload_files(form, "zabaged_file"):
        raise HTTPException(
            400,
            "Ruční upload podkladů není podporován – použijte výřez na mapě (ČÚZK open data).",
        )

    remote_ip = client_ip(request)
    limit_msg = check_create_job(remote_ip)
    if limit_msg:
        raise HTTPException(429, limit_msg)

    options = {
        **DEFAULT_OPTIONS,
        "source_mode": "map",
        "bbox_wgs84": list(bbox),
        "sm5_sheets": [s["mapnom"] for s in sheets],
        "client_ip": remote_ip,
        "map_scale": resolved["map_scale"],
        "scalefactor": resolved["scalefactor"],
        "contour_interval": resolved["contour_interval"],
        "indexcontours": resolved["indexcontours"],
    }
    cliff_raw = _form_str(form, "kp_cliff_symbol").strip().lower()
    if cliff_raw in {"earth_bank", "rock_face", "symbol_206", "off"}:
        options["kp_cliff_symbol"] = cliff_raw
    vege_raw = _form_str(form, "kp_vege_height").strip()
    if vege_raw:
        try:
            vh = float(vege_raw.replace(",", "."))
        except ValueError:
            vh = None
        if vh is not None and any(abs(vh - c) < 1e-6 for c in KP_VEGE_HEIGHT_CHOICES):
            options["kp_vege_height"] = resolve_vege_height({"kp_vege_height": vh})
    sens_raw = _form_str(form, "kp_cliff_sensitivity").strip().lower()
    if sens_raw in KP_CLIFF_SENSITIVITY:
        options["kp_cliff_sensitivity"] = resolve_cliff_sensitivity(
            {"kp_cliff_sensitivity": sens_raw}
        )
    def _opt_bool(key: str) -> bool:
        return _form_str(form, key).strip().lower() in {"1", "true", "yes", "on"}

    options["kp_osm_benches"] = _opt_bool("kp_osm_benches")
    options["kp_osm_lamps"] = _opt_bool("kp_osm_lamps")
    options["kp_osm_playground_equipment"] = _opt_bool(
        "kp_osm_playground_equipment"
    )
    options["kp_osm_priority"] = _opt_bool("kp_osm_priority")
    options["kp_osm_footway_as_sidewalk"] = _opt_bool(
        "kp_osm_footway_as_sidewalk"
    )
    options["sprint_courtyard_olive"] = _opt_bool("sprint_courtyard_olive")
    options["sprint_residual_paved"] = _opt_bool("sprint_residual_paved")
    residual_size_raw = _form_str(form, "sprint_residual_size").strip().lower()
    if residual_size_raw in {"small", "medium", "large"}:
        options["sprint_residual_size"] = residual_size_raw
    ostatni_raw = _form_str(form, "ostatni_plocha").strip().lower()
    if ostatni_raw in {"none", "small", "medium", "large"}:
        options["ostatni_plocha"] = ostatni_raw
    options["ostatni_plocha_as_403"] = _opt_bool("ostatni_plocha_as_403")
    # Zpětná kompatibilita starého checkboxu.
    if _opt_bool("kp_osm_furniture"):
        options["kp_osm_benches"] = True
        options["kp_osm_lamps"] = True
    mode_raw = _form_str(form, "output_mode").strip().lower()
    if mode_raw in {"png", "png_only"}:
        options["output_zip"] = False
    elif mode_raw in {"png_zip", "zip", "both", ""}:
        options["output_zip"] = True
    elif _form_str(form, "output_zip").strip():
        options["output_zip"] = _opt_bool("output_zip")
    options["output_references"] = _opt_bool("output_references")
    reuse_id = _form_str(form, "reuse_job_id").strip()
    if reuse_id:
        try:
            prev = db.get_job(reuse_id)
        except KeyError:
            prev = None
        prev_bbox = (prev or {}).get("options", {}).get("bbox_wgs84")
        if prev and prev.get("has_reusable_lidar") and db.bbox_close(prev_bbox or [], bbox):
            options["reused_from"] = reuse_id
    dup = db.find_duplicate_active_job(options, within_minutes=5)
    if dup:
        logger.info(
            "Duplicate job skipped – returning existing %s (status=%s)",
            dup["id"],
            dup.get("status"),
        )
        db.append_log(
            dup["id"],
            "Stejný výřez už běží/čeká – další Spustit generování přeskočeno.",
        )
        return dup
    job = db.create_job(name, preset_id, options)
    job_id = job["id"]

    def log(msg: str) -> None:
        db.append_log(job_id, msg)

    # Kopie LAZ z předchozího jobu běží až ve workeru (run_job_pipeline),
    # ať odpověď „job založen“ nepřijde až po dlouhém copy.
    if options.get("reused_from"):
        log(
            f"Iterace z jobu {options['reused_from']}: LAZ se zkopíruje na začátku běhu."
        )

    log(
        f"Prijato: listy={','.join(options['sm5_sheets'])}, "
        f"1:{resolved['map_scale']} · {resolved['contour_interval']} m "
        f"(preset={preset_id}), "
        f"vege={options.get('kp_vege_height', 2.0)} m, "
        f"srázy={options.get('kp_cliff_sensitivity', 'normal')}/"
        f"{options.get('kp_cliff_symbol', 'earth_bank')}, "
        f"lavičky={'ano' if options.get('kp_osm_benches') else 'ne'}, "
        f"lampy={'ano' if options.get('kp_osm_lamps') else 'ne'}, "
        f"herní prvky={'ano' if options.get('kp_osm_playground_equipment') else 'ne'}, "
        f"priorita OSM={'ano' if options.get('kp_osm_priority') else 'ne'}, "
        f"footway=chodník={'ano' if options.get('kp_osm_footway_as_sidewalk') else 'ne'}, "
        f"dvory oliva={'ano' if options.get('sprint_courtyard_olive', True) else 'ne'}, "
        f"residential 501={'ano' if options.get('sprint_residual_paved') else 'ne'}"
        f"/{options.get('sprint_residual_size', 'small')}, "
        f"ostatní plocha={options.get('ostatni_plocha', 'small')}"
        f"{'/403' if options.get('ostatni_plocha_as_403') else ''}, "
        f"ref. PNG={'ano' if options.get('output_references', True) else 'ne'}, "
        f"výstup={'PNG+ZIP' if options.get('output_zip', True) else 'jen PNG'}"
    )

    try:
        worker.enqueue(job_id)
        pos = worker.queue_position(job_id)
        if pos and pos > 0:
            log(f"Job ve fronte – pozice {pos}.")

        logger.info("Job %s enqueued (queue_pos=%s)", job_id, pos)
        return db.get_job(job_id)
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"CHYBA pri vytvareni jobu: {exc}")
        log(tb)
        db.update_job(job_id, status="failed", phase="upload", error=str(exc))
        logger.exception("Job %s upload failed", job_id)
        raise HTTPException(500, f"Nahrani selhalo: {exc}") from exc


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    png = STATIC_DIR / "logo.png"
    if png.is_file():
        return FileResponse(png, media_type="image/png")
    svg = STATIC_DIR / "logo.svg"
    if svg.is_file():
        return FileResponse(svg, media_type="image/svg+xml")
    raise HTTPException(404, "Logo nenalezeno")


@app.get("/tiles/{z}/{x}/{y}.png", include_in_schema=False)
def map_tile(z: int, x: int, y: int):
    try:
        path = fetch_tile(z, x, y)
    except TileError as exc:
        raise HTTPException(400, str(exc)) from exc
    return FileResponse(
        path,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=86400",
            "CDN-Cache-Control": "public, max-age=604800",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/jobs/{job_id}/start")
def api_start_job(job_id: str):
    try:
        job = db.get_job(job_id)
    except KeyError:
        raise HTTPException(404, "Job nenalezen")
    if job["status"] == "running":
        return job
    if worker.is_busy() and not worker.can_accept_job():
        raise HTTPException(503, f"Fronta je plná (max {MAX_QUEUE_SIZE}).")
    worker.enqueue(job_id)
    return db.get_job(job_id)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
