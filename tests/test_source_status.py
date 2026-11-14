"""Kontrola dostupnosti zdrojů dat (app.source_status) – bez sítě."""

from __future__ import annotations

import time

import pytest

import app.source_status as ss


@pytest.fixture()
def fresh_state(data_dir, monkeypatch):
    monkeypatch.setattr(ss.settings, "SOURCE_CHECK_TTL_MIN", 3.0)
    monkeypatch.setattr(ss, "PROBE_RETRY_WAIT_S", 0)
    monkeypatch.setattr(ss.settings, "SOURCE_ALERT_AFTER_MIN", 0.0)
    return data_dir


@pytest.mark.parametrize(
    "codes, any_4xx_ok, ok, detail, planned",
    [
        ([200], False, True, None, False),
        ([503, 200], False, True, None, False),  # krátký výpadek: opakovaný pokus projde
        ([503, 503], False, False, "HTTP 503", False),
        ([None, None], False, False, "URLError: timeout", False),
        ([400], True, True, None, False),  # Overpass bez dotazu
        ([404, 404], False, False, "HTTP 404", False),
        # DMVS 2026-11: 503 s textem „Plánovaná odstávka“
        ([(503, "Plánovaná odstávka")] * 2, False, False, "plánovaná odstávka, HTTP 503", True),
        ([(404, "odstávka")] * 2, False, False, "HTTP 404", False),  # jen 5xx
    ],
)
def test_check_probe(fresh_state, monkeypatch, codes, any_4xx_ok, ok, detail, planned):
    seq = list(codes)

    def fake(url, method):
        code, text = item if isinstance(item := seq.pop(0), tuple) else (item, "")
        return code, (None if code else "URLError: timeout"), text

    monkeypatch.setattr(ss, "_probe_url", fake)
    probe = ss.Probe("x", "X", ("https://x.example/",), "dopad", any_4xx_ok=any_4xx_ok)
    r = ss.check_probe(probe)
    assert (r["ok"], r["detail"], r["planned"]) == (ok, detail, planned)


def test_check_all_tracks_down_since_file_and_api(fresh_state, monkeypatch, client):
    state = {"dtm": False}
    probes: list[str] = []

    def fake(url, method):
        probes.append(url)
        return (200, None, "") if state["dtm"] or "dmvs" not in url else (503, None, "")

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
    assert [s["key"] for s in body["services"]] == [p.key for p in ss.probes_for("CZ")]

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


def test_outage_alert_once_after_threshold_and_once_on_recovery(fresh_state, monkeypatch):
    """Výpadek ≥ SOURCE_ALERT_AFTER_MIN: jeden mail; další kontroly nic; po obnovení jeden mail."""
    import json
    from datetime import datetime, timedelta, timezone

    import app.mail

    monkeypatch.setattr(ss.settings, "SOURCE_ALERT_AFTER_MIN", 30.0)
    monkeypatch.setattr(ss.settings, "FEEDBACK_TO", "provoz@example.cz")
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(app.mail, "send_mail", lambda to, subj, body, **kw: sent.append((to, subj, body)))
    state = {"dtm": False}

    def fake(url, method):
        if state["dtm"] or "dmvs" not in url:
            return 200, None, ""
        return 503, None, "Plánovaná odstávka"

    monkeypatch.setattr(ss, "_probe_url", fake)
    ss.check_all()
    assert sent == []  # výpadek právě začal

    path = fresh_state / "source_status.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    old = (datetime.now(timezone.utc) - timedelta(minutes=45)).replace(microsecond=0).isoformat()
    data["services"]["dtm"]["down_since"] = old
    path.write_text(json.dumps(data), encoding="utf-8")

    ss.check_all()
    ss.check_all()
    assert len(sent) == 1
    to, subject, body = sent[0]
    assert to == "provoz@example.cz" and "plánovanou odstávku" in subject and "45 min" in body

    # jiný proces se starým stavem (alerted chybí) mail nezopakuje – značka v DATA_ROOT
    data = json.loads(path.read_text(encoding="utf-8"))
    data["services"]["dtm"]["alerted"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    ss.check_all()
    assert len(sent) == 1

    state["dtm"] = True
    ss.check_all()
    ss.check_all()
    assert len(sent) == 2 and "znovu běží" in sent[1][1]


def test_short_outage_sends_nothing(fresh_state, monkeypatch):
    import app.mail

    monkeypatch.setattr(ss.settings, "SOURCE_ALERT_AFTER_MIN", 30.0)
    sent: list[str] = []
    monkeypatch.setattr(app.mail, "send_mail", lambda to, subj, body, **kw: sent.append(subj))
    state = {"down": True}
    monkeypatch.setattr(
        ss, "_probe_url", lambda url, method: (503, None, "") if state["down"] and "dmvs" in url else (200, None, "")
    )
    ss.check_all()
    state["down"] = False
    ss.check_all()
    assert sent == []


def test_local_sources_by_country(fresh_state, monkeypatch, client):
    """Web ukáže místní servery země výřezu; vypnutá země se nekontroluje."""
    urls: list[str] = []

    def fake(url, method):
        urls.append(url)
        return (503, None, "") if "luchtfoto" in url else (200, None, "")

    monkeypatch.setattr(ss, "_probe_url", fake)
    ss.check_all()
    assert not any("pdok.nl" in u for u in urls)  # NL je beta, bez COUNTRIES_BETA se nenabízí
    monkeypatch.setenv("COUNTRIES_BETA", "NL")
    assert ss.is_stale()  # nově nabízená země ještě nemá výsledek
    ss.check_all()
    nl = client.get("/api/source_status", params={"country": "nl"}).json()
    keys = [s["key"] for s in nl["services"]]
    assert nl["country"] == "NL" and "osm" in keys and "nl_ahn" in keys and "lidar" not in keys
    assert [s["key"] for s in ss.down_services(country="NL")] == ["nl_luchtfoto"]
    assert ss.down_services() == []  # české zdroje běží
    cz = client.get("/api/source_status", params={"country": "XX"}).json()
    assert cz["country"] == "CZ" and "nl_ahn" not in [s["key"] for s in cz["services"]]
