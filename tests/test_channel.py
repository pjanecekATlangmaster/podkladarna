"""Kanály A/B: stabilní verze bez zmínky o testu, testovací s bannerem a noindex."""

from __future__ import annotations

import re

import pytest

import app.main as main


def test_stable_channel_has_no_banner_and_no_noindex(client, monkeypatch):
    monkeypatch.setattr(main.app_settings, "INSTANCE_CHANNEL", "stable")
    res = client.get("/")
    assert res.status_code == 200
    assert "channel-banner" not in res.text and 'content="noindex' not in res.text
    assert "about-card" in res.text  # na stabilní verzi úvod zůstává
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
    assert "about-card" not in res.text and "O co jde" not in res.text  # úvodní blok je na testu skrytý
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


def test_usage_footer_marks_test_channel_and_has_no_personal_data(client, monkeypatch):
    from app import db

    job = db.create_job("j", "forest_10000", {"client_ip": "1.2.3.4", "map_types": ["les"]})
    db.update_job(job["id"], status="done")
    db.record_job_usage(job["id"], "done", {"total_seconds": 7200, "downloaded_bytes": 2.5e9})
    db.record_download(job["id"], 12_000_000)

    monkeypatch.setattr(main.app_settings, "SHOW_USAGE_STATS", True)
    text = client.get("/").text
    # Katalog textů pro app.js (window.I18N) obsahuje i hlášky testovací verze; zkoumá se jen vykreslená stránka.
    text = re.sub(r"<script data-cfasync=\"false\">window\.I18N = .*?;</script>", "", text, flags=re.S)
    assert 'class="footer-usage"' in text
    assert "vygenerováno 1 mapa (1 zadání)" in text and "2 h zpracování" in text
    assert "zpracováno 2,5 GB podkladů" in text and "1× staženo (12 MB)" in text
    assert "1.2.3.4" not in text and "Testovací verze" not in text
    en = client.get("/?lang=en").text
    assert "1 map generated (1 job)" in en and "2.5\u00a0GB of source data processed" in en

    monkeypatch.setattr(main.app_settings, "INSTANCE_CHANNEL", "test")
    assert "Testovací verze – od " in client.get("/?lang=cs").text
    en = client.get("/?lang=en").text
    assert "Test version – since " in en and "Go to the stable version" in en
    client.get("/?lang=cs")
    assert client.get("/api/usage_stats").json()["channel"] == "test"

    monkeypatch.setattr(main.app_settings, "SHOW_USAGE_STATS", False)
    assert "footer-usage" not in client.get("/").text


def test_usage_stats_api_counts_private_jobs_users_and_area(client, monkeypatch):
    """Box „v číslech“: agregace i se soukromými joby, odhad uživatelů z osolené IP, plocha map."""
    from app import db

    bbox = [14.40, 50.03, 14.42, 50.04]
    jobs = [
        db.create_job("a", "forest_10000", {"client_ip": "1.2.3.4", "map_types": ["les"], "bbox_wgs84": bbox}),
        db.create_job("b", "forest_10000", {"client_ip": "1.2.3.4", "map_types": ["les"]}),
        db.create_job(
            "c", "sprint_4000",
            {"client_ip": "5.6.7.8", "private": True, "notify_email": "x@example.com",
             "map_types": ["sprint"], "bbox_wgs84": bbox},
        ),
    ]
    for job in jobs:
        db.update_job(job["id"], status="done")
        db.record_job_usage(job["id"], "done", {"total_seconds": 600})

    monkeypatch.setattr(main.app_settings, "SHOW_USAGE_STATS", True)
    res = client.get("/api/usage_stats")
    data = res.json()
    assert data["enabled"] is True and data["donations"] is None
    assert data["totals"]["jobs_done"]["value"] == 3  # soukromý job se počítá
    assert data["last_30_days"]["jobs"] == 3 and data["last_30_days"]["avg_build_seconds"] == 600
    assert data["last_year"]["users"] == 2
    area = data["totals"]["area_km2"]["value"]
    assert area == pytest.approx(2 * db.job_area_km2({"bbox_wgs84": bbox}))
    assert "1.2.3.4" not in res.text and "example.com" not in res.text
    assert 'id="usage-box"' in client.get("/").text

    monkeypatch.setattr(main.app_settings, "SHOW_USAGE_STATS", False)
    assert client.get("/api/usage_stats").json() == {"enabled": False}
