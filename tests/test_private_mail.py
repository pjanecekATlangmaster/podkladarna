from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest

from app import db
from app.mail import MailError, build_private_ready_email, send_mail


def test_new_download_token_opaque():
    a = db.new_download_token()
    b = db.new_download_token()
    assert len(a) >= 32
    assert a != b
    assert "/" not in a


def test_private_job_creates_token_and_hides_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("app.db.DOWNLOADS_DIR", tmp_path / "dl")
    (tmp_path / "jobs").mkdir()
    db.init_db()

    job = db.create_job(
        "priv",
        "forest_10000",
        {"private": True, "notify_email": "user@example.com"},
    )
    assert job["private"] is True
    assert job["options"]["notify_email"] == "user@example.com"
    assert "download_token" not in job
    assert job["has_output"] is False
    assert job["has_preview"] is False
    assert job.get("expires_at")

    with db.connect() as conn:
        row = conn.execute(
            "SELECT download_token FROM jobs WHERE id = ?",
            (job["id"],),
        ).fetchone()
    token = row["download_token"]
    assert token
    assert db.token_matches({"download_token": token}, token)
    assert not db.token_matches({"download_token": token}, "wrong")

    by_tok = db.get_job_by_download_token(token)
    assert by_tok["id"] == job["id"]
    assert by_tok["download_token"] == token


def test_list_jobs_hides_private(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("app.db.DOWNLOADS_DIR", tmp_path / "dl")
    (tmp_path / "jobs").mkdir()
    db.init_db()
    pub = db.create_job("pub", "forest_10000", {})
    priv = db.create_job(
        "priv",
        "forest_10000",
        {"private": True, "notify_email": "a@b.cz"},
    )
    public_ids = {j["id"] for j in db.list_jobs(include_private=False)}
    assert pub["id"] in public_ids
    assert priv["id"] not in public_ids
    all_ids = {j["id"] for j in db.list_jobs(include_private=True)}
    assert priv["id"] in all_ids


def test_job_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("app.db.DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr("app.db.PRIVATE_JOB_RETENTION_HOURS", 48)
    (tmp_path / "jobs").mkdir()
    db.init_db()
    job = db.create_job(
        "priv",
        "forest_10000",
        {"private": True, "notify_email": "a@b.cz"},
    )
    assert not db.job_is_expired(job)
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?",
            (old, job["id"]),
        )
        conn.commit()
    job2 = db.get_job(job["id"])
    assert db.job_is_expired(job2)


def test_send_mail_starttls_mock(monkeypatch):
    monkeypatch.setattr("app.mail.settings.SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr("app.mail.settings.SMTP_PORT", 25)
    monkeypatch.setattr("app.mail.settings.SMTP_ENCRYPTION", "starttls")
    monkeypatch.setattr("app.mail.settings.SMTP_USER", "")
    monkeypatch.setattr("app.mail.settings.SMTP_PASSWORD", "")
    monkeypatch.setattr("app.mail.settings.SMTP_FROM", "from@example.com")
    monkeypatch.setattr("app.mail.settings.SMTP_FROM_NAME", "Test")

    calls: dict = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls["host"] = host
            calls["port"] = port
            calls["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            calls.setdefault("ehlo", 0)
            calls["ehlo"] += 1

        def starttls(self, context=None):
            calls["starttls"] = True

        def login(self, user, password):
            calls["login"] = (user, password)

        def send_message(self, msg: EmailMessage):
            calls["to"] = msg["To"]
            calls["subject"] = msg["Subject"]
            calls["body"] = msg.get_content()

    monkeypatch.setattr("app.mail.smtplib.SMTP", FakeSMTP)
    send_mail("user@example.com", "Předmět", "Tělo e-mailu")
    assert calls["host"] == "smtp.example.com"
    assert calls["port"] == 25
    assert calls["starttls"] is True
    assert "login" not in calls
    assert calls["to"] == "user@example.com"
    assert calls["subject"] == "Předmět"
    assert "Tělo" in calls["body"]


def test_send_mail_requires_config(monkeypatch):
    monkeypatch.setattr("app.mail.settings.SMTP_HOST", "")
    monkeypatch.setattr("app.mail.settings.SMTP_FROM", "from@example.com")
    with pytest.raises(MailError, match="SMTP není nastavené"):
        send_mail("a@b.cz", "x", "y")


def test_build_private_ready_email_mentions_expiry():
    subject, body = build_private_ready_email(
        job_name="Test mapa",
        download_url="https://example/d/abc",
        retention_hours=48,
    )
    assert "Test mapa" in subject
    assert "https://example/d/abc" in body
    assert "48" in body


def test_api_private_job_requires_email(client, monkeypatch):
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
            "name": "priv-no-mail",
            "preset_id": "sprint_2m",
            "bbox": "14.40,50.08,14.42,50.09",
            "private": "1",
        },
    )
    assert r.status_code == 400
    assert "e-mail" in r.json()["detail"].lower() or "email" in r.json()["detail"].lower()


def test_api_private_hidden_from_list_and_token_download(client, monkeypatch, data_dir):
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
            "name": "priv-ok",
            "preset_id": "sprint_2m",
            "bbox": "14.40,50.08,14.42,50.09",
            "private": "1",
            "notify_email": "petr@example.com",
        },
    )
    assert r.status_code == 200
    job = r.json()
    assert job["private"] is True
    assert job["has_output"] is False
    job_id = job["id"]

    listed = client.get("/api/jobs").json()["jobs"]
    assert all(j["id"] != job_id for j in listed)

    # Artefakt na disku
    out = Path(data_dir) / "jobs" / job_id / "output"
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / "podkladarna_output.zip"
    zip_path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    assert client.get(f"/api/jobs/{job_id}/download").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/preview.png").status_code == 404

    with db.connect() as conn:
        token = conn.execute(
            "SELECT download_token FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()["download_token"]

    ok = client.get(f"/api/jobs/{job_id}/download?token={token}")
    assert ok.status_code == 200
    by_d = client.get(f"/d/{token}")
    assert by_d.status_code == 200


def test_purge_expired_private(tmp_path, monkeypatch):
    from app.cleanup import purge_old_jobs

    monkeypatch.setattr("app.cleanup.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("app.db.DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr("app.db.PRIVATE_JOB_RETENTION_HOURS", 48)
    (tmp_path / "jobs").mkdir()
    db.init_db()

    old = db.create_job(
        "old-priv",
        "forest_10000",
        {"private": True, "notify_email": "a@b.cz"},
    )
    db.update_job(old["id"], status="done")
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET created_at = ?, updated_at = ? WHERE id = ?",
            (old_ts, old_ts, old["id"]),
        )
        conn.commit()

    removed = purge_old_jobs(retention_hours=0)
    assert removed >= 1
    assert old["id"] not in {j["id"] for j in db.list_jobs(include_private=True)}
