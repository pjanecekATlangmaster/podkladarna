"""Výpis nepovedených jobů pro denní kontrolu (GET /api/ops/failed_jobs) – token a bez osobních údajů."""

from __future__ import annotations

from app import db, settings


def test_failed_jobs_endpoint(data_dir, monkeypatch, client):
    job = db.create_job("Tajny les Novak", "forest_10000", {
        "private": True, "notify_email": "petr@example.cz", "client_ip": "10.1.2.3", "country": "FR",
    })
    db.append_log(job["id"], "Stahuji výšky pro Tajny les Novak")
    db.append_log(job["id"], "Hotovo, odkaz poslán na petr@example.cz")
    db.append_log(job["id"], "CHYBA: Výšková data (fr_lidarhd) nejsou k dispozici: HTTP Error 400")
    db.append_log(job["id"], "Traceback (most recent call last):\nRuntimeError: Výšková data (fr_lidarhd)")
    db.update_job(job["id"], status="failed", error="Mapu nejde vytvořit.")
    ok = db.create_job("Jiny", "sprint_4000", {})
    db.update_job(ok["id"], status="done")

    monkeypatch.setattr(settings, "SOURCE_EVENTS_TOKEN", "")
    assert client.get("/api/ops/failed_jobs").status_code == 404
    monkeypatch.setattr(settings, "SOURCE_EVENTS_TOKEN", "tajne-123")
    assert client.get("/api/ops/failed_jobs").status_code == 401
    r = client.get("/api/ops/failed_jobs", headers={"Authorization": "Bearer tajne-123"})
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    assert [j["id"] for j in jobs] == [job["id"]]
    j = jobs[0]
    assert j["country"] == "FR" and j["private"] is True and j["status"] == "failed"
    assert j["traceback"].endswith("RuntimeError: Výšková data (fr_lidarhd)")
    assert any(line.startswith("CHYBA") for line in j["log"])
    for secret in ("Novak", "petr@example.cz", "10.1.2.3", "tajne-123"):
        assert secret not in r.text
