from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest

from app.rate_limit import check_create_job, is_exempt, queue_priority_key


def _reload_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    import app.settings as settings

    importlib.reload(settings)
    import app.rate_limit as rate_limit

    importlib.reload(rate_limit)


def test_is_exempt(data_dir, monkeypatch):
    _reload_settings(monkeypatch, RATE_LIMIT_EXEMPT_IPS="1.2.3.4, 5.6.7.8")
    assert is_exempt("1.2.3.4")
    assert not is_exempt("9.9.9.9")


def test_active_jobs_limit(data_dir):
    import app.db as db

    db.init_db()
    for i in range(2):
        job = db.create_job(f"j{i}", "sprint_2m", {"client_ip": "10.0.0.1"})
        db.update_job(job["id"], status="running")
    msg = check_create_job("10.0.0.1")
    assert msg is not None
    assert "2" in msg


def test_hourly_limit(data_dir, monkeypatch):
    import app.db as db

    _reload_settings(monkeypatch, MAX_JOBS_PER_IP_HOUR="3")
    db.init_db()
    now = datetime.now(timezone.utc)
    for i in range(3):
        job = db.create_job(f"h{i}", "sprint_2m", {"client_ip": "10.0.0.2"})
        db.update_job(job["id"], status="done")
    msg = check_create_job("10.0.0.2")
    assert msg is not None
    assert "hodin" in msg.lower() or "hodinu" in msg.lower()


def test_exempt_skips_limits(data_dir, monkeypatch):
    import app.db as db

    _reload_settings(monkeypatch, RATE_LIMIT_EXEMPT_IPS="10.0.0.3", MAX_JOBS_PER_IP_HOUR="1")
    db.init_db()
    job = db.create_job("x", "sprint_2m", {"client_ip": "10.0.0.3"})
    db.update_job(job["id"], status="running")
    job2 = db.create_job("y", "sprint_2m", {"client_ip": "10.0.0.3"})
    db.update_job(job2["id"], status="queued")
    assert check_create_job("10.0.0.3") is None


def test_queue_priority_prefers_less_loaded_ip(data_dir):
    import app.db as db

    db.init_db()
    j1 = db.create_job("a", "sprint_2m", {"client_ip": "1.1.1.1"})
    db.update_job(j1["id"], status="running")
    j2 = db.create_job("b", "sprint_2m", {"client_ip": "1.1.1.1"})
    db.update_job(j2["id"], status="queued")
    j3 = db.create_job("c", "sprint_2m", {"client_ip": "2.2.2.2"})
    db.update_job(j3["id"], status="queued")
    assert queue_priority_key(j3["id"]) < queue_priority_key(j2["id"])


def test_purge_old_jobs_uses_hours(data_dir, monkeypatch):
    import app.cleanup as cleanup
    import app.db as db

    _reload_settings(monkeypatch, JOB_RETENTION_HOURS="48", JOB_RETENTION_DAYS="0")
    import app.settings as settings

    importlib.reload(settings)
    importlib.reload(cleanup)
    db.init_db()
    job = db.create_job("old", "sprint_2m", {})
    old = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    db.update_job(job["id"], status="done")
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (old, job["id"]))
        conn.commit()
    assert cleanup.purge_old_jobs() == 1
    with pytest.raises(KeyError):
        db.get_job(job["id"])


def test_api_rate_limit_429(client, monkeypatch):
    import app.db as db
    import app.main as main

    monkeypatch.setattr(main, "client_ip", lambda _req: "192.168.50.1")
    monkeypatch.setattr(
        main,
        "query_sm5_sheets",
        lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}],
    )
    monkeypatch.setattr(
        main.worker,
        "enqueue",
        lambda job_id: db.update_job(job_id, status="queued", phase="waiting"),
    )
    payload = {
        "name": "limit-test",
        "preset_id": "sprint_2m",
        "bbox": "14.40,50.08,14.42,50.09",
    }
    for i in range(2):
        r = client.post("/api/jobs", data={**payload, "name": f"limit-{i}"})
        assert r.status_code == 200, r.text
    r = client.post("/api/jobs", data={**payload, "name": "limit-3"})
    assert r.status_code == 429
