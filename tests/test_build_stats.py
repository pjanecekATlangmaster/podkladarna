"""Statistiky buildu jobu: časy kroků, data podkladů (staženo / cache), zápis a API detailu."""

from app.pipeline import build_stats
from app.pipeline.job_progress import JobProgress


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_build_stats_steps_sources_and_roundtrip(tmp_path):
    build_stats.reset()
    clock = _Clock()
    progress = JobProgress(None, ["alpha", "beta", "gamma"], clock=clock)
    build_stats.attach_progress(progress)
    progress.begin("alpha")
    clock.t = 30
    progress.skip("beta", reason="z cache")
    progress.begin("gamma")
    clock.t = 100
    progress.finish_all()

    laz = tmp_path / "DMR5G.laz"
    laz.write_bytes(b"x" * 1000)
    build_stats.note_file("lidar_dmr", laz, cached=True)
    build_stats.note_file("lidar_dmr", laz, cached=True)  # týž soubor podruhé se nezapočte
    build_stats.note_bytes("reference", 500, cached=False)
    build_stats.note_file("osm", tmp_path / "chybi.json", cached=True)  # neexistuje → ignorovat
    build_stats.note_map("les", 42.04, parallel=True)
    build_stats.set_info(app_version="9.9.9", dtm=False, area_km2=1.5)
    build_stats.note_background("dtm", packages=3, regions=["CZ010"])
    build_stats.note_background("dtm", seconds=12.34, wait_seconds=0.04)

    assert build_stats.write(tmp_path) is not None
    data = build_stats.read(tmp_path)
    assert data["total_seconds"] == 100
    assert [(s["label"], s["seconds"], bool(s.get("skipped"))) for s in data["steps"]] == [
        ("alpha", 30, False),
        ("beta", 0, True),
        ("gamma", 70, False),
    ]
    assert data["sources"]["lidar_dmr"]["cached_bytes"] == 1000
    assert data["sources"]["lidar_dmr"]["cached_files"] == 1
    assert "osm" not in data["sources"]
    assert (data["downloaded_bytes"], data["cached_bytes"]) == (500, 1000)
    assert data["maps"] == [{"name": "les", "seconds": 42.0, "parallel": True}]
    assert data["background"] == [
        {"name": "dtm", "packages": 3, "regions": ["CZ010"], "seconds": 12.3, "wait_seconds": 0.0}
    ]
    assert (data["app_version"], data["dtm"], data["area_km2"]) == ("9.9.9", False, 1.5)
    build_stats.reset()


def test_job_detail_api_includes_build_stats(client, monkeypatch, tmp_path):
    from app import db, main

    monkeypatch.setattr(main, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(db, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.sqlite")
    db.init_db()
    job_id = db.create_job("Statistiky", "forest_10000", {})["id"]
    assert "build_stats" not in client.get(f"/api/jobs/{job_id}").json()

    build_stats.reset()
    build_stats.note_bytes("zabaged", 2048, cached=False)
    build_stats.write(tmp_path / "jobs" / job_id)
    build_stats.reset()
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["build_stats"]["sources"]["zabaged"]["downloaded_bytes"] == 2048
    # Seznam jobů zůstává lehký.
    listed = [j for j in client.get("/api/jobs").json()["jobs"] if j["id"] == job_id]
    assert listed and "build_stats" not in listed[0]
