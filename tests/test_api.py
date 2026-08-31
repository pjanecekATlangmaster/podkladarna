from __future__ import annotations

import time


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "disk_free_gb" in body


def test_presets(client):
    r = client.get("/api/presets")
    assert r.status_code == 200
    presets = r.json()
    assert "sprint_2m" in presets
    assert "forest_7500" in presets


def test_create_job_multipart(client):
    r = client.post(
        "/api/jobs",
        data={
            "name": "test-upload",
            "preset_id": "sprint_2m",
            "run_vectors": "false",
            "output_png": "true",
            "output_dxf": "true",
            "output_zabaged_clean": "false",
            "savetempfolders": "true",
        },
        files=[
            ("dmr_files", ("DMR5G.laz", b"fake-laz-dmr", "application/octet-stream")),
            ("dmp_files", ("DMP1G.laz", b"fake-laz-dmp", "application/octet-stream")),
        ],
    )
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["id"]
    assert job["status"] in ("running", "queued", "failed")

    log = client.get(f"/api/jobs/{job['id']}/log")
    assert log.status_code == 200
    lines = [x["line"] for x in log.json()["lines"]]
    assert any("Prijato: DMR=1" in ln for ln in lines)
    assert any("Nahravam DMR5G.laz" in ln for ln in lines)


def test_create_job_rejects_missing_dmr(client):
    r = client.post(
        "/api/jobs",
        data={"name": "bez-dmr", "preset_id": "sprint_2m"},
        files=[
            ("dmp_files", ("DMP1G.laz", b"x", "application/octet-stream")),
        ],
    )
    assert r.status_code == 400
    assert "DMR" in r.json()["detail"]


def test_create_job_with_zabaged(client):
    r = client.post(
        "/api/jobs",
        data={"name": "se-zabaged", "preset_id": "sprint_2m", "run_vectors": "false"},
        files=[
            ("dmr_files", ("a.laz", b"dmr", "application/octet-stream")),
            ("dmp_files", ("b.laz", b"dmp", "application/octet-stream")),
            ("zabaged_file", ("Zabaged.zip", b"PK\x03\x04", "application/zip")),
        ],
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    log_lines = client.get(f"/api/jobs/{job_id}/log").json()["lines"]
    text = "\n".join(x["line"] for x in log_lines)
    assert "ZABAGED=ano" in text


def test_index_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Podkladárna" in r.text
