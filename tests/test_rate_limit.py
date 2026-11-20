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
    # Různé bboxy – stejný výřez by deduplikace vrátila existující job (200).
    bboxes = (
        "14.40,50.08,14.42,50.09",
        "14.41,50.08,14.43,50.09",
        "14.42,50.08,14.44,50.09",
    )
    for i, bbox in enumerate(bboxes[:2]):
        r = client.post(
            "/api/jobs",
            data={
                "name": f"limit-{i}",
                "preset_id": "sprint_2m",
                "bbox": bbox,
            },
        )
        assert r.status_code == 200, r.text
    r = client.post(
        "/api/jobs",
        data={
            "name": "limit-3",
            "preset_id": "sprint_2m",
            "bbox": bboxes[2],
        },
    )
    assert r.status_code == 429


def test_tile_fetch_limit_per_ip_cached_tiles_free(client, monkeypatch, tmp_path):
    """/tiles (revize 2026-11-16): dlaždice mimo cache nejvýš TILE_FETCH_PER_IP_MIN za minutu na IP, z cache bez limitu."""
    import app.main as main
    import app.settings as settings
    import app.tiles as tiles

    monkeypatch.setattr(tiles, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(settings, "TILE_FETCH_PER_IP_MIN", 2)
    monkeypatch.setattr(settings, "TILE_FETCH_PER_MIN", 100)
    tiles.reset_fetch_limits()
    png = tmp_path / "t.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(100))
    monkeypatch.setattr(main, "fetch_tile", lambda z, x, y: png)  # stažení bez uložení do cache
    ip = {"x": "10.0.0.1"}
    monkeypatch.setattr(main, "client_ip", lambda _req: ip["x"])

    assert [client.get(f"/tiles/10/{x}/300.png").status_code for x in range(3)] == [200, 200, 429]
    cached = tiles.tile_cache_path(10, 9, 300)
    cached.parent.mkdir(parents=True)
    cached.write_bytes(png.read_bytes())
    assert client.get("/tiles/10/9/300.png").status_code == 200  # z cache i po limitu
    ip["x"] = "10.0.0.2"
    assert client.get("/tiles/10/5/300.png").status_code == 200  # jiná IP má vlastní limit
    assert client.get("/tiles/30/5/300.png").status_code == 400  # neplatná dlaždice jako dřív


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("Jan.Novak+mapy@GMail.com", "jannovak@gmail.com"),
        ("jan.novak@googlemail.com", "jannovak@gmail.com"),
        ("jan.novak+x@example.cz", "jan.novak@example.cz"),
        ("  Petr@Example.org ", "petr@example.org"),
    ],
)
def test_normalize_email_for_limit(raw, normalized):
    import app.db as db

    assert db.normalize_email_for_limit(raw) == normalized


def test_private_email_daily_limit(data_dir, monkeypatch):
    import app.db as db

    _reload_settings(monkeypatch, MAX_PRIVATE_JOBS_PER_EMAIL_DAY="3")
    from app.rate_limit import check_private_email

    db.init_db()
    opts = {"private": True, "client_ip": "10.0.1.1"}
    # Aliasy téže schránky se sčítají; API, ukázka, objednávka a veřejný job ne.
    for email in ("tester@gmail.com", "Tes.ter+a@gmail.com"):
        db.create_job("p", "sprint_2m", {**opts, "notify_email": email})
    db.create_job("api", "sprint_2m", {**opts, "api_client": True, "notify_email": "tester@gmail.com"})
    db.create_job("s", "sprint_2m", {**opts, "sample": True, "notify_email": "tester@gmail.com"})
    db.create_job("o", "sprint_2m", {**opts, "order_id": "ord1", "notify_email": "tester@gmail.com"})
    db.create_job("pub", "sprint_2m", {"client_ip": "10.0.1.1", "notify_email": "tester@gmail.com"})
    assert check_private_email("t.e.s.t.e.r@gmail.com", "10.0.1.1") is None
    db.create_job("p", "sprint_2m", {**opts, "notify_email": "tester+c@googlemail.com"})
    msg = check_private_email("tester@gmail.com", "10.0.1.1")
    assert msg is not None and msg.key == "api.rate.email_daily"
    assert msg.params["hours"] == 23 or msg.params["hours"] == 24
    assert check_private_email("jiny@example.cz", "10.0.1.1") is None
    # Joby starší než 24 h z okna vypadnou.
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    with db.connect() as conn:
        conn.execute("UPDATE job_origin SET created_at = ?", (old,))
        conn.commit()
    assert check_private_email("tester@gmail.com", "10.0.1.1") is None


def test_private_email_limit_off_and_exempt(data_dir, monkeypatch):
    import app.db as db

    _reload_settings(monkeypatch, MAX_PRIVATE_JOBS_PER_EMAIL_DAY="1", RATE_LIMIT_EXEMPT_IPS="10.0.1.9")
    from app.rate_limit import check_private_email

    db.init_db()
    db.create_job("p", "sprint_2m", {"private": True, "notify_email": "a@example.cz"})
    assert check_private_email("a@example.cz", "10.0.1.2") is not None
    assert check_private_email("a@example.cz", "10.0.1.9") is None
    _reload_settings(monkeypatch, MAX_PRIVATE_JOBS_PER_EMAIL_DAY="0")
    from app.rate_limit import check_private_email as off

    assert off("a@example.cz", "10.0.1.2") is None


def test_api_private_email_limit_429(client, monkeypatch):
    import app.db as db
    import app.main as main
    import app.rate_limit as rate_limit

    monkeypatch.setattr(main, "client_ip", lambda _req: "192.168.51.1")
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setattr(rate_limit, "MAX_PRIVATE_JOBS_PER_EMAIL_DAY", 1)
    monkeypatch.setattr(main, "query_sm5_sheets", lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}])
    monkeypatch.setattr(main.worker, "enqueue", lambda job_id: db.update_job(job_id, status="queued", phase="waiting"))
    data = {"name": "p", "preset_id": "sprint_2m", "private": "1", "notify_email": "tester@example.cz"}
    r = client.post("/api/jobs", data={**data, "bbox": "14.40,50.08,14.42,50.09"})
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/jobs?lang=en",
        data={**data, "notify_email": "Tester+2@example.cz", "bbox": "14.41,50.08,14.43,50.09"},
    )
    assert r.status_code == 429
    assert "private jobs are limited to 1 per 24 hours" in r.json()["detail"]
    # Veřejný job téhož uživatele limit e-mailu neomezuje.
    r = client.post("/api/jobs", data={"name": "pub", "preset_id": "sprint_2m", "bbox": "14.42,50.08,14.44,50.09"})
    assert r.status_code == 200, r.text


def test_banned_email(data_dir, monkeypatch):
    _reload_settings(monkeypatch, BANNED_EMAILS=" Spam.Mer@gmail.com , x@example.cz", RATE_LIMIT_EXEMPT_IPS="10.0.2.1")
    from app.rate_limit import check_banned_email, is_banned_email

    assert is_banned_email("spammer+1@googlemail.com")
    assert is_banned_email("X@Example.cz")
    assert not is_banned_email("y@example.cz")
    msg = check_banned_email("spammer@gmail.com")
    assert msg is not None and msg.key == "api.rate.email_banned"
    assert check_banned_email("") is None


def test_api_banned_email_403(client, monkeypatch):
    import app.db as db
    import app.main as main
    import app.rate_limit as rate_limit

    monkeypatch.setattr(main, "client_ip", lambda _req: "192.168.52.1")
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setattr(rate_limit, "BANNED_EMAILS", "blocked@example.cz")
    monkeypatch.setattr(main, "query_sm5_sheets", lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}])
    monkeypatch.setattr(main.worker, "enqueue", lambda job_id: db.update_job(job_id, status="queued", phase="waiting"))
    data = {"name": "p", "preset_id": "sprint_2m", "private": "1", "bbox": "14.40,50.08,14.42,50.09"}
    r = client.post("/api/jobs", data={**data, "notify_email": "Blocked+x@example.cz"})
    assert r.status_code == 403
    assert "fér" in r.json()["detail"]
    r = client.post("/api/jobs", data={**data, "notify_email": "ok@example.cz"})
    assert r.status_code == 200, r.text


def test_origin_stats_emails_without_plain_address(data_dir, capsys):
    import app.db as db
    from app import origin_stats

    db.init_db()
    for email in ("heavy@example.cz", "heavy+1@example.cz", "light@example.cz"):
        db.create_job("p", "sprint_2m", {"private": True, "client_ip": "10.0.3.1", "notify_email": email})
    origin_stats.main(["--emails"])
    out = capsys.readouterr().out
    assert "@" not in out and db.email_limit_tag("heavy@example.cz") in out
    origin_stats.main(["--email", "HEAVY@example.cz"])
    out = capsys.readouterr().out
    assert "24 h 2, 30 dní 2, celkem 2" in out and "heavy" not in out


def test_private_email_limit_default_8(data_dir, monkeypatch):
    import app.db as db

    monkeypatch.delenv("MAX_PRIVATE_JOBS_PER_EMAIL_DAY", raising=False)
    _reload_settings(monkeypatch)
    from app.rate_limit import check_private_email

    db.init_db()
    for _ in range(7):
        db.create_job("p", "sprint_2m", {"private": True, "notify_email": "d@example.cz"})
    assert check_private_email("d@example.cz", "10.0.4.1") is None
    db.create_job("p", "sprint_2m", {"private": True, "notify_email": "d@example.cz"})
    msg = check_private_email("d@example.cz", "10.0.4.1")
    assert msg is not None and msg.params["limit"] == 8 and "omezen na 8 za 24 hodin" in msg
