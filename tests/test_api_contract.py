"""Stabilní kontrakt API pro externí klienty (Stavitel) – bez dohody neměnit.

Popis: DEV.md „API pro externí klienty (Stavitel) – stabilní kontrakt“.
"""
from __future__ import annotations

import importlib
import zipfile

import pytest

API_KEY = "test-klic-stavitel-0123456789"
BBOX = "14.40,50.08,14.42,50.09"


@pytest.fixture()
def api_client(data_dir, monkeypatch):
    monkeypatch.setenv("API_CLIENT_KEY", API_KEY)
    monkeypatch.setenv("API_KEY_MAX_ACTIVE_JOBS", "1")
    import app.settings as settings

    importlib.reload(settings)
    import app.rate_limit as rate_limit

    importlib.reload(rate_limit)
    import app.db as db
    import app.main as main
    import app.worker as worker

    importlib.reload(db)
    importlib.reload(worker)
    importlib.reload(main)
    db.init_db()
    monkeypatch.setattr(
        main, "query_sm5_sheets", lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}]
    )
    monkeypatch.setattr(main.worker, "enqueue", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "queue_position", lambda *_a, **_k: 0)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        yield client


def _post(client, key: str | None = API_KEY, **data):
    form = {"name": "Stavitel projekt", "map_scale": "10000", "bbox": BBOX, **data}
    headers = {"X-Api-Key": key} if key is not None else {}
    return client.post("/api/jobs", data=form, headers=headers)


@pytest.mark.parametrize(
    "scale, extra, expected_types",
    [
        ("10000", {}, ["les"]),
        ("7500", {"map_types": "les"}, ["les"]),
        ("15000", {}, ["mtbo"]),
        ("10000", {"map_types": ["les", "mtbo"]}, ["les", "mtbo"]),
        ("4000", {"map_types": "sprint"}, None),  # sprint = měřítko, map_types se ignoruje
    ],
)
def test_create_private_job_with_key(api_client, scale, extra, expected_types):
    r = _post(api_client, map_scale=scale, **extra)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] in {"pending", "queued"}
    assert body["private"] is True
    assert body["download_token"]
    assert body["options"]["map_scale"] == int(scale)
    assert body["options"]["bbox_wgs84"] == [14.40, 50.08, 14.42, 50.09]
    assert body["options"].get("map_types") == expected_types
    assert API_KEY not in r.text
    listed = api_client.get("/api/jobs").json()["jobs"]
    assert body["id"] not in {j["id"] for j in listed}


@pytest.mark.parametrize("key", ["spatny-klic", ""])
def test_wrong_key_rejected(api_client, key):
    r = _post(api_client, key=key)
    assert r.status_code == 401
    assert API_KEY not in r.text


def test_key_header_rejected_when_not_configured(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main.app_settings, "API_CLIENT_KEY", "")
    r = client.post(
        "/api/jobs",
        data={"name": "x", "map_scale": "10000", "bbox": BBOX},
        headers={"X-Api-Key": "cokoli"},
    )
    assert r.status_code == 401


def test_key_limit_replaces_ip_limit(api_client):
    first = _post(api_client)
    assert first.status_code == 200
    # Limit podle IP (2 aktivní) se na klíč neuplatní, platí vlastní (zde 1 aktivní job).
    second = _post(api_client, map_scale="15000")
    assert second.status_code == 429
    import app.db as db

    db.update_job(first.json()["id"], status="done")
    third = _post(api_client, map_scale="15000")
    assert third.status_code == 200


def test_duplicate_returns_token(api_client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "check_create_api_job", lambda: None)
    first = _post(api_client).json()
    again = _post(api_client)
    assert again.status_code == 200
    body = again.json()
    assert body["duplicate_skipped"] is True
    assert body["id"] == first["id"]
    assert body["download_token"] == first["download_token"]


def test_download_requires_token(api_client):
    import app.db as db
    from app.settings import JOBS_DIR

    job = _post(api_client).json()
    job_id, token = job["id"], job["download_token"]
    assert api_client.get(f"/api/jobs/{job_id}/download", params={"token": token}).status_code == 409
    out = JOBS_DIR / job_id / "output"
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out / "podkladarna_output.zip", "w") as zf:
        zf.writestr("StavitelProj-les.omap", "<map/>")
    db.update_job(job_id, status="done")
    assert api_client.get(f"/api/jobs/{job_id}").json()["status"] == "done"
    assert api_client.get(f"/api/jobs/{job_id}/download").status_code == 404
    assert api_client.get(f"/api/jobs/{job_id}/download", params={"token": "x"}).status_code == 404
    ok = api_client.get(f"/api/jobs/{job_id}/download", params={"token": token})
    assert ok.status_code == 200
    assert ok.content[:2] == b"PK"


def test_job_status_values():
    from app import db

    assert db.JOB_DONE_STATUSES == {"done", "done_with_errors"}
    assert db.JOB_FINISHED_STATUSES == {"done", "done_with_errors", "failed"}


@pytest.mark.parametrize(
    "scale, map_types, expected",
    [
        (4000, None, ["StavitelProj-sprint.omap"]),
        (7500, ["les"], ["StavitelProj-les.omap"]),
        (10000, ["les"], ["StavitelProj-les.omap"]),
        (10000, ["mtbo"], ["StavitelProj-mtbo.omap"]),
        (10000, ["les", "mtbo"], ["StavitelProj-les.omap", "StavitelProj-mtbo.omap"]),
        (15000, ["mtbo"], ["StavitelProj-mtbo.omap"]),
    ],
)
def test_omap_names_in_zip(scale, map_types, expected):
    from app.pipeline.package_oom import (
        BUILD_CERTAIN_MAPS,
        omap_variant_filename,
        resolve_discipline_presets,
    )
    from app.pipeline.job_options import load_presets

    assert BUILD_CERTAIN_MAPS is False
    discs = resolve_discipline_presets(
        "", load_presets(), map_scale=scale, map_types=map_types
    )
    names = [omap_variant_filename(tag, map_name="Stavitel projekt") for tag, _p, _s in discs]
    assert names == expected


def test_omap_stem_rules():
    from app.pipeline.package_oom import omap_map_stem

    assert omap_map_stem("Česká lípa park") == "CeskaLipaPar"
    assert omap_map_stem("") == "Mapa"
