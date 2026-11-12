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


def test_usage_totals_count_once_survive_purge_and_catch_up(tmp_path, monkeypatch):
    """Souhrn použití: job se započítá jednou (i po restartu), stažení, přežije úklid jobu i původu."""
    import json

    jobs = tmp_path / "jobs"
    monkeypatch.setattr("app.cleanup.JOBS_DIR", jobs)
    monkeypatch.setattr("app.db.JOBS_DIR", jobs)
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "t.db")
    jobs.mkdir()
    db.init_db()

    a = db.create_job("a", "forest_10000", {"map_types": ["les", "mtbo"]})
    stats = {"total_seconds": 600, "output_zip_bytes": 5_000_000, "downloaded_bytes": 1e9,
             "cached_bytes": 2e9, "maps": [{"name": "les"}, {"name": "mtbo"}]}
    db.update_job(a["id"], status="done")
    assert db.record_job_usage(a["id"], "done", stats)
    assert not db.record_job_usage(a["id"], "done", stats)  # podruhé nic
    db.record_download(a["id"], 5_000_000)

    # Job dokončený bez zápisu (starší image): započítá se při startu z build_stats.json.
    b = db.create_job("b", "sprint_4000", {"map_types": ["sprint"]})
    db.update_job(b["id"], status="failed")
    (jobs / b["id"] / "work").mkdir(parents=True, exist_ok=True)
    (jobs / b["id"] / "work" / "build_stats.json").write_text(json.dumps({"total_seconds": 60}))
    running = db.create_job("c", "sprint_4000", {})
    db.update_job(running["id"], status="running")
    db.init_db()
    db.init_db()

    assert db.delete_job(a["id"]) and db.delete_job(b["id"])
    later = datetime.now(timezone.utc) + timedelta(days=400)
    assert db.purge_job_origin(now=later, days=365) == 3
    t = {k: v["value"] for k, v in db.usage_totals().items()}
    assert t == {"jobs_done": 1, "maps_done": 2, "jobs_failed": 1, "build_seconds": 660,
                 "output_bytes": 5e6, "source_bytes": 3e9, "download_count": 1, "download_bytes": 5e6,
                 "build_seconds_backfilled": 0}


def test_usage_build_seconds_whole_run_and_backfill(tmp_path, monkeypatch):
    """Doba zpracování: bez build_stats.json celá doba běhu z jobs; dřív započítané bez času jednou doplnit."""
    jobs = tmp_path / "jobs"
    monkeypatch.setattr("app.db.JOBS_DIR", jobs)
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "t.db")
    jobs.mkdir()
    db.init_db()
    t0 = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)

    timed = db.create_job("a", "sprint_4000", {"map_types": ["sprint"]})
    db.update_job(timed["id"], status="done")
    assert db.record_job_usage(timed["id"], "done", {"total_seconds": 1200})
    # Timeout: build_stats.json chybí, čas z jobs (start → konec).
    killed = db.create_job("b", "sprint_4000", {"map_types": ["sprint"]})
    db.update_job(killed["id"], status="failed")
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET started_at = ?, updated_at = ? WHERE id = ?",
                     (t0.isoformat(), (t0 + timedelta(minutes=90)).isoformat(), killed["id"]))
        conn.commit()
    assert db.record_job_usage(killed["id"], "failed", None)
    # Starý job započítaný s nulou a už smazaný: odhad = medián presetu.
    old = db.create_job("c", "sprint_4000", {"map_types": ["sprint"]})
    db.update_job(old["id"], status="done")
    with db.connect() as conn:
        conn.execute("UPDATE job_origin SET usage_counted = 1, status = 'done' WHERE job_id = ?",
                     (old["id"],))
        conn.execute("DELETE FROM usage_totals WHERE key = 'build_seconds_backfilled'")
        conn.commit()
    assert db.delete_job(old["id"])
    db.init_db()
    db.init_db()  # podruhé nic

    t = {k: v["value"] for k, v in db.usage_totals().items()}
    assert t["build_seconds"] == 1200 + 5400 + (1200 + 5400) / 2
    assert t["build_seconds_backfilled"] == 1
