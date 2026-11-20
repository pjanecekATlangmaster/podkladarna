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

        def _probe_url(url, method):
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
