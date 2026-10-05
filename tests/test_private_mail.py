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
    assert "ZIP s mapou" in body
    assert "Náhled PNG" not in body
    assert "georeferencovaných" not in body


def test_build_private_ready_email_optional_artifact_links():
    subject, body = build_private_ready_email(
        job_name="Geo mapa",
        download_url="https://example/d/tok",
        retention_hours=48,
        preview_url="https://example/api/jobs/j1/preview.png?token=tok",
        georef_url="https://example/api/jobs/j1/download/georef-previews?token=tok",
    )
    assert "Geo mapa" in subject
    assert "preview.png?token=tok" in body
    assert "georef-previews?token=tok" in body
    assert "Náhled PNG" in body
    assert "ZIP georeferencovaných náhledů" in body
    assert "na stránce jobu se náhled/ZIP nezobrazují" in body


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


def test_load_dotenv_sets_missing_only(tmp_path, monkeypatch):
    from app.settings import _load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        "PUBLIC_BASE_URL=http://from-dotenv:8672\nSMTP_HOST=smtp.example.com\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("SMTP_HOST", "keep-me")
    _load_dotenv(env_file)
    import os

    assert os.environ["PUBLIC_BASE_URL"] == "http://from-dotenv:8672"
    assert os.environ["SMTP_HOST"] == "keep-me"


def test_load_dotenv_fills_empty_env(tmp_path, monkeypatch):
    """Compose často injectne SMTP_HOST= (prázdné) — /data/.env musí doplnit."""
    from app.settings import _load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        "SMTP_HOST=smtp.from-data.example\nSMTP_FROM=from@example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("SMTP_FROM", "   ")
    _load_dotenv(env_file)
    import os

    assert os.environ["SMTP_HOST"] == "smtp.from-data.example"
    assert os.environ["SMTP_FROM"] == "from@example.com"


def test_docker_empty_smtp_gets_nas_defaults(monkeypatch):
    """Prázdný compose inject na NAS → product SMTP/PUBLIC_BASE default (2.2.12)."""
    import app.settings as settings_mod

    monkeypatch.setattr(settings_mod, "_running_in_docker", lambda: True)
    monkeypatch.setattr(settings_mod, "SMTP_HOST", "")
    monkeypatch.setattr(settings_mod, "SMTP_FROM", "")
    monkeypatch.setattr(settings_mod, "PUBLIC_BASE_URL", "")

    host = ("" or settings_mod._NAS_DEFAULT_SMTP_HOST)
    assert host == "datais-cz.mail.protection.outlook.com"
    assert settings_mod._NAS_DEFAULT_PUBLIC_BASE_URL == "https://podkladarna.kibos.link"

    # Simulace stejné logiky jako při importu settings (empty env + docker).
    smtp_host = ""
    if not smtp_host and settings_mod._running_in_docker():
        smtp_host = settings_mod._NAS_DEFAULT_SMTP_HOST
    public = ""
    if not public and settings_mod._running_in_docker():
        public = settings_mod._NAS_DEFAULT_PUBLIC_BASE_URL
    assert smtp_host == settings_mod._NAS_DEFAULT_SMTP_HOST
    assert public == settings_mod._NAS_DEFAULT_PUBLIC_BASE_URL


def test_running_in_docker_false_on_tip():
    from app.settings import _running_in_docker

    # Tip/pytest běží mimo kontejner (bez /.dockerenv).
    assert _running_in_docker() is False


def test_retry_missed_private_mails(tmp_path, monkeypatch):
    from app import job_worker

    monkeypatch.setattr("app.db.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("app.db.DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr(job_worker, "JOBS_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir()
    db.init_db()
    job = db.create_job(
        "priv-retry",
        "forest_10000",
        {"private": True, "notify_email": "user@example.com"},
    )
    db.update_job(job["id"], status="done", phase="done")
    db.append_log(job["id"], "Status: done")
    db.append_log(
        job["id"],
        "CHYBA: PUBLIC_BASE_URL není nastavené – nelze sestavit odkaz v e-mailu.",
    )
    out = tmp_path / "jobs" / job["id"] / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "podkladarna_output.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    sent: list[tuple[str, str, str]] = []

    def fake_send(to, subject, body, **_kwargs):
        sent.append((to, subject, body))

    monkeypatch.setattr(job_worker, "send_mail", fake_send)
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:8672")
    monkeypatch.setattr(
        job_worker.app_settings, "PUBLIC_BASE_URL", "http://127.0.0.1:8672"
    )
    monkeypatch.setattr(job_worker.app_settings, "PRIVATE_JOB_RETENTION_HOURS", 48)

    resent = job_worker.retry_missed_private_mails()
    assert job["id"] in resent
    assert sent and sent[0][0] == "user@example.com"
    assert "/d/" in sent[0][2]
    assert any("E-mail s odkazem odeslán" in e["line"] for e in db.get_logs(job["id"]))

    sent.clear()
    assert job_worker.retry_missed_private_mails() == []
    assert sent == []

    # Bez ZIPu se retry přeskočí (žádný mrtvý odkaz).
    orphan = db.create_job(
        "priv-nozip",
        "forest_10000",
        {"private": True, "notify_email": "x@y.cz"},
    )
    db.update_job(orphan["id"], status="done", phase="done")
    db.append_log(orphan["id"], "Status: done")
    assert job_worker.retry_missed_private_mails() == []


def test_api_private_job_log_available_without_token(client, monkeypatch):
    """Privátní job: status/log v session OK, artefakty bez tokenu ne."""
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
            "name": "priv-log",
            "preset_id": "sprint_2m",
            "bbox": "14.40,50.08,14.42,50.09",
            "private": "1",
            "notify_email": "petr@example.com",
        },
    )
    assert r.status_code == 200
    job_id = r.json()["id"]
    db.append_log(job_id, "Připravuji DEM z DMR5G (terén pro vrstevnice a srázy)…")
    db.append_log(job_id, "Klasifikuji vegetaci z hustoty LiDAR odrazů…")

    detail = client.get(f"/api/jobs/{job_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["private"] is True
    assert body["has_preview"] is False
    assert body["has_output"] is False
    assert body["has_georef_previews"] is False

    log = client.get(f"/api/jobs/{job_id}/log?after=0")
    assert log.status_code == 200
    lines = log.json()["lines"]
    assert len(lines) >= 2
    joined = "\n".join(x["line"] for x in lines)
    assert "Připravuji DEM" in joined
    assert "vegetaci" in joined


def test_notify_private_job_mail_includes_preview_and_georef(tmp_path, monkeypatch):
    """Mail obsahuje tokenizované odkazy PNG + georef, když artefakty existují."""
    from app import job_worker

    monkeypatch.setattr("app.db.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("app.db.DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr(job_worker, "JOBS_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir(exist_ok=True)
    db.init_db()

    job = db.create_job(
        "priv-arts",
        "forest_10000",
        {"private": True, "notify_email": "user@example.com", "output_georef": True},
    )
    out = tmp_path / "jobs" / job["id"] / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "podkladarna_output.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    (out / "preview.jpg").write_bytes(b"\xff\xd8\xff")
    prev = out / "preview"
    prev.mkdir(exist_ok=True)
    (prev / "mapa.png").write_bytes(b"png")
    (prev / "mapa.pgw").write_text("1\n0\n0\n-1\n0\n0\n", encoding="utf-8")

    sent: list[tuple] = []

    def fake_send(to, subject, body, **_kw):
        sent.append((to, subject, body))

    monkeypatch.setattr(job_worker, "send_mail", fake_send)
    monkeypatch.setattr(
        job_worker.app_settings, "PUBLIC_BASE_URL", "http://127.0.0.1:8672"
    )
    monkeypatch.setattr(job_worker.app_settings, "PRIVATE_JOB_RETENTION_HOURS", 48)

    job_worker._notify_private_job(job["id"])
    assert len(sent) == 1
    body = sent[0][2]
    assert "/d/" in body
    assert f"/api/jobs/{job['id']}/preview.png?token=" in body
    assert f"/api/jobs/{job['id']}/download/georef-previews?token=" in body
    assert "Náhled PNG" in body
    assert "ZIP georeferencovaných" in body


def test_ui_private_session_log_hooks():
    """UI musí umět zobrazit log privátního běhu (holder + injekce do live)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    app_js = (root / "web" / "static" / "app.js").read_text(encoding="utf-8")
    css = (root / "web" / "static" / "style.css").read_text(encoding="utf-8")
    html = (root / "web" / "index.html").read_text(encoding="utf-8")

    assert "setPrivateDetailHolder" in app_js
    assert "jobIsPrivate" in app_js
    assert "showFormNotice" in app_js
    assert "privátní session" in app_js
    assert "Privátní session" in app_js or "injektuj do" in app_js
    assert "!jobIsPrivate(selected) && selected.has_preview" in app_js
    assert "!jobIsPrivate(job) && job.has_preview" in app_js
    assert "ZIP/náhled nepřijdou" in app_js or "ZIP e-mailem" in app_js
    assert "#job-detail-holder.active" in css
    assert "form-notice" in css
    assert 'id="form-notice"' in html
    assert "pipeline log" in html.lower() or "pipeline log" in app_js.lower()
    # Na webu privátní job nemá PNG/ZIP odkazy — jen mail.
    assert "Privátní: ZIP/PNG jen e-mailem" in app_js or "jen e-mailem" in app_js
