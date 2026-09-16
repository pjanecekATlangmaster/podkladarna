from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.whats_new import whats_new_payload


def test_whats_new_payload_includes_czech_disclaimer(monkeypatch):
    released = datetime(2026, 9, 16, 16, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("PODKLADARNA_BUILT_AT", released.isoformat())
    payload = whats_new_payload(now=released + timedelta(hours=1))
    assert "rozbilo" in payload["disclaimer"]
    if payload["entries"]:
        # Po překladu by nadpis neměl být holý anglický "Replace …"
        assert not payload["entries"][0]["title"].startswith("Replace ")


def test_whats_new_payload_hot_when_fresh(monkeypatch):
    released = datetime(2026, 9, 16, 16, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("PODKLADARNA_BUILT_AT", released.isoformat())
    now = released + timedelta(hours=1)
    payload = whats_new_payload(now=now)
    assert payload["tone"] == "hot"
    assert payload["open"] is True
    assert payload["version"]
    assert isinstance(payload["entries"], list)
    assert payload["age_hours"] == 1.0


def test_whats_new_payload_calm_after_week(monkeypatch):
    released = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("PODKLADARNA_BUILT_AT", released.isoformat())
    now = released + timedelta(days=10)
    payload = whats_new_payload(now=now)
    assert payload["tone"] == "calm"
    assert payload["open"] is False


def test_whats_new_filters_old_entries(monkeypatch):
    released = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("PODKLADARNA_BUILT_AT", released.isoformat())
    now = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)
    payload = whats_new_payload(now=now)
    assert payload["entries"]
    assert all(e["date"] >= "2026-08-17" for e in payload["entries"])


def test_api_whats_new(client, monkeypatch):
    monkeypatch.setenv(
        "PODKLADARNA_BUILT_AT",
        datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc).isoformat(),
    )
    r = client.get("/api/whats_new")
    assert r.status_code == 200
    body = r.json()
    assert "tone" in body
    assert "entries" in body
    assert "version" in body
    assert "disclaimer" in body
    assert "rozbilo" in body["disclaimer"]


def test_index_has_whats_new_box(client):
    html = client.get("/").text
    assert 'id="whats-new"' in html
    assert "whats-new-list" in html
    assert "whats-new-disclaimer" in html
    assert "whats-new-scroll" in html
