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
    """Řídíme monotonic – na CI často začíná u 0 a 0.0 ≠ „dávno“."""
    calls = {"n": 0}
    clock = {"t": 10_000.0}

    def fake_purge(**_kwargs):
        calls["n"] += 1
        return 0

    monkeypatch.setattr("app.cleanup.purge_old_jobs", fake_purge)
    monkeypatch.setattr("app.cleanup.time.monotonic", lambda: clock["t"])
    monkeypatch.setattr("app.cleanup._last_pageview_purge", None)

    assert maybe_purge_old_jobs(min_interval_s=3600) == 0
    assert calls["n"] == 1

    clock["t"] += 10
    assert maybe_purge_old_jobs(min_interval_s=3600) == 0
    assert calls["n"] == 1

    clock["t"] += 3600
    assert maybe_purge_old_jobs(min_interval_s=3600) == 0
    assert calls["n"] == 2


def test_job_origin_survives_job_purge_and_expires(tmp_path, monkeypatch):
    """Původ přežije úklid jobu (se stavem), IP / e-mail jen jako značky, retence ve dnech."""
    monkeypatch.setattr("app.cleanup.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "t.db")
    (tmp_path / "jobs").mkdir()
    db.init_db()

    pub = db.create_job("pub", "forest_10000", {"client_ip": "1.2.3.4", "notify_email": "a@b.cz"})
    priv = db.create_job("priv", "forest_10000", {"client_ip": "1.2.3.4", "private": True, "notify_email": "x@y.cz"})
    other = db.create_job("other", "forest_10000", {"client_ip": "1.2.3.9"})
    db.update_job(pub["id"], status="done")
    assert db.delete_job(pub["id"])

    with db.connect() as conn:
        rows = {r["job_id"]: dict(r) for r in conn.execute("SELECT * FROM job_origin")}
    dump = repr(rows)
    assert "1.2.3" not in dump and "x@y.cz" not in dump
    assert rows[pub["id"]]["status"] == "done"
    assert rows[pub["id"]]["email_tag"] is None
    assert rows[priv["id"]]["email_tag"]
    assert rows[pub["id"]]["ip_tag"] == rows[priv["id"]]["ip_tag"] != rows[other["id"]]["ip_tag"]
    assert rows[pub["id"]]["ip_net_tag"] == rows[other["id"]]["ip_net_tag"]

    later = datetime.now(timezone.utc) + timedelta(days=366)
    assert db.purge_job_origin(now=later, days=365) == 3
