"""Kontrola dostupnosti zdrojů dat (app.source_status) – bez sítě."""

from __future__ import annotations

import pytest

import app.source_status as ss


@pytest.fixture()
def fresh_state(monkeypatch):
    monkeypatch.setattr(ss, "_state", {"checked_at": None, "services": {}})
    monkeypatch.setattr(ss, "PROBE_RETRY_WAIT_S", 0)


@pytest.mark.parametrize(
    "codes, any_4xx_ok, ok, detail",
    [
        ([200], False, True, None),
        ([503, 200], False, True, None),  # krátký výpadek: opakovaný pokus projde
        ([503, 503], False, False, "HTTP 503"),
        ([None, None], False, False, "URLError: timeout"),
        ([400], True, True, None),  # Overpass bez dotazu
        ([404, 404], False, False, "HTTP 404"),
    ],
)
def test_check_probe(fresh_state, monkeypatch, codes, any_4xx_ok, ok, detail):
    seq = list(codes)

    def fake(url, method):
        code = seq.pop(0)
        return code, (None if code else "URLError: timeout")

    monkeypatch.setattr(ss, "_probe_url", fake)
    probe = ss.Probe("x", "X", ("https://x.example/",), "dopad", any_4xx_ok=any_4xx_ok)
    r = ss.check_probe(probe)
    assert (r["ok"], r["detail"]) == (ok, detail)


def test_check_all_tracks_down_since_and_api(fresh_state, monkeypatch, client):
    state = {"dtm": False}
    monkeypatch.setattr(
        ss, "_probe_url", lambda url, method: (200, None) if state["dtm"] or "dmvs" not in url else (503, None)
    )
    first = ss.check_all()
    dtm = next(s for s in first["services"] if s["key"] == "dtm")
    assert not dtm["ok"] and dtm["down_since"] and dtm["needs"] == "dtm"
    assert all(s["ok"] for s in first["services"] if s["key"] != "dtm")
    since = dtm["down_since"]
    second = ss.check_all()
    assert next(s for s in second["services"] if s["key"] == "dtm")["down_since"] == since
    assert [s["key"] for s in ss.down_services()] == ["dtm"]
    assert ss.down_services(use_dtm=False) == []

    body = client.get("/api/source_status").json()
    assert body["enabled"] is False  # testy kontrolu nespouštějí (conftest)
    assert [s["key"] for s in body["services"]] == [p.key for p in ss.PROBES]

    state["dtm"] = True
    ss.check_all()
    assert ss.down_services() == []
    assert next(s for s in ss.snapshot()["services"] if s["key"] == "dtm")["down_since"] is None
