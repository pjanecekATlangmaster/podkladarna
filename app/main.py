from __future__ import annotations

import logging
import mimetypes
import shutil
import traceback
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db, worker
from app.cleanup import purge_old_jobs, start_cleanup_scheduler
from app.pipeline.fetch_openzu import (
    FetchError,
    MAX_BBOX_KM,
    MAX_SHEETS,
    bbox_exceeds_limit,
    bbox_size_km,
    estimate_minutes,
    parse_bbox,
    query_sm5_sheets,
)
from app.pipeline.ini_builder import load_presets
from app.settings import CLEANUP_INTERVAL_HOURS, DEFAULT_OPTIONS, JOBS_DIR, MAX_QUEUE_SIZE
from app.tiles import TileError, fetch_tile
from app.tool_env import tool_status

logger = logging.getLogger("podkladarna")


def _form_bool(value: str | None) -> bool:
    return str(value or "").lower() in ("true", "1", "yes", "on")


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

app = FastAPI(title="Podkladarna", version="1.2.0")

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
# Minimální image (conda) často nezná .svg → nginx nosniff logo nenačte.
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("image/png", ".png")


@app.on_event("startup")
def startup() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()
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


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (STATIC_DIR.parent / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/presets")
def api_presets():
    presets = load_presets()
    return {
        k: {"id": k, "label": v.get("label", k), **v}
        for k, v in presets.items()
    }


@app.get("/api/sheets")
def api_sheets(bbox: str):
    """bbox=west,south,east,north (WGS84) → listy SM5 + odhad času."""
    try:
        west, south, east, north = parse_bbox(bbox)
    except FetchError as exc:
        raise HTTPException(400, str(exc)) from exc
    width_km, height_km = bbox_size_km(west, south, east, north)
    if bbox_exceeds_limit(west, south, east, north):
        return {
            "sheets": [],
            "count": 0,
            "max_sheets": MAX_SHEETS,
            "width_km": round(width_km, 2),
            "height_km": round(height_km, 2),
            "max_km": MAX_BBOX_KM,
            "too_large": True,
            "too_large_reason": "size",
            "estimate_minutes": None,
            "label": f"Výřez {width_km:.1f} × {height_km:.1f} km",
            "hint": (
                f"Výřez {width_km:.1f} × {height_km:.1f} km je moc velký "
                f"(max {MAX_BBOX_KM:.0f} × {MAX_BBOX_KM:.0f} km). Zmenšete ho."
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
    return {
        "sheets": sheets,
        "count": len(sheets),
        "max_sheets": MAX_SHEETS,
        "width_km": round(width_km, 2),
        "height_km": round(height_km, 2),
        "max_km": MAX_BBOX_KM,
        "too_large": sheets_too_big,
        "too_large_reason": "sheets" if sheets_too_big else None,
        "estimate_minutes": estimate_minutes(len(sheets)) if sheets and not sheets_too_big else None,
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


@app.get("/api/jobs/{job_id}/download")
def api_download(job_id: str):
    zip_path = JOBS_DIR / job_id / "output" / "podkladarna_output.zip"
    if not zip_path.exists():
        raise HTTPException(404, "Vystup jeste neni pripraven")
    return FileResponse(zip_path, filename=f"podkladarna_{job_id}.zip")


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
    usage = shutil.disk_usage(JOBS_DIR)
    tools = tool_status()
    return {
        "ok": True,
        "data_dir": str(JOBS_DIR),
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

    preset_id = _form_str(form, "preset_id").strip()
    presets = load_presets()
    if not preset_id:
        raise HTTPException(400, "Chybí typ mapy.")
    if preset_id not in presets:
        raise HTTPException(400, f"Neznamy preset: {preset_id}")

    if worker.is_busy() and not worker.can_accept_job():
        raise HTTPException(
            503,
            f"Fronta je plná (max {MAX_QUEUE_SIZE} čekajících jobů). "
            "Počkejte na dokončení běžících generování.",
        )

    source_mode = _form_str(form, "source_mode").strip().lower()
    bbox_raw = _form_str(form, "bbox").strip()
    bbox: tuple[float, float, float, float] | None = None
    if source_mode == "map" or (not source_mode and bbox_raw):
        source_mode = "map"
        if not bbox_raw:
            raise HTTPException(400, "Chybí výřez na mapě (bbox).")
        try:
            bbox = parse_bbox(bbox_raw)
        except FetchError as exc:
            raise HTTPException(400, str(exc)) from exc
        width_km, height_km = bbox_size_km(*bbox)
        if bbox_exceeds_limit(*bbox):
            raise HTTPException(
                400,
                f"Výřez je moc velký ({width_km:.1f} × {height_km:.1f} km, "
                f"max {MAX_BBOX_KM:.0f} × {MAX_BBOX_KM:.0f} km).",
            )
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
    else:
        source_mode = "upload"

    dmr_uploads = _upload_files(form, "dmr_files")
    dmp_uploads = _upload_files(form, "dmp_files")
    zabaged_uploads = _upload_files(form, "zabaged_file")
    zabaged_file = zabaged_uploads[0] if zabaged_uploads else None

    if source_mode == "upload" and (not dmr_uploads or not dmp_uploads):
        field_names = sorted(form.keys())
        raise HTTPException(
            400,
            f"Chybi DMR nebo DMP soubor. Prijata pole: {field_names}. "
            "Ocekavam dmr_files a dmp_files s LAZ/LAS.",
        )

    options = {
        **DEFAULT_OPTIONS,
        "run_vectors": _form_bool(_form_str(form, "run_vectors", "true")),
        "output_png": _form_bool(_form_str(form, "output_png", "true")),
        "output_dxf": _form_bool(_form_str(form, "output_dxf", "true")),
        "output_zabaged_clean": _form_bool(_form_str(form, "output_zabaged_clean", "false")),
        "savetempfolders": _form_bool(_form_str(form, "savetempfolders", "false")),
        "source_mode": source_mode,
    }
    if bbox:
        options["bbox_wgs84"] = list(bbox)
        options["sm5_sheets"] = [s["mapnom"] for s in sheets]
    reuse_id = _form_str(form, "reuse_job_id").strip()
    if reuse_id and bbox:
        try:
            prev = db.get_job(reuse_id)
        except KeyError:
            prev = None
        prev_bbox = (prev or {}).get("options", {}).get("bbox_wgs84")
        if prev and prev.get("has_reusable_lidar") and db.bbox_close(prev_bbox or [], bbox):
            options["reused_from"] = reuse_id
    job = db.create_job(name, preset_id, options)
    job_id = job["id"]
    job_dir = JOBS_DIR / job_id

    def log(msg: str) -> None:
        db.append_log(job_id, msg)

    if options.get("reused_from"):
        copied = db.copy_reusable_work(options["reused_from"], job_id)
        log(
            f"Iterace z jobu {options['reused_from']}: kopíruji {len(copied)} souborů "
            "(LiDAR/ZABAGED, bez ČÚZK). Temp Karttapullautinu se z LAZ sestaví znovu."
        )

    log(
        f"Prijato: rezim={source_mode}, DMR={len(dmr_uploads)}, DMP={len(dmp_uploads)}, "
        f"ZABAGED={'ano' if zabaged_file else 'ne'}"
        + (f", listy={','.join(options.get('sm5_sheets') or [])}" if bbox else "")
    )

    try:
        async def save_uploads(files: list[UploadFile], dest: Path) -> None:
            dest.mkdir(parents=True, exist_ok=True)
            for i, uf in enumerate(files):
                raw_name = uf.filename or f"upload_{i}.bin"
                target = dest / Path(raw_name).name
                if target.exists():
                    target = dest / f"{i}_{Path(raw_name).name}"
                log(f"Nahravam {target.name} …")
                with target.open("wb") as out:
                    shutil.copyfileobj(uf.file, out)
                log(f"OK {target.name} ({target.stat().st_size / 1e6:.1f} MB)")

        if source_mode == "upload":
            await save_uploads(dmr_uploads, job_dir / "input" / "dmr")
            await save_uploads(dmp_uploads, job_dir / "input" / "dmp")
        if zabaged_file:
            zdest = job_dir / "input" / "zabaged"
            zdest.mkdir(parents=True, exist_ok=True)
            zname = Path(zabaged_file.filename or "Zabaged_full.zip").name
            zpath = zdest / zname
            log(f"Nahravam {zpath.name} …")
            with zpath.open("wb") as out:
                shutil.copyfileobj(zabaged_file.file, out)
            log(f"OK {zpath.name} ({zpath.stat().st_size / 1e6:.1f} MB)")

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
