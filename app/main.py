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
from app.feedback import (
    MAX_COMMENT_LEN,
    MAX_CONTACT_LEN,
    MAX_JOB_ID_LEN,
    check_feedback_rate,
    send_feedback,
)
from app.guide_text import WEB_ABOUT_HTML
from app.mail import MailError
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
from app.settings import (
    CLEANUP_INTERVAL_HOURS,
    DEFAULT_OPTIONS,
    DOWNLOADS_DIR,
    JOBS_DIR,
    MAX_QUEUE_SIZE,
    APP_VERSION,
    PRIVATE_JOB_RETENTION_HOURS,
    default_footway_as_sidewalk,
)
from app.tiles import TileError, fetch_tile
from app.tool_env import log_ignored_gdal_plugins, tool_status
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
    log_ignored_gdal_plugins()
    db.init_db()
    interrupted = worker.recover_after_restart()
    if interrupted:
        logger.info("Recovered %s interrupted job(s): %s", len(interrupted), interrupted)
    from app.job_worker import retry_missed_private_mails

    resent = retry_missed_private_mails()
    if resent:
        logger.info("Resent private download mail for %s job(s): %s", len(resent), resent)
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
            "Windows: OSGeo4W / QGIS PATH, nebo docker compose -f docker-compose.dev.yml up",
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
    html = html.replace("{{APP_VERSION}}", APP_VERSION)
    # Cloudflare cachuje /static/* až 4 h – query podle verze vynutí nový JS/CSS po deployi.
    v = urllib.parse.quote(APP_VERSION, safe="")
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={v}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={v}"')
    return HTMLResponse(html)


@app.get("/licence", response_class=HTMLResponse)
def licence_page() -> HTMLResponse:
    html = (WEB_DIR / "licence.html").read_text(encoding="utf-8")
    html = html.replace("{{APP_VERSION}}", APP_VERSION)
    v = urllib.parse.quote(APP_VERSION, safe="")
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={v}"')
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
    jobs = db.list_jobs(include_private=False)
    for job in jobs:
        pos = worker.queue_position(job["id"])
        if pos is not None:
            job["queue_position"] = pos
    return {"jobs": jobs, **worker.queue_snapshot()}


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str):
    try:
        job = db.get_job(job_id)
    except KeyError:
        raise HTTPException(404, "Job nenalezen")
    if db.job_is_expired(job):
        db.delete_job(job_id)
        raise HTTPException(410, "Privátní job vypršel a byl smazán.")
    return job


@app.get("/api/jobs/{job_id}/log")
def api_job_log(job_id: str, after: int = 0):
    try:
        job = db.get_job(job_id)
    except KeyError:
        raise HTTPException(404, "Job nenalezen")
    if db.job_is_expired(job):
        db.delete_job(job_id)
        raise HTTPException(410, "Privátní job vypršel a byl smazán.")
    return {"lines": db.get_logs(job_id, after)}


@app.get("/api/jobs/{job_id}/download/oom")
def api_download_oom(job_id: str, token: str | None = None):
    """Zpětná kompatibilita – stejný balíček jako /download."""
    return api_download(job_id, token=token)


def _output_zip_path(job_id: str) -> Path | None:
    out = JOBS_DIR / job_id / "output"
    for name in ("podkladarna_output.zip", "podkladarna_oom.zip"):
        path = out / name
        if path.is_file():
            return path
    return None


def _load_job_for_artifact(job_id: str, token: str | None) -> dict:
    """Načte job; u privátních vyžaduje platný token a kontroluje expiraci."""
    try:
        job = db.get_job(job_id, reveal_artifacts=True)
    except KeyError:
        raise HTTPException(404, "Job nenalezen") from None
    if db.job_is_private(job):
        raw_token = None
        with db.connect() as conn:
            row = conn.execute(
                "SELECT download_token FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row:
                raw_token = row["download_token"]
        job["download_token"] = raw_token
        if db.job_is_expired(job):
            db.delete_job(job_id)
            raise HTTPException(410, "Privátní odkaz vypršel – job byl smazán.")
        if not db.token_matches(job, token):
            raise HTTPException(404, "Job nenalezen")
    return job


@app.get("/api/jobs/{job_id}/download")
def api_download(job_id: str, token: str | None = None):
    job = _load_job_for_artifact(job_id, token)
    zip_path = _output_zip_path(job_id)
    if not zip_path:
        raise HTTPException(404, "Vystup jeste neni pripraven")
    filename = db.zip_download_filename(job_id, job["name"])
    return FileResponse(zip_path, filename=filename)


@app.get("/api/jobs/{job_id}/download/georef-previews")
def api_download_georef_previews(job_id: str, token: str | None = None):
    """Malý ZIP jen s georeferencovanými náhledy (PNG+PGW±GeoTIFF)."""
    from app.pipeline.oom_preview import (
        GEOREF_PREVIEWS_ZIP_NAME,
        build_georef_previews_zip,
    )

    job = _load_job_for_artifact(job_id, token)
    out = JOBS_DIR / job_id / "output"
    zip_path = out / GEOREF_PREVIEWS_ZIP_NAME
    if not zip_path.is_file():
        # Lazy: sestav z preview/, pokud job doběhl před zavedením malého ZIPu.
        built = build_georef_previews_zip(out)
        if built is None or not built.is_file():
            raise HTTPException(404, "Georeferencovane nahledy nejsou k dispozici")
        zip_path = built
    stem = db.safe_zip_stem(job["name"])
    filename = f"podkladarna-{stem}-georef-nahledy.zip"
    return FileResponse(zip_path, filename=filename)


@app.get("/api/jobs/{job_id}/preview.png")
def api_preview(job_id: str, token: str | None = None):
    from app.pipeline.preview import resolve_preview_png

    _load_job_for_artifact(job_id, token)
    job_dir = JOBS_DIR / job_id
    png = resolve_preview_png(job_dir / "output", job_dir / "work")
    if png is None:
        raise HTTPException(404, "Nahled neni k dispozici")
    return FileResponse(png)


@app.get("/d/{token}")
def download_by_token(token: str):
    """Privátní stažení ZIP podle neprůhledného tokenu (odkaz z e-mailu)."""
    try:
        job = db.get_job_by_download_token(token)
    except KeyError:
        raise HTTPException(404, "Odkaz neplatí nebo job neexistuje.") from None
    if db.job_is_expired(job):
        db.delete_job(job["id"])
        raise HTTPException(410, "Privátní odkaz vypršel – job byl smazán.")
    zip_path = _output_zip_path(job["id"])
    if not zip_path:
        raise HTTPException(404, "Vystup jeste neni pripraven")
    filename = db.zip_download_filename(job["id"], job["name"])
    return FileResponse(zip_path, filename=filename)


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


@app.post("/api/feedback")
async def api_feedback(request: Request):
    """Textová zpětná vazba → e-mail Petrovi.

    Nezávislé na frontě generování (žádný worker.is_busy / check_create_job).
    """
    content_type = (request.headers.get("content-type") or "").lower()
    honeypot = ""
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(400, "Neplatný JSON.") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "Neplatný JSON.")
        contact = str(payload.get("contact") or "").strip()
        comment = str(payload.get("comment") or "").strip()
        job_id = str(payload.get("job_id") or "").strip()
        honeypot = str(
            payload.get("website") or payload.get("company") or ""
        ).strip()
    else:
        try:
            form = await request.form()
        except Exception as exc:
            raise HTTPException(400, f"Nepodařilo se přečíst formulář: {exc}") from exc
        contact = _form_str(form, "contact").strip()
        comment = _form_str(form, "comment").strip()
        job_id = _form_str(form, "job_id").strip()
        honeypot = (
            _form_str(form, "website").strip()
            or _form_str(form, "company").strip()
        )

    # Honeypot: boti vyplní skryté pole — tiše OK, nic neodesílat.
    if honeypot:
        logger.info("Feedback honeypot hit from %s", client_ip(request))
        return {"ok": True}

    if not contact:
        raise HTTPException(400, "Vyplňte kontakt (e-mail nebo telefon).")
    if not comment:
        raise HTTPException(400, "Napište komentář.")
    if not job_id:
        raise HTTPException(400, "Chybí job_id – zpětná vazba patří ke konkrétnímu jobu.")
    if len(contact) > MAX_CONTACT_LEN:
        raise HTTPException(400, f"Kontakt je moc dlouhý (max {MAX_CONTACT_LEN} znaků).")
    if len(comment) > MAX_COMMENT_LEN:
        raise HTTPException(400, f"Komentář je moc dlouhý (max {MAX_COMMENT_LEN} znaků).")
    if len(job_id) > MAX_JOB_ID_LEN:
        raise HTTPException(400, "Neplatné job_id.")
    if not all(c.isalnum() or c in "-_" for c in job_id):
        raise HTTPException(400, "Neplatné job_id.")

    # Job může běžet, čekat i být hotový — odeslat jde vždy (Petr 2026-10-05).
    job_name: str | None = None
    job_status: str | None = None
    try:
        job = db.get_job(job_id)
        job_name = str(job.get("name") or "").strip() or None
        job_status = str(job.get("status") or "").strip() or None
    except KeyError:
        raise HTTPException(404, "Job nenalezen.") from None

    ip = client_ip(request)
    limit_msg = check_feedback_rate(ip)
    if limit_msg:
        raise HTTPException(429, limit_msg)

    try:
        send_feedback(
            contact=contact,
            comment=comment,
            job_id=job_id,
            client_ip=ip,
            job_name=job_name,
            job_status=job_status,
        )
    except MailError as exc:
        # 502, ne 503 — 503 si FE/klienti spojují s plnou frontou generování.
        logger.warning("Feedback mail failed: %s", exc)
        raise HTTPException(
            502,
            "Zpětnou vazbu teď nešlo odeslat (e-mail). Zkuste to později, "
            "nebo napište na GitHub Issues.",
        ) from exc

    return {"ok": True}


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
    if cliff_raw == "rock_face":
        # Legacy: force linear 201 už nedává smysl (skály = plochy 201.2/206).
        cliff_raw = "auto"
    if cliff_raw in {"auto", "earth_bank", "symbol_206", "off"}:
        options["kp_cliff_symbol"] = cliff_raw
    knoll_raw = _form_str(form, "include_knolls").strip().lower()
    if knoll_raw in {"0", "false", "no", "off"}:
        options["include_knolls"] = False
    elif knoll_raw in {"1", "true", "yes", "on"}:
        options["include_knolls"] = True
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
    footway_raw = _form_str(form, "kp_osm_footway_as_sidewalk").strip().lower()
    if footway_raw in {"0", "false", "no", "off"}:
        options["kp_osm_footway_as_sidewalk"] = False
    elif footway_raw in {"1", "true", "yes", "on"}:
        options["kp_osm_footway_as_sidewalk"] = True
    else:
        # Chybí ve formuláři → sprint default ON, les/MTBO OFF.
        options["kp_osm_footway_as_sidewalk"] = default_footway_as_sidewalk(
            options.get("map_scale"),
            preset_id,
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
    # Georef PNG/TIFF do ZIPu – default off (formulářové combo Formát už není).
    options["output_georef"] = _opt_bool("output_georef")
    options["output_references"] = _opt_bool("output_references")
    options["force_refresh"] = _opt_bool("force_refresh")
    # Privátní režim: mimo veřejný seznam + e-mail s tokenizovaným odkazem.
    if _opt_bool("private"):
        notify_email = _form_str(form, "notify_email").strip()
        if not notify_email or "@" not in notify_email or "." not in notify_email.split("@")[-1]:
            raise HTTPException(
                400,
                "Privátní režim vyžaduje platný e-mail pro odkaz ke stažení.",
            )
        options["private"] = True
        options["notify_email"] = notify_email
    # Legacy API flag – vždy vypnuto (Karttapullautin runtime není).
    options["use_kp"] = False
    reuse_id = _form_str(form, "reuse_job_id").strip()
    if reuse_id:
        try:
            prev = db.get_job(reuse_id)
        except KeyError:
            prev = None
        prev_bbox = (prev or {}).get("options", {}).get("bbox_wgs84")
        if prev and prev.get("has_reusable_lidar") and db.bbox_close(prev_bbox or [], bbox):
            options["reused_from"] = reuse_id
    dup = db.find_duplicate_active_job(options)
    if dup:
        logger.info(
            "Duplicate job skipped – returning existing %s (status=%s)",
            dup["id"],
            dup.get("status"),
        )
        msg = (
            "Nový job se nezaložil – stejný výřez a volby už "
            f"{'běží' if dup.get('status') == 'running' else 'čekají'} "
            f"(job {dup['id'][:8]}…, „{dup.get('name') or 'bez názvu'}“)."
        )
        db.append_log(dup["id"], msg)
        out = dict(dup)
        out["duplicate_skipped"] = True
        out["duplicate_message"] = msg
        return out
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
        f"srázy={options.get('kp_cliff_sensitivity', 'low')}/"
        f"{options.get('kp_cliff_symbol', 'auto')}, "
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
        f"georef PNG/TIFF={'ano' if options.get('output_georef') else 'ne'}, "
        f"force_refresh={'ano' if options.get('force_refresh') else 'ne'}, "
        f"privátní={'ano' if options.get('private') else 'ne'}, "
        f"výstup={'PNG+ZIP' if options.get('output_zip', True) else 'jen PNG'}"
    )
    if options.get("private"):
        hours = PRIVATE_JOB_RETENTION_HOURS if PRIVATE_JOB_RETENTION_HOURS > 0 else 48
        log(
            f"Privátní režim – po dokončení přijde e-mail na {options.get('notify_email')} "
            f"(odkaz platí {hours} h)."
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
