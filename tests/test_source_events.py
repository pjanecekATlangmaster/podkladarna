"""Deník problémů zdrojů a souhrnný e-mail (app.source_events) – bez sítě."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import app.mail
import app.source_events as se
import app.source_status as ss


@pytest.fixture()
def fresh(data_dir, monkeypatch):
    monkeypatch.setattr(ss.settings, "SOURCE_CHECK_TTL_MIN", 3.0)
    monkeypatch.setattr(ss, "PROBE_RETRY_WAIT_S", 0)
    monkeypatch.setattr(ss.settings, "SOURCE_ALERT_AFTER_MIN", 0.0)
    monkeypatch.setattr(se.settings, "SOURCE_DIGEST_HOURS", 24.0)
    monkeypatch.setattr(se.settings, "FEEDBACK_TO", "provoz@example.cz")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(app.mail, "send_mail", lambda to, subj, body, **kw: sent.append((subj, body)))
    return data_dir, sent


@pytest.mark.parametrize(
    "code, error, planned, kind",
    [
        (503, None, False, "transient"),
        (None, "URLError: timeout", False, "transient"),
        (429, None, False, "transient"),
        (404, None, False, "persistent"),  # změněná URL = oprava u nás
        (403, None, False, "persistent"),
        (None, "URLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", False, "persistent"),
        (503, None, True, "planned"),
    ],
)
def test_classify(code, error, planned, kind):
    assert se.classify(code, error, planned) == kind


def _set_last(data_dir, hours_ago):
    last = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat()
    (data_dir / "source_digest.json").write_text(json.dumps({"last": last}), encoding="utf-8")


def test_events_logged_and_one_digest_per_period(fresh, monkeypatch):
    data_dir, sent = fresh
    state = {"dtm": 503}
    monkeypatch.setattr(
        ss, "_probe_url",
        lambda url, method, timeout_s=None: (state["dtm"], None, "") if state["dtm"] and "dmvs" in url else (200, None, ""),
    )
    ss.check_all()  # první běh založí období, nic neposílá
    assert sent == [] and (data_dir / "source_digest.json").is_file()
    ss.check_all()
    state["dtm"] = None
    ss.check_all()
    events = se.read_events()
    assert [e["event"] for e in events] == ["down", "down", "up"]
    assert events[0]["key"] == "dtm" and events[0]["kind"] == "transient" and events[0]["code"] == 503
    text = (data_dir / "source_events.jsonl").read_text(encoding="utf-8")
    assert "@" not in text  # žádné e-maily ani jiné osobní údaje

    _set_last(data_dir, 25)
    assert se.maybe_send_digest() is True
    assert se.maybe_send_digest() is False  # další období teprve začalo
    assert len(sent) == 1
    subject, body = sent[0]
    assert "souhrn zdrojů" in subject and "DTM krajů" in body and "2×" in body and "teď běží" in body


def test_digest_silent_when_nothing_to_report(fresh, monkeypatch):
    data_dir, sent = fresh
    monkeypatch.setattr(ss, "_probe_url", lambda url, method, timeout_s=None: (200, None, ""))
    ss.check_all()
    _set_last(data_dir, 25)
    assert se.maybe_send_digest() is False and sent == []


def test_summary_orders_persistent_first():
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    ev = lambda key, kind: {"ts": now, "event": "down", "key": key, "label": key, "countries": ["X"], "kind": kind}
    events = [ev("a", "transient"), ev("a", "transient"), ev("b", "persistent")]
    services = {"a": {"ok": False, "down_since": now}, "b": {"ok": False, "down_since": now}}
    groups = se.summary(events, services)
    assert [(g["key"], g["state"], g["count"]) for g in groups] == [("b", "persistent", 1), ("a", "down", 2)]


def test_endpoint_requires_token(fresh, monkeypatch, client):
    monkeypatch.setattr(ss, "_probe_url", lambda url, method, timeout_s=None: (404, None, "") if "dmvs" in url else (200, None, ""))
    ss.check_all()
    monkeypatch.setattr(se.settings, "SOURCE_EVENTS_TOKEN", "")
    assert client.get("/api/source_events").status_code == 404
    monkeypatch.setattr(se.settings, "SOURCE_EVENTS_TOKEN", "tajne-123")
    assert client.get("/api/source_events").status_code == 401
    assert client.get("/api/source_events", headers={"Authorization": "Bearer spatne"}).status_code == 401
    r = client.get("/api/source_events", params={"days": 1}, headers={"Authorization": "Bearer tajne-123"})
    assert r.status_code == 200
    body = r.json()
    assert body["summary"][0]["key"] == "dtm" and body["summary"][0]["state"] == "persistent"
    assert body["events"][0]["kind"] == "persistent" and "tajne" not in r.text


def test_prune_drops_old_events(data_dir, monkeypatch):
    monkeypatch.setattr(se.settings, "SOURCE_EVENTS_KEEP_DAYS", 30)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).replace(microsecond=0).isoformat()
    se.record([{"key": "a", "ok": False}], {}, now=old)
    se.record([{"key": "b", "ok": False}], {})
    se.prune()
    assert [e["key"] for e in se.read_events()] == ["b"]
