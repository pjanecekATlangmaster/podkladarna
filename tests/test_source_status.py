"""Kontrola dostupnosti zdrojů dat (app.source_status) – bez sítě."""

from __future__ import annotations

import time

import pytest

import app.source_status as ss


@pytest.fixture()
def fresh_state(data_dir, monkeypatch):
    monkeypatch.setattr(ss.settings, "SOURCE_CHECK_TTL_MIN", 3.0)
    monkeypatch.setattr(ss, "PROBE_RETRY_WAIT_S", 0)
    return data_dir


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


def test_check_all_tracks_down_since_file_and_api(fresh_state, monkeypatch, client):
    state = {"dtm": False}
    probes: list[str] = []

    def fake(url, method):
        probes.append(url)
        return (200, None) if state["dtm"] or "dmvs" not in url else (503, None)

    monkeypatch.setattr(ss, "_probe_url", fake)
    first = ss.check_all()
    assert (fresh_state / "source_status.json").is_file()  # vidí ho i proces jobu
    dtm = next(s for s in first["services"] if s["key"] == "dtm")
    assert not dtm["ok"] and dtm["down_since"] and dtm["needs"] == "dtm"
    assert all(s["ok"] for s in first["services"] if s["key"] != "dtm")
    since = dtm["down_since"]
    second = ss.check_all()
    assert next(s for s in second["services"] if s["key"] == "dtm")["down_since"] == since
    # čerstvý výsledek: job ani web nic znovu nevolají
    probes.clear()
    assert [s["key"] for s in ss.down_services()] == ["dtm"]
    assert ss.down_services(use_dtm=False) == []
    assert ss.refresh_if_stale() is False and probes == []
    body = client.get("/api/source_status").json()
    assert body["enabled"] is True and body["checking"] is False
    assert [s["key"] for s in body["services"]] == [p.key for p in ss.PROBES]

    # po TTL: job ověří znovu sám (synchronně)
    state["dtm"] = True
    monkeypatch.setattr(ss.settings, "SOURCE_CHECK_TTL_MIN", 0.0001)
    import time

    time.sleep(0.02)
    assert ss.down_services() == []
    assert next(s for s in ss.snapshot()["services"] if s["key"] == "dtm")["down_since"] is None


def test_refresh_if_stale_runs_one_background_check(fresh_state, monkeypatch):
    import threading

    gate = threading.Event()
    calls: list[int] = []

    def slow_check():
        calls.append(1)
        gate.wait(5)

    monkeypatch.setattr(ss, "check_all", slow_check)
    assert ss.refresh_if_stale() is True  # nic uloženo → kontrola na pozadí
    assert ss.refresh_if_stale() is True  # další návštěvník: žádná druhá kontrola
    gate.set()
    for _ in range(100):
        if not ss._refresh_lock.locked():
            break
        time.sleep(0.01)
    assert calls == [1]
    monkeypatch.setattr(ss.settings, "SOURCE_CHECK_TTL_MIN", 0.0)
    assert ss.refresh_if_stale() is False and ss.down_services() == []
