"""Kanály A/B: stabilní verze bez zmínky o testu, testovací s bannerem a noindex."""

from __future__ import annotations

import app.main as main


def test_stable_channel_has_no_banner_and_no_noindex(client, monkeypatch):
    monkeypatch.setattr(main.app_settings, "INSTANCE_CHANNEL", "stable")
    res = client.get("/")
    assert res.status_code == 200
    assert "channel-banner" not in res.text and 'content="noindex' not in res.text
    assert "x-robots-tag" not in res.headers
    assert client.get("/api/health").json()["channel"] == "stable"


def test_test_channel_shows_banner_with_stable_link_and_noindex(client, monkeypatch):
    monkeypatch.setattr(main.app_settings, "INSTANCE_CHANNEL", "test")
    monkeypatch.setattr(main.app_settings, "INSTANCE_NAME", "B")
    monkeypatch.setattr(main.app_settings, "STABLE_URL", "https://podkladarna.kibos.link")
    res = client.get("/")
    assert res.status_code == 200
    assert 'class="channel-banner"' in res.text
    assert "Testovací verze (B)" in res.text
    assert 'href="https://podkladarna.kibos.link"' in res.text
    assert '<meta name="robots" content="noindex, nofollow"' in res.text
    assert res.headers["x-robots-tag"] == "noindex, nofollow"
    # noindex i na API / statice
    assert client.get("/api/health").headers["x-robots-tag"] == "noindex, nofollow"
    health = client.get("/api/health").json()
    assert health["channel"] == "test" and health["instance"] == "B"


def test_banner_escapes_stable_url(monkeypatch):
    monkeypatch.setattr(main.app_settings, "INSTANCE_CHANNEL", "test")
    monkeypatch.setattr(main.app_settings, "STABLE_URL", 'https://x/"><script>')
    html = main.apply_channel("<head></head><body><p>x</p></body>")
    assert "<script>" not in html and "&quot;&gt;&lt;script&gt;" in html


def test_jobs_api_exposes_shared_busy_only_as_flag(client):
    data = client.get("/api/jobs").json()
    assert data["shared_busy"] is False  # bez PODKLADARNA_SHARED_SLOTS vždy False
    assert "current" in data and "private_live" in data
