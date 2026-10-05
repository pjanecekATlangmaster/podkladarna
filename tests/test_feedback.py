from __future__ import annotations

import pytest

from app.feedback import (
    build_feedback_email,
    check_feedback_rate,
    reset_feedback_rate_limits,
    send_feedback,
)
from app.mail import MailError


@pytest.fixture(autouse=True)
def _clear_feedback_limits():
    reset_feedback_rate_limits()
    yield
    reset_feedback_rate_limits()


def test_build_feedback_email():
    subject, body = build_feedback_email(
        contact="petr@example.com",
        comment="Srázy u Barrandova chybí.",
        job_id="abc123",
        client_ip="1.2.3.4",
    )
    assert "zpětná vazba" in subject
    assert "abc123" in subject
    assert "petr@example.com" in body
    assert "Srázy u Barrandova chybí." in body
    assert "Job ID: abc123" in body
    assert "IP: 1.2.3.4" in body


def test_feedback_api_sends_mail(client, monkeypatch):
    import app.main as main

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
            "job_id": "job_42",
        },
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert sent["contact"] == "mapar@example.com"
    assert sent["comment"] == "PNG vypadá dobře, díky."
    assert sent["job_id"] == "job_42"
    assert sent["client_ip"]


def test_feedback_api_form_post(client, monkeypatch):
    import app.main as main

    sent = {}
    monkeypatch.setattr(main, "send_feedback", lambda **kw: sent.update(kw))
    r = client.post(
        "/api/feedback",
        data={
            "contact": "+420733575541",
            "comment": "Chybí lavičky u rybníka.",
            "job_id": "x1",
        },
    )
    assert r.status_code == 200
    assert sent["contact"] == "+420733575541"
    assert "lavičky" in sent["comment"]


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
            "website": "https://spam.example",
        },
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert called["n"] == 0


def test_feedback_requires_fields(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "send_feedback", lambda **_k: None)
    r = client.post("/api/feedback", json={"contact": "", "comment": "x"})
    assert r.status_code == 400
    r2 = client.post("/api/feedback", json={"contact": "a@b.cz", "comment": ""})
    assert r2.status_code == 400


def test_feedback_rate_limit(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "send_feedback", lambda **_k: None)
    monkeypatch.setattr("app.feedback.settings.MAX_FEEDBACK_PER_IP_HOUR", 2)
    monkeypatch.setattr("app.settings.MAX_FEEDBACK_PER_IP_HOUR", 2)

    for _ in range(2):
        ok = client.post(
            "/api/feedback",
            json={"contact": "a@b.cz", "comment": "ok"},
        )
        assert ok.status_code == 200

    blocked = client.post(
        "/api/feedback",
        json={"contact": "a@b.cz", "comment": "spam"},
    )
    assert blocked.status_code == 429
    assert "hodinu" in blocked.json()["detail"]


def test_feedback_smtp_error_503(client, monkeypatch):
    import app.main as main

    def fail(**_kwargs):
        raise MailError("SMTP není nastavené")

    monkeypatch.setattr(main, "send_feedback", fail)
    r = client.post(
        "/api/feedback",
        json={"contact": "a@b.cz", "comment": "test"},
    )
    assert r.status_code == 503
    assert "nešlo odeslat" in r.json()["detail"]


def test_send_feedback_uses_feedback_to(monkeypatch):
    captured = {}

    def fake_send_mail(to, subject, body, **_kw):
        captured["to"] = to
        captured["subject"] = subject
        captured["body"] = body

    monkeypatch.setattr("app.feedback.settings.FEEDBACK_TO", "owner@example.com")
    monkeypatch.setattr("app.feedback.settings.SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr("app.feedback.settings.SMTP_FROM", "from@example.com")
    monkeypatch.setattr("app.feedback.send_mail", fake_send_mail)

    send_feedback(contact="u@x.cz", comment="Ahoj", job_id="j1")
    assert captured["to"] == "owner@example.com"
    assert "j1" in captured["subject"]
    assert "u@x.cz" in captured["body"]


def test_check_feedback_rate_exempt(monkeypatch):
    monkeypatch.setattr("app.feedback.settings.MAX_FEEDBACK_PER_IP_HOUR", 1)
    monkeypatch.setattr("app.rate_limit.RATE_LIMIT_EXEMPT_IPS", frozenset({"9.9.9.9"}))
    assert check_feedback_rate("9.9.9.9") is None
    assert check_feedback_rate("9.9.9.9") is None
