from __future__ import annotations

import time


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "disk_free_gb" in body
    assert "downloads_dir" in body


def test_presets(client):
    r = client.get("/api/presets")
    assert r.status_code == 200
    presets = r.json()
    assert "sprint_2m" in presets
    assert "forest_7500" in presets
    assert "mtbo_10000" in presets
    assert "mtbo_15000" in presets
    assert presets["mtbo_10000"]["label"] == "MTBO 1:10000 · 5 m"
    assert presets["mtbo_15000"]["scalefactor"] == 1.5
    assert presets["mtbo_10000"]["group"] == "MTBO"


def test_create_job_rejects_laz_upload(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main,
        "query_sm5_sheets",
        lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}],
    )
    r = client.post(
        "/api/jobs",
        data={
            "name": "upload",
            "preset_id": "sprint_2m",
            "bbox": "14.40,50.08,14.42,50.09",
        },
        files=[
            ("dmr_files", ("DMR5G.laz", b"fake-laz-dmr", "application/octet-stream")),
            ("dmp_files", ("DMP1G.laz", b"fake-laz-dmp", "application/octet-stream")),
        ],
    )
    assert r.status_code == 400
    assert "upload" in r.json()["detail"].lower()


def test_create_job_rejects_missing_preset(client):
    r = client.post(
        "/api/jobs",
        data={
            "name": "bez-presetu",
            "bbox": "14.40,50.08,14.42,50.09",
        },
    )
    assert r.status_code == 400
    assert "typ mapy" in r.json()["detail"].lower()


def test_create_job_rejects_missing_bbox(client):
    r = client.post(
        "/api/jobs",
        data={"name": "bez-vyrezu", "preset_id": "sprint_2m"},
    )
    assert r.status_code == 400
    assert "bbox" in r.json()["detail"].lower() or "výřez" in r.json()["detail"].lower()


def test_index_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Podkladárna" in r.text
    html = r.text
    assert "bbox-map" in html
    assert "/static/logo.png" in html
    assert "/static/leaflet/leaflet.js" in html
    assert "unpkg.com" not in html
    assert 'name="run_vectors"' not in html
    assert 'name="zabaged_file"' not in html
    assert 'name="dmr_files"' not in html
    assert "job-detail" in html
    assert "job-detail-holder" in html
    assert "jobs-list" in html
    assert "Podkladárna v1.5" in html
    assert "jobs-live" in html
    assert "jobs-finished-bar" in html
    assert 'href="/licence"' in html
    assert "creativecommons.org/licenses/by/4.0" in html
    assert "DEPLOY.md" not in html


def test_licence_page(client):
    r = client.get("/licence")
    assert r.status_code == 200
    html = r.text
    assert "MIT" in html
    assert "ČÚZK" in html
    assert "ZABAGED" in html
    assert "Karttapullautin" in html
    assert "Leaflet" in html
    assert "OpenStreetMap" in html
    assert "Petr Janeček" in html


def test_download_oom_redirects_to_main_zip(client, tmp_path, monkeypatch):
    """Starší URL /download/oom vrací stejný balíček jako /download."""
    from app import db, main

    job_id = "oomlegacy"
    out = tmp_path / "jobs" / job_id / "output"
    out.mkdir(parents=True)
    (out / "podkladarna_output.zip").write_bytes(b"zip")
    monkeypatch.setattr(main, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(db, "JOBS_DIR", tmp_path / "jobs")

    r = client.get(f"/api/jobs/{job_id}/download/oom")
    assert r.status_code == 200
    assert r.content == b"zip"


def test_logo_png(client):
    r = client.get("/static/logo.png")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/png")
    assert len(r.content) > 100


def test_favicon(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")


def test_copy_reusable_lidar(data_dir):
    import app.db as db

    db.init_db()
    src = db.create_job("stary", "sprint_2m", {"bbox_wgs84": [14.4, 50.08, 14.42, 50.09]})
    dest = db.create_job("novy", "sprint_2m", {})
    lidar = db.JOBS_DIR / src["id"] / "work" / "lidar"
    lidar.mkdir(parents=True, exist_ok=True)
    (lidar / "merged_crop.laz").write_bytes(b"x" * 2000)
    zabaged = db.JOBS_DIR / src["id"] / "input" / "zabaged"
    zabaged.mkdir(parents=True, exist_ok=True)
    (zabaged / "Zabaged_ags.zip").write_bytes(b"old-zip")
    copied = db.copy_reusable_work(src["id"], dest["id"])
    assert any("merged_crop.laz" in name for name in copied)
    assert (db.JOBS_DIR / dest["id"] / "work" / "lidar" / "merged_crop.laz").exists()
    assert not any("zabaged" in name for name in copied)
    assert not (db.JOBS_DIR / dest["id"] / "input" / "zabaged" / "Zabaged_ags.zip").exists()
    assert db.get_job(src["id"])["has_reusable_lidar"] is True
    assert db.bbox_close(src["options"]["bbox_wgs84"], [14.4, 50.08, 14.42, 50.09])
