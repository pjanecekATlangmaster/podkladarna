from __future__ import annotations

import json
import logging
import shutil
import traceback
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import db, worker
from app.pipeline.ini_builder import load_presets
from app.settings import DEFAULT_OPTIONS, JOBS_DIR

logger = logging.getLogger("podkladarna")


def _form_bool(value: str) -> bool:
    return value.lower() in ("true", "1", "yes", "on")

app = FastAPI(title="Podkladarna", version="1.0.0")

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def startup() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


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


@app.post("/api/jobs")
async def api_create_job(
    name: str = Form(...),
    preset_id: str = Form("sprint_2m"),
    run_vectors: str = Form("true"),
    output_png: str = Form("true"),
    output_dxf: str = Form("true"),
    output_zabaged_clean: str = Form("true"),
    savetempfolders: str = Form("true"),
    dmr_files: list[UploadFile] = File(default=[]),
    dmp_files: list[UploadFile] = File(default=[]),
    zabaged_file: UploadFile | None = File(default=None),
):
    presets = load_presets()
    if preset_id not in presets:
        raise HTTPException(400, f"Neznamy preset: {preset_id}")

    dmr_uploads = [f for f in dmr_files if f.filename]
    dmp_uploads = [f for f in dmp_files if f.filename]
    if not dmr_uploads or not dmp_uploads:
        raise HTTPException(
            400,
            "Nahrajte alespon jeden DMR a jeden DMP soubor (LAZ/LAS). "
            "Pri uploadu pres HTTPS zkontrolujte limit velikosti na reverse proxy.",
        )

    options = {
        **DEFAULT_OPTIONS,
        "run_vectors": _form_bool(run_vectors),
        "output_png": _form_bool(output_png),
        "output_dxf": _form_bool(output_dxf),
        "output_zabaged_clean": _form_bool(output_zabaged_clean),
        "savetempfolders": _form_bool(savetempfolders),
    }
    job = db.create_job(name, preset_id, options)
    job_id = job["id"]
    job_dir = JOBS_DIR / job_id

    def log(msg: str) -> None:
        db.append_log(job_id, msg)

    try:
        async def save_uploads(files: list[UploadFile], dest: Path) -> None:
            dest.mkdir(parents=True, exist_ok=True)
            for i, uf in enumerate(files):
                if not uf.filename:
                    continue
                target = dest / uf.filename
                if target.exists():
                    target = dest / f"{i}_{uf.filename}"
                log(f"Nahravam {uf.filename} …")
                with target.open("wb") as out:
                    shutil.copyfileobj(uf.file, out)
                log(f"OK {uf.filename} ({target.stat().st_size / 1e6:.1f} MB)")

        await save_uploads(dmr_uploads, job_dir / "input" / "dmr")
        await save_uploads(dmp_uploads, job_dir / "input" / "dmp")
        if zabaged_file and zabaged_file.filename:
            zdest = job_dir / "input" / "zabaged"
            zdest.mkdir(parents=True, exist_ok=True)
            zpath = zdest / (zabaged_file.filename or "Zabaged_full.zip")
            log(f"Nahravam {zpath.name} …")
            with zpath.open("wb") as out:
                shutil.copyfileobj(zabaged_file.file, out)
            log(f"OK {zpath.name} ({zpath.stat().st_size / 1e6:.1f} MB)")

        if worker.is_busy():
            db.update_job(job_id, status="queued")
            log("Job ve fronte – ceka na dokonceni predchoziho.")
        else:
            worker.enqueue(job_id)

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
