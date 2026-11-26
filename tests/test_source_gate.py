"""Odmítnutí jobu při potvrzeném výpadku zdroje výšek (app.source_gate)."""

from __future__ import annotations

import pytest

from app import i18n, settings, source_gate, source_status
from app.pipeline import elevation_raster

LYON = (4.825, 45.754, 4.840, 45.762)


@pytest.fixture
def gate(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(settings, "SOURCE_GATE", True)
    monkeypatch.setattr(settings, "SOURCE_GATE_TTL_MIN", 5.0)
    monkeypatch.setattr(source_status, "PROBE_RETRY_WAIT_S", 0.0)
    source_gate.clear_cache()
    calls: list[str] = []

    def fake_url(responses):
        it = iter(responses)

        def _probe_url(url, method, timeout_s=None):
            calls.append(url)
            return next(it, responses[-1])

        monkeypatch.setattr(source_status, "_probe_url", _probe_url)

    yield fake_url, calls
    source_gate.clear_cache()


DOWN = (None, "TimeoutError: timed out", "")
UP = (200, None, "")


def test_critical_probes_only_foreign_elevation():
    assert source_gate.critical_probes("CZ") == []  # Česko beze změny
    keys = {p.key for p in source_gate.critical_probes("PL")}
    assert keys == {"pl_elevation"}  # OSM ani ortofoto nejsou kritické
    assert "osm" not in {p.key for p in source_gate.critical_probes("FR")}


# (odpovědi zdroje po sobě, odmítnout) – odmítá se až po dvou selháních za sebou
@pytest.mark.parametrize(
    "responses, rejected",
    [
        ([DOWN, DOWN], True),
        ([DOWN, UP], False),  # jednorázové selhání se přijme
        ([UP], False),
    ],
)
def test_reject_only_confirmed_outage(gate, responses, rejected):
    fake_url, _calls = gate
    fake_url(responses)
    out = source_gate.check("PL")
    assert (out is not None) == rejected
    if rejected:
        assert out["key"] == "pl_elevation"
        assert "neodpovídá" in i18n.localize(out["message"], "cs")
        assert "not responding" in i18n.localize(out["message"], "en")


def test_fallback_accepts_without_probe(gate):
    fake_url, calls = gate
    fake_url([DOWN, DOWN])
    assert source_gate.check("PL", has_fallback=True) is None
    assert calls == []


def test_result_is_cached(gate):
    fake_url, calls = gate
    fake_url([UP])
    assert source_gate.check("PL") is None
    assert source_gate.check("PL") is None
    assert len(calls) == 1  # druhé kliknutí bez dotazu na cizí server


def test_fresh_status_ok_skips_probe(gate):
    fake_url, calls = gate
    fake_url([DOWN, DOWN])
    source_status._save({"checked_at": source_status._now_iso(), "services": {"pl_elevation": {"ok": True}}})
    assert source_gate.check("PL") is None
    assert calls == []


def test_planned_outage_message(gate):
    fake_url, _calls = gate
    fake_url([(503, None, "Plánovaná odstávka")])
    out = source_gate.check("PL")
    assert out["planned"] and "odstávku" in i18n.localize(out["message"], "cs")


def test_api_rejects_job_without_counting(client, monkeypatch, gate):
    import app.main as main
    from app import db

    fake_url, _calls = gate
    fake_url([DOWN, DOWN])
    monkeypatch.setenv("COUNTRIES_BETA", "FR")
    monkeypatch.setenv("SITES", "testserver=cs:free:*")
    monkeypatch.setitem(elevation_raster.COVERAGE, "fr_lidarhd", lambda _b: 1.0)
    monkeypatch.setattr(elevation_raster, "_COVERAGE_CACHE", {})
    monkeypatch.setattr(main.worker, "enqueue", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "queue_position", lambda *_a, **_k: 0)
    bbox = ",".join(str(v) for v in LYON)
    data = {"preset_id": "sprint_2m", "output_mode": "png", "name": "lyon", "bbox": bbox}
    for _ in range(3):  # opakované kliknutí: pořád odmítnuto, ne limitem (429)
        r = client.post("/api/jobs", data=data)
        assert r.status_code == 503
        assert "neodpovídá" in r.json()["detail"]
    assert not db.list_jobs()  # job nevznikl, limity (počítané z DB) se nezvýšily
    source_gate.clear_cache()
    fake_url([UP])
    r = client.post("/api/jobs", data=data)
    assert r.status_code == 200


# --- job selhaný na výpadku zdroje --------------------------------------------------------------------

@pytest.mark.parametrize(
    "country, technical, log_text, live, expected",
    [
        ("PL", "HTTP Error 503: Service Unavailable", "{note}", [DOWN], "pl_elevation"),  # řádek startovní kontroly
        ("PL", "výšková služba má výpadek na své straně (HTTP 400: Database connection failed)", "", [UP], "pl_elevation"),
        ("FR", "Výšková data (fr_lidarhd) nejsou k dispozici: výšková služba má výpadek na své straně (HTTP 400: "
               "Layer IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93 unknown, 4 pokusy)", "", [UP], "fr_lidarhd"),
        ("PL", "HTTP Error 503: Service Unavailable", "", [DOWN, DOWN], "pl_elevation"),  # potvrzeno teď
        ("PL", "HTTP Error 503: Service Unavailable", "", [UP], None),  # zdroj běží: obecná chyba
        ("PL", "No space left on device", "{note}", [DOWN], None),  # disk není výpadek zdroje
        ("CZ", "HTTP Error 503: Service Unavailable", "", [DOWN, DOWN], None),  # Česko beze změny
    ],
)
def test_failed_job_outage(gate, country, technical, log_text, live, expected):
    fake_url, _calls = gate
    fake_url(live)
    label = next(p.label for p in source_gate.critical_probes("PL"))
    probe = source_gate.failed_job_outage(country, technical, log_text.replace("{note}", f"Zdroj {label} neodpovídal. Ověří se při stahování."))
    assert (probe.key if probe else None) == expected


def test_source_outage_job_not_counted_in_limits(data_dir, monkeypatch):
    import importlib

    import app.db as db

    for key, val in {"MAX_JOBS_PER_IP_DAY": "2", "MAX_JOBS_PER_IP_HOUR": "2", "MAX_ACTIVE_JOBS_PER_IP": "100",
                     "MAX_PRIVATE_JOBS_PER_EMAIL_DAY": "2"}.items():
        monkeypatch.setenv(key, val)
    import app.rate_limit as rate_limit

    importlib.reload(settings)
    importlib.reload(rate_limit)
    db.init_db()
    opts = {"client_ip": "10.0.7.1", "private": True, "notify_email": "a@example.cz", "country": "PL"}
    for _ in range(2):
        job = db.create_job("pl", "sprint_2m", opts)
        db.update_job(job["id"], status="failed")
        db.mark_source_outage(job["id"], "pl_elevation")
    assert rate_limit.check_create_job("10.0.7.1") is None
    assert rate_limit.check_private_email("a@example.cz", "10.0.7.1") is None
    db.create_job("pl", "sprint_2m", opts)
    db.create_job("pl", "sprint_2m", opts)
    assert rate_limit.check_create_job("10.0.7.1") is not None  # běžné joby se počítají dál
    assert rate_limit.check_private_email("a@example.cz", "10.0.7.1") is not None


def test_recheck_throttled_and_merged(gate):
    fake_url, calls = gate
    fake_url([UP])
    source_status._save({
        "checked_at": source_status._now_iso(),
        "services": {"pl_elevation": {"key": "pl_elevation", "ok": False, "down_since": source_status._now_iso()}},
    })
    out = source_gate.recheck("PL")
    assert out["rechecked"] == ["pl_elevation"] and not out["throttled"]
    assert source_status._load()["services"]["pl_elevation"]["ok"] is True
    assert source_gate.check("PL") is None  # brána má čerstvý výsledek, žádný další dotaz
    n = len(calls)
    source_status._save({"checked_at": source_status._now_iso(),
                         "services": {"pl_elevation": {"key": "pl_elevation", "ok": False}}})
    out = source_gate.recheck("PL")  # podruhé do minuty: bez dotazu na cizí server
    assert out["rechecked"] == [] and out["throttled"] and len(calls) == n
    source_gate._recheck_at.clear()


def test_sheets_warns_recent_outage(client, monkeypatch, gate):
    from app import db

    monkeypatch.setenv("COUNTRIES_BETA", "FR")
    monkeypatch.setenv("SITES", "testserver=cs:free:*")
    monkeypatch.setitem(elevation_raster.COVERAGE, "fr_lidarhd", lambda _b: 1.0)
    monkeypatch.setattr(elevation_raster, "_COVERAGE_CACHE", {})
    bbox = ",".join(str(v) for v in LYON)
    assert client.get("/api/sheets", params={"bbox": bbox}).json()["recent_source_outage"] is None
    job = db.create_job("lyon", "sprint_2m", {"bbox_wgs84": list(LYON), "country": "FR"})
    db.update_job(job["id"], status="failed")
    db.mark_source_outage(job["id"], "fr_lidarhd")
    body = client.get("/api/sheets", params={"bbox": bbox}).json()
    assert body["recent_source_outage"]["key"] == "fr_lidarhd" and body["recent_source_outage"]["minutes"] >= 1
    assert isinstance(body["recent_source_outage"]["label"], str)


def test_source_status_marks_blocking_source(client, monkeypatch, gate):
    monkeypatch.setenv("COUNTRIES_BETA", "PL")
    monkeypatch.setenv("SITES", "testserver=cs:free:*")
    source_status._save({"checked_at": source_status._now_iso(), "services": {
        "pl_elevation": {"key": "pl_elevation", "label": "GUGiK", "ok": False},
        "osm": {"key": "osm", "label": "OSM", "ok": False},
    }})
    services = {s["key"]: s for s in client.get("/api/source_status", params={"country": "PL"}).json()["services"]}
    assert services["pl_elevation"]["blocks_job"] and not services["osm"]["blocks_job"]


def test_rejection_and_failure_recorded_in_source_events(gate):
    from app import source_events

    fake_url, _calls = gate
    fake_url([DOWN, DOWN])
    assert source_gate.check("PL") is not None
    probe = source_gate.critical_probes("PL")[0]
    source_gate._record_impact("job_failed", probe, "PL", None)
    events = source_events.read_events()
    assert [e["event"] for e in events] == ["rejected", "job_failed"]
    assert events[0]["countries"] == ["PL"] and "client_ip" not in events[0]
    group = source_events.summary(events)[0]
    assert group["key"] == "pl_elevation" and group["rejected"] == 1 and group["job_failed"] == 1
    subject, body = source_events.build_digest_email([{**group, "state": "down"}], source_events._now())
    assert "odmítnuté joby 1×" in body
