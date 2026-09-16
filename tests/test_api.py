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


def test_create_job_output_mode_png_only(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main,
        "query_sm5_sheets",
        lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}],
    )
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "enqueue", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "queue_position", lambda *_a, **_k: 0)
    r = client.post(
        "/api/jobs",
        data={
            "name": "png-only",
            "preset_id": "sprint_2m",
            "bbox": "14.40,50.08,14.42,50.09",
            "output_mode": "png",
        },
    )
    assert r.status_code == 200
    assert r.json()["options"]["output_zip"] is False


def test_create_job_skips_reference_pngs(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main,
        "query_sm5_sheets",
        lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}],
    )
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "enqueue", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "queue_position", lambda *_a, **_k: 0)
    r = client.post(
        "/api/jobs",
        data={
            "name": "no-refs",
            "preset_id": "sprint_2m",
            "bbox": "14.40,50.08,14.42,50.09",
            "output_mode": "png_zip",
            # checkbox vypnutý = pole chybí
        },
    )
    assert r.status_code == 200
    assert r.json()["options"]["output_references"] is False

    r2 = client.post(
        "/api/jobs",
        data={
            "name": "with-refs",
            "preset_id": "sprint_2m",
            "bbox": "14.40,50.08,14.42,50.09",
            "output_mode": "png_zip",
            "output_references": "1",
        },
    )
    assert r2.status_code == 200
    assert r2.json()["options"]["output_references"] is True




def test_create_job_sprint_courtyard_olive(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main,
        "query_sm5_sheets",
        lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}],
    )
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "enqueue", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "queue_position", lambda *_a, **_k: 0)
    off = client.post(
        "/api/jobs",
        data={
            "name": "no-olive",
            "preset_id": "sprint_2m",
            "bbox": "14.40,50.08,14.42,50.09",
            "output_mode": "png_zip",
            "output_references": "1",
        },
    )
    assert off.status_code == 200
    assert off.json()["options"]["sprint_courtyard_olive"] is False

    on = client.post(
        "/api/jobs",
        data={
            "name": "olive",
            "preset_id": "sprint_2m",
            "bbox": "14.40,50.08,14.42,50.09",
            "output_mode": "png_zip",
            "output_references": "1",
            "sprint_courtyard_olive": "1",
        },
    )
    assert on.status_code == 200
    assert on.json()["options"]["sprint_courtyard_olive"] is True


def test_map_options(client):
    r = client.get("/api/map_options")
    assert r.status_code == 200
    body = r.json()
    assert body["scales"] == [4000, 7500, 10000, 15000]
    assert body["contours_by_scale"]["4000"] == [2.0, 2.5, 5.0]
    assert body["contours_by_scale"]["10000"] == [5.0]
    assert body["default_contour_by_scale"]["4000"] == 2.5


def test_create_job_with_map_scale(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main,
        "query_sm5_sheets",
        lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}],
    )
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "enqueue", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "queue_position", lambda *_a, **_k: 0)
    r = client.post(
        "/api/jobs",
        data={
            "name": "scale-job",
            "map_scale": "10000",
            "contour_interval": "5",
            "bbox": "14.40,50.08,14.42,50.09",
            "output_mode": "png_zip",
            "output_references": "1",
            "sprint_courtyard_olive": "1",
            "kp_osm_priority": "1",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["preset_id"] == "forest_10000"
    assert body["options"]["map_scale"] == 10000
    assert body["options"]["contour_interval"] == 5.0
    assert body["options"]["scalefactor"] == 1.0

    bad = client.post(
        "/api/jobs",
        data={
            "name": "bad-contour",
            "map_scale": "10000",
            "contour_interval": "2.5",
            "bbox": "14.40,50.08,14.42,50.09",
        },
    )
    assert bad.status_code == 400
    assert "ekvidistance" in bad.json()["detail"].lower()


def test_create_job_rejects_missing_preset(client):
    r = client.post(
        "/api/jobs",
        data={
            "name": "bez-presetu",
            "bbox": "14.40,50.08,14.42,50.09",
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"].lower()
    assert "měřítko" in detail or "meritko" in detail


def test_create_job_rejects_missing_bbox(client):
    r = client.post(
        "/api/jobs",
        data={"name": "bez-vyrezu", "map_scale": "4000", "contour_interval": "2.5"},
    )
    assert r.status_code == 400
    assert "bbox" in r.json()["detail"].lower() or "výřez" in r.json()["detail"].lower()


def test_index_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Podkladárna" in r.text
    html = r.text
    assert "bbox-map" in html
    assert "O co jde" in html
    assert "48 hodin" in html
    assert "PNG náhled" in html
    assert 'name="map_scale"' in html
    assert 'name="contour_interval"' in html
    assert 'name="preset_id"' not in html
    assert 'name="output_mode"' in html
    assert 'name="output_references"' in html
    assert 'id="output_references" value="1" checked' in html
    assert 'name="sprint_courtyard_olive"' in html
    assert "OSM detaily" in html
    assert "courtyard-olive-wrap" in html
    assert "pracovní podklad" in html
    assert "jasně danými" in html
    assert "postaru" in html
    assert "/static/logo.png" in html
    assert "/static/leaflet/leaflet.js" in html
    assert "unpkg.com" not in html
    assert 'name="run_vectors"' not in html
    assert 'name="zabaged_file"' not in html
    assert 'name="dmr_files"' not in html
    assert "job-detail" in html
    assert "job-detail-holder" in html
    assert "jobs-list" in html
    assert "Podkladárna v1.8" in html
    assert "github.com/pjanecekATlangmaster/podkladarna/issues" in html
    assert "zpětnou vazbu" in html
    assert "jobs-live" in html
    assert "jobs-finished-bar" in html
    assert 'href="/licence"' in html
    assert "creativecommons.org/licenses/by/4.0" in html
    assert "DEPLOY.md" not in html
    assert "output-disciplines-hint" in html

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

    monkeypatch.setattr(main, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(db, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.sqlite")
    db.init_db()
    job = db.create_job("Nusle MTBO", "mtbo_10000", {})
    job_id = job["id"]
    out = tmp_path / "jobs" / job_id / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "podkladarna_output.zip").write_bytes(b"zip")

    r = client.get(f"/api/jobs/{job_id}/download/oom")
    assert r.status_code == 200
    assert r.content == b"zip"
    cd = r.headers.get("content-disposition", "")
    assert "podkladarna" in cd
    assert "nusle" in cd.casefold() and "mtbo" in cd.casefold()
    assert job_id not in cd


def test_zip_download_filename_uses_project_name(tmp_path, monkeypatch):
    from app import db

    monkeypatch.setattr(db, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.sqlite")
    db.init_db()
    a = db.create_job("Nusle", "forest_10000", {})
    b = db.create_job("Nusle", "forest_10000", {})
    c = db.create_job("Jiný", "forest_10000", {})
    assert db.zip_download_filename(a["id"], a["name"]) == "podkladarna-nusle.zip"
    assert db.zip_download_filename(b["id"], b["name"]) == "podkladarna-nusle-2.zip"
    assert db.zip_download_filename(c["id"], c["name"]) == "podkladarna-jiný.zip"
    assert db.safe_zip_stem("a/b:c*?.zip") == "abc.zip"
    assert db.safe_zip_stem("") == "podkladarna"
    assert db.zip_download_filename("x", "") == "podkladarna.zip"


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
