"""Neopravené chyby jobu: značka ⚠, souhrn na konci logu, stav done_with_errors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.pipeline.job_progress import (
    IssueCollector,
    indented_log,
    log_issue,
)
from app.pipeline.run_job import background_log


def test_collector_records_main_and_background_issues():
    lines: list[str] = []
    issues = IssueCollector(lines.append)
    main = indented_log(issues)
    bg = background_log(issues, "podklady")

    main("Stahuji ZABAGED…")
    log_issue(main, "OSM: přeskočeno (timeout)")
    log_issue(bg, "Ortofoto: přeskočeno (HTTP 503)")
    # Stejná chyba podruhé (např. RÚIAN pro vegetaci i výstup) se nezdvojí.
    log_issue(main, "OSM: přeskočeno (timeout)")

    assert issues.items == [
        "OSM: přeskočeno (timeout)",
        "[podklady] Ortofoto: přeskočeno (HTTP 503)",
    ]
    assert "  ⚠ OSM: přeskočeno (timeout)" in lines
    assert "[podklady] ⚠ Ortofoto: přeskočeno (HTTP 503)" in lines

    issues.report()
    assert lines[-4:] == [
        "",
        "⚠ Dokončeno s chybami (2):",
        "  - OSM: přeskočeno (timeout)",
        "  - [podklady] Ortofoto: přeskočeno (HTTP 503)",
    ]
    # Souhrn se sám znovu nesbírá.
    assert len(issues.items) == 2


def test_collector_without_issues_reports_nothing():
    lines: list[str] = []
    issues = IssueCollector(lines.append)
    issues("vše v pořádku")
    issues.report()
    assert lines == ["vše v pořádku"]
    assert issues.items == []


def _isolated_db(tmp_path, monkeypatch):
    from app import job_worker

    monkeypatch.setattr("app.db.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("app.db.DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr(job_worker, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(job_worker, "ensure_proj_data", lambda: None)
    (tmp_path / "jobs").mkdir()
    db.init_db()
    return job_worker


def test_run_job_with_issues_sets_done_with_errors(tmp_path, monkeypatch):
    job_worker = _isolated_db(tmp_path, monkeypatch)
    job = db.create_job("s chybou", "forest_10000", {})
    monkeypatch.setattr(
        job_worker,
        "run_job_pipeline",
        lambda *a, **k: ["OSM: 1 z 4 dlaždic se nepodařilo stáhnout"],
    )

    assert job_worker.run_job(job["id"]) == 0

    saved = db.get_job(job["id"])
    assert saved["status"] == db.STATUS_DONE_WITH_ERRORS
    assert saved["error"].startswith("Mapa je hotová, ale je neúplná")
    assert "OpenStreetMap" in saved["error"]
    lines = [e["line"] for e in db.get_logs(job["id"])]
    assert any(ln.startswith("Shrnutí pro uživatele: Mapa je hotová") for ln in lines)
    assert "Status: done_with_errors" in lines
    assert db.STATUS_DONE_WITH_ERRORS in db.JOB_DONE_STATUSES
    assert db.STATUS_DONE_WITH_ERRORS in db.JOB_FINISHED_STATUSES


def test_run_job_without_issues_stays_done(tmp_path, monkeypatch):
    job_worker = _isolated_db(tmp_path, monkeypatch)
    job = db.create_job("ok", "forest_10000", {})
    monkeypatch.setattr(job_worker, "run_job_pipeline", lambda *a, **k: [])

    assert job_worker.run_job(job["id"]) == 0

    saved = db.get_job(job["id"])
    assert saved["status"] == "done"
    assert not saved["error"]


def test_cleanup_purges_old_done_with_errors(tmp_path, monkeypatch):
    from app.cleanup import purge_old_jobs

    monkeypatch.setattr("app.cleanup.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "t.db")
    (tmp_path / "jobs").mkdir()
    db.init_db()
    old = db.create_job("old", "forest_10000", {})
    db.update_job(old["id"], status=db.STATUS_DONE_WITH_ERRORS)
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (old_ts, old["id"]))
        conn.commit()

    assert purge_old_jobs(retention_hours=48) == 1


@pytest.mark.parametrize(
    "svc, dtm_needed, is_issue",
    [
        ({"key": "dtm", "needs": "dtm", "planned": True}, False, False),  # les / MTBO: jen poznámka
        ({"key": "dtm", "needs": "dtm", "planned": False}, False, False),
        ({"key": "dtm", "needs": "dtm", "planned": True}, True, True),  # sprint z DTM: upozornění
        ({"key": "osm", "needs": None, "planned": False}, False, True),  # ostatní zdroje beze změny
    ],
)
def test_down_source_lines_dtm_only_matters_for_sprint(svc, dtm_needed, is_issue):
    from app.pipeline.run_job import down_source_lines

    svc = {"label": "Zdroj X", "detail": "HTTP 503", "impact": "dopad", **svc}
    [(issue, text)] = down_source_lines([svc], dtm_needed=dtm_needed)
    assert issue is is_issue
    assert ("OSM a ZABAGED" in text) is (not is_issue)


@pytest.mark.parametrize(
    "key, confirmed, is_issue",
    [
        ("osm", True, False),  # a934d7c44276: Overpass 502 při kontrole, stažení přes zrcadlo prošlo
        ("osm", False, True),  # OSM se opravdu nestáhlo: chyba jobu jako dřív
        ("pl_elevation", True, False),  # výšky z rastru mimo Česko (GUGiK)
        ("fi_chm", False, True),  # výška porostu Metsäkeskus chybí
        ("lidar", True, True),  # český LiDAR se nepotvrzuje: beze změny
        ("zabaged", True, True),
    ],
)
def test_source_probe_issue_only_when_download_failed(key, confirmed, is_issue):
    from app import source_status
    from app.pipeline import job_progress
    from app.pipeline.run_job import resolve_source_issues

    lines: list[str] = []
    issues = IssueCollector(lines.append)
    log = indented_log(issues)
    job_progress.reset_confirmed_sources()
    role = source_status.probe_role(key)
    if confirmed and role:
        job_progress.confirm_source(role)
    text = "Zdroj X při poslední kontrole neodpovídal (HTTP 502): dopad"
    if role:
        resolve_source_issues([(role, "X", text)], log, issues)
    else:  # zdroj bez potvrzení: chyba už na startu (``run_job_pipeline``)
        log_issue(log, text)
    assert bool(issues.items) is is_issue
    if not is_issue:
        assert any("dočasně nedostupný" in ln for ln in lines)
    job_progress.reset_confirmed_sources()
