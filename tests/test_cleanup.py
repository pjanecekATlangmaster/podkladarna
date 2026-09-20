from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db
from app.cleanup import maybe_purge_old_jobs, purge_old_jobs


def test_purge_old_jobs_removes_expired(tmp_path, monkeypatch):
    monkeypatch.setattr("app.cleanup.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "t.db")
    (tmp_path / "jobs").mkdir()
    db.init_db()

    fresh = db.create_job("fresh", "forest_10000", {})
    old = db.create_job("old", "forest_10000", {})
    db.update_job(fresh["id"], status="done")
    db.update_job(old["id"], status="done")
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET updated_at = ? WHERE id = ?",
            (old_ts, old["id"]),
        )
        conn.commit()

    removed = purge_old_jobs(retention_hours=48)
    assert removed == 1
    ids = {j["id"] for j in db.list_jobs()}
    assert fresh["id"] in ids
    assert old["id"] not in ids


def test_maybe_purge_throttles(monkeypatch):
    calls = {"n": 0}

    def fake_purge(**_kwargs):
        calls["n"] += 1
        return 0

    monkeypatch.setattr("app.cleanup.purge_old_jobs", fake_purge)
    monkeypatch.setattr("app.cleanup._last_pageview_purge", 0.0)
    assert maybe_purge_old_jobs(min_interval_s=3600) == 0
    assert maybe_purge_old_jobs(min_interval_s=3600) == 0
    assert calls["n"] == 1
