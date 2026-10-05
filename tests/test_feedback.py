from __future__ import annotations

import pytest

from app import db
from app.feedback import (
    build_feedback_email,
    check_feedback_rate,
    job_page_url,
    reset_feedback_rate_limits,
    send_feedback,
)
from app.mail import MailError


@pytest.fixture(autouse=True)
def _clear_feedback_limits():
    reset_feedback_rate_limits()
    yield
    reset_feedback_rate_limits()


def _done_job(name: str = "Mapa") -> dict:
    job = db.create_job(name, "forest_10000", {})
    db.update_job(job["id"], status="done", phase="done")
    return db.get_job(job["id"])


def _running_job(name: str = "Běží") -> dict:
    job = db.create_job(name, "forest_10000", {})
    db.update_job(job["id"], status="running", phase="vectors")
    return db.get_job(job["id"])


def test_build_feedback_email(monkeypatch):
    monkeypatch.setattr(
        "app.feedback.settings.PUBLIC_BASE_URL",
        "https://podkladarna.example",
    )
    subject, body = build_feedback_email(
        contact="petr@example.com",
        comment="Srázy u Barrandova chybí.",
        job_id="abc123",
        client_ip="1.2.3.4",
        job_name="Barrandov",
        job_status="running",
    )
    assert "zpětná vazba" in subject
    assert "abc123" in subject
    assert "petr@example.com" in body
    assert "Srázy u Barrandova chybí." in body
    assert "Job ID: abc123" in body
    assert "Název: Barrandov" in body
    assert "Stav jobu: running" in body
    assert "https://podkladarna.example/api/jobs/abc123" in body
    assert "IP: 1.2.3.4" in body
    assert "v příloze" not in body

    _, body_att = build_feedback_email(
        contact="a@b.cz",
        comment="x",
        job_id="j1",
        preview_attached=True,
    )
    assert "Náhled PNG: v příloze tohoto e-mailu." in body_att


def test_job_page_url(monkeypatch):
    monkeypatch.setattr("app.feedback.settings.PUBLIC_BASE_URL", "https://x.test/")
    assert job_page_url("j1") == "https://x.test/api/jobs/j1"
    monkeypatch.setattr("app.feedback.settings.PUBLIC_BASE_URL", "")
    assert job_page_url("j1") is None


def test_feedback_api_sends_mail(client, monkeypatch):
    import app.main as main

    job = _done_job("PNG OK")
    sent = {}

    def fake_send(**kwargs):
        sent.update(kwargs)

    monkeypatch.setattr(main, "send_feedback", fake_send)
    monkeypatch.setattr("app.feedback.settings.FEEDBACK_TO", "janecek@datais.cz")

    r = client.post(
        "/api/feedback",
        json={
            "contact": "mapar@example.com",
            "comment": "PNG vypadá dobře, díky.",
            "job_id": job["id"],
        },
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert sent["contact"] == "mapar@example.com"
    assert sent["comment"] == "PNG vypadá dobře, díky."
    assert sent["job_id"] == job["id"]
    assert sent["job_name"] == "PNG OK"
    assert sent["job_status"] == "done"
    assert sent["client_ip"]


def test_feedback_accepts_running_job(client, monkeypatch):
    """Petr: odeslat jde i při běžícím jobu."""
    import app.main as main

    job = _running_job("Ještě běží")
    sent = {}
    monkeypatch.setattr(main, "send_feedback", lambda **kw: sent.update(kw))
    r = client.post(
        "/api/feedback",
        json={
            "contact": "a@b.cz",
            "comment": "Už teď vypadá vegetace hustě.",
            "job_id": job["id"],
        },
    )
    assert r.status_code == 200
    assert sent["job_id"] == job["id"]
    assert sent["job_status"] == "running"


def test_feedback_api_form_post(client, monkeypatch):
    import app.main as main

    job = _done_job("Form")
    sent = {}
    monkeypatch.setattr(main, "send_feedback", lambda **kw: sent.update(kw))
    r = client.post(
        "/api/feedback",
        data={
            "contact": "+420733575541",
            "comment": "Chybí lavičky u rybníka.",
            "job_id": job["id"],
        },
    )
    assert r.status_code == 200
    assert sent["contact"] == "+420733575541"
    assert "lavičky" in sent["comment"]
    assert sent["job_id"] == job["id"]


def test_feedback_honeypot_skips_send(client, monkeypatch):
    import app.main as main

    called = {"n": 0}

    def boom(**_kwargs):
        called["n"] += 1
        raise AssertionError("honeypot must not send")

    monkeypatch.setattr(main, "send_feedback", boom)
    r = client.post(
        "/api/feedback",
        json={
            "contact": "bot@spam.test",
            "comment": "buy now",
            "job_id": "whatever",
            "website": "https://spam.example",
        },
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert called["n"] == 0


def test_feedback_requires_fields(client, monkeypatch):
    import app.main as main

    job = _done_job()
    monkeypatch.setattr(main, "send_feedback", lambda **_k: None)
    r = client.post(
        "/api/feedback",
        json={"contact": "", "comment": "x", "job_id": job["id"]},
    )
    assert r.status_code == 400
    r2 = client.post(
        "/api/feedback",
        json={"contact": "a@b.cz", "comment": "", "job_id": job["id"]},
    )
    assert r2.status_code == 400
    r3 = client.post(
        "/api/feedback",
        json={"contact": "a@b.cz", "comment": "bez jobu"},
    )
    assert r3.status_code == 400
    assert "job_id" in r3.json()["detail"]


def test_feedback_unknown_job_404(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "send_feedback", lambda **_k: None)
    r = client.post(
        "/api/feedback",
        json={
            "contact": "a@b.cz",
            "comment": "ghost",
            "job_id": "no_such_job_zzz",
        },
    )
    assert r.status_code == 404


def test_feedback_rate_limit(client, monkeypatch):
    import app.main as main

    job = _done_job()
    monkeypatch.setattr(main, "send_feedback", lambda **_k: None)
    monkeypatch.setattr("app.feedback.settings.MAX_FEEDBACK_PER_IP_HOUR", 2)
    monkeypatch.setattr("app.settings.MAX_FEEDBACK_PER_IP_HOUR", 2)

    for _ in range(2):
        ok = client.post(
            "/api/feedback",
            json={"contact": "a@b.cz", "comment": "ok", "job_id": job["id"]},
        )
        assert ok.status_code == 200

    blocked = client.post(
        "/api/feedback",
        json={"contact": "a@b.cz", "comment": "spam", "job_id": job["id"]},
    )
    assert blocked.status_code == 429
    assert "hodinu" in blocked.json()["detail"]


def test_feedback_smtp_error_502_not_queue_503(client, monkeypatch):
    """Mail fail nesmí být 503 (FE to dřív maskovalo jako plnou frontu jobů)."""
    import app.main as main

    job = _done_job()

    def fail(**_kwargs):
        raise MailError("SMTP není nastavené")

    monkeypatch.setattr(main, "send_feedback", fail)
    r = client.post(
        "/api/feedback",
        json={"contact": "a@b.cz", "comment": "test", "job_id": job["id"]},
    )
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "nešlo odeslat" in detail
    assert "SMTP není nastavené" in detail
    assert "fronta" not in detail.lower()


def test_feedback_ignores_job_queue_busy(client, monkeypatch):
    """Feedback nesmí záviset na worker.is_busy / frontě generování."""
    import app.main as main

    job = _running_job("busy-ok")
    sent = {}
    monkeypatch.setattr(main, "send_feedback", lambda **kw: sent.update(kw))
    monkeypatch.setattr(main.worker, "is_busy", lambda: True)
    monkeypatch.setattr(main.worker, "can_accept_job", lambda: False)
    r = client.post(
        "/api/feedback",
        json={
            "contact": "a@b.cz",
            "comment": "I při plné frontě.",
            "job_id": job["id"],
        },
    )
    assert r.status_code == 200
    assert sent["job_id"] == job["id"]


def test_send_feedback_uses_feedback_to(monkeypatch):
    captured = {}

    def fake_send_mail(to, subject, body, **kw):
        captured["to"] = to
        captured["subject"] = subject
        captured["body"] = body
        captured["attachments"] = kw.get("attachments")

    monkeypatch.setattr("app.feedback.settings.FEEDBACK_TO", "owner@example.com")
    monkeypatch.setattr(
        "app.feedback.settings.PUBLIC_BASE_URL",
        "https://podkladarna.example",
    )
    monkeypatch.setattr("app.feedback.settings.SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr("app.feedback.settings.SMTP_FROM", "from@example.com")
    monkeypatch.setattr("app.feedback.send_mail", fake_send_mail)
    monkeypatch.setattr("app.feedback._job_preview_attachment", lambda _jid: None)

    send_feedback(
        contact="u@x.cz",
        comment="Ahoj",
        job_id="j1",
        job_status="queued",
    )
    assert captured["to"] == "owner@example.com"
    assert "j1" in captured["subject"]
    assert "u@x.cz" in captured["body"]
    assert "https://podkladarna.example/api/jobs/j1" in captured["body"]
    assert "queued" in captured["body"]
    assert captured["attachments"] is None


def test_send_feedback_attaches_preview(tmp_path, monkeypatch):
    """Feedback mail přiloží náhled PNG, když job má preview na disku."""
    from pathlib import Path

    import app.feedback as feedback_mod

    monkeypatch.setattr("app.db.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("app.db.DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr(feedback_mod, "JOBS_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir(exist_ok=True)
    db.init_db()

    job = db.create_job("s preview", "forest_10000", {})
    out = tmp_path / "jobs" / job["id"] / "output"
    out.mkdir(parents=True, exist_ok=True)
    png = out / "preview.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 12)

    captured: dict = {}

    def fake_send_mail(to, subject, body, **kw):
        captured["to"] = to
        captured["body"] = body
        captured["attachments"] = kw.get("attachments")

    monkeypatch.setattr("app.feedback.settings.FEEDBACK_TO", "owner@example.com")
    monkeypatch.setattr("app.feedback.settings.PUBLIC_BASE_URL", "")
    monkeypatch.setattr("app.feedback.send_mail", fake_send_mail)

    send_feedback(contact="u@x.cz", comment="Podívej", job_id=job["id"])
    assert "v příloze tohoto e-mailu" in captured["body"]
    atts = captured["attachments"] or []
    assert len(atts) == 1
    assert Path(atts[0]).name == "preview.png"
    assert Path(atts[0]).read_bytes().startswith(b"\x89PNG")


def test_check_feedback_rate_exempt(monkeypatch):
    monkeypatch.setattr("app.feedback.settings.MAX_FEEDBACK_PER_IP_HOUR", 1)
    monkeypatch.setattr("app.rate_limit.RATE_LIMIT_EXEMPT_IPS", frozenset({"9.9.9.9"}))
    assert check_feedback_rate("9.9.9.9") is None
    assert check_feedback_rate("9.9.9.9") is None
