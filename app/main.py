from __future__ import annotations

import logging
import shutil
import traceback
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db, worker
from app.pipeline.ini_builder import load_presets
from app.settings import DEFAULT_OPTIONS, JOBS_DIR

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
        # filename může být None u některých klientů – řeší se při ukládání
        files.append(value)
    return files


def _form_str(form, key: str, default: str = "") -> str:
    val = form.get(key)
    if val is None:
        return default
    return str(val)

app = FastAPI(title="Podkladarna", version="1.0.0")

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def startup() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()


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


@app.get("/api/jobs")
def api_list_jobs():
    return {"jobs": db.list_jobs(), "busy": worker.is_busy(), "current": worker.current_job_id()}


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
    return {
        "ok": True,
        "data_dir": str(JOBS_DIR),
        "disk_free_gb": round(usage.free / 1e9, 2),
        "busy": worker.is_busy(),
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

    preset_id = _form_str(form, "preset_id", "sprint_2m")
    presets = load_presets()
    if preset_id not in presets:
        raise HTTPException(400, f"Neznamy preset: {preset_id}")

    dmr_uploads = _upload_files(form, "dmr_files")
    dmp_uploads = _upload_files(form, "dmp_files")
    zabaged_uploads = _upload_files(form, "zabaged_file")
    zabaged_file = zabaged_uploads[0] if zabaged_uploads else None

    if not dmr_uploads or not dmp_uploads:
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
        "output_zabaged_clean": _form_bool(_form_str(form, "output_zabaged_clean", "true")),
        "savetempfolders": _form_bool(_form_str(form, "savetempfolders", "true")),
    }
    job = db.create_job(name, preset_id, options)
    job_id = job["id"]
    job_dir = JOBS_DIR / job_id

    def log(msg: str) -> None:
        db.append_log(job_id, msg)

    log(
        f"Prijato: DMR={len(dmr_uploads)}, DMP={len(dmp_uploads)}, "
        f"ZABAGED={'ano' if zabaged_file else 'ne'}"
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

        if worker.is_busy():
            db.update_job(job_id, status="queued")
            log("Job ve fronte – ceka na dokonceni predchoziho.")
        else:
            worker.enqueue(job_id)

        logger.info("Job %s created and queued", job_id)
        return db.get_job(job_id)
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"CHYBA pri vytvareni jobu: {exc}")
        log(tb)
        db.update_job(job_id, status="failed", phase="upload", error=str(exc))
        logger.exception("Job %s upload failed", job_id)
        raise HTTPException(500, f"Nahrani selhalo: {exc}") from exc


@app.post("/api/jobs/{job_id}/start")
def api_start_job(job_id: str):
    try:
        job = db.get_job(job_id)
    except KeyError:
        raise HTTPException(404, "Job nenalezen")
    if job["status"] == "running":
        return job
    if worker.is_busy():
        db.update_job(job_id, status="queued")
        return db.get_job(job_id)
    worker.enqueue(job_id)
    return db.get_job(job_id)
