from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import db, worker
from app.pipeline.ini_builder import load_presets
from app.settings import DEFAULT_OPTIONS, JOBS_DIR


def _form_bool(value: str) -> bool:
    return value.lower() in ("true", "1", "yes", "on")

app = FastAPI(title="Podkladarna", version="1.0.0")

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def startup() -> None:
    db.init_db()


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

    if not dmr_files or not dmp_files:
        raise HTTPException(400, "Nahrajte alespon jeden DMR a jeden DMP soubor (LAZ/LAS)")

    options = {
        **DEFAULT_OPTIONS,
        "run_vectors": _form_bool(run_vectors),
        "output_png": _form_bool(output_png),
        "output_dxf": _form_bool(output_dxf),
        "output_zabaged_clean": _form_bool(output_zabaged_clean),
        "savetempfolders": _form_bool(savetempfolders),
    }
    job = db.create_job(name, preset_id, options)
    job_dir = JOBS_DIR / job["id"]

    async def save_uploads(files: list[UploadFile], dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        for i, uf in enumerate(files):
            if not uf.filename:
                continue
            target = dest / uf.filename
            if target.exists():
                target = dest / f"{i}_{uf.filename}"
            with target.open("wb") as out:
                shutil.copyfileobj(uf.file, out)

    await save_uploads(dmr_files, job_dir / "input" / "dmr")
    await save_uploads(dmp_files, job_dir / "input" / "dmp")
    if zabaged_file and zabaged_file.filename:
        zdest = job_dir / "input" / "zabaged"
        zdest.mkdir(parents=True, exist_ok=True)
        zpath = zdest / (zabaged_file.filename or "Zabaged_full.zip")
        with zpath.open("wb") as out:
            shutil.copyfileobj(zabaged_file.file, out)

    if worker.is_busy():
        db.update_job(job["id"], status="queued")
    else:
        worker.enqueue(job["id"])

    return db.get_job(job["id"])


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
