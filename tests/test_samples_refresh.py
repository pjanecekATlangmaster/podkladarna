from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

NOW = datetime(2026, 11, 20, 2, 0, tzinfo=timezone.utc)


def test_config_lists_known_sample_files():
    """Každá ukázka v configs/samples.yaml má jméno, které web nabízí (app.main._SAMPLE_NAME_RE)."""
    from app import main, samples_refresh

    samples = samples_refresh.load_samples()
    assert {s["file"] for s in samples} == {
        "sprint.zip", "les.zip", "mtbo.zip", "sprint-en.zip", "les-en.zip", "mtbo-en.zip", "reference-en.zip"
    }
    for s in samples:
        assert main._SAMPLE_NAME_RE.match(s["file"])
        assert s["lang"] == ("cs" if not s["file"].endswith("-en.zip") else "en")


@pytest.mark.parametrize(
    ("state", "manual", "due"),
    [
        ({}, False, True),  # ještě nikdy
        ({"version": "{minor}.0", "done_at": "{now}"}, False, False),  # aktuální
        ({"version": "{minor}.9", "done_at": "{now}"}, False, False),  # jiný patch = beze změny
        ({"version": "1.0.0", "done_at": "{now}"}, False, True),  # jiná minor verze
        ({"version": "{minor}.0", "done_at": "2026-09-01T00:00:00+00:00"}, False, True),  # starší než 30 dní
        ({"version": "{minor}.0", "done_at": "{now}"}, True, True),  # ruční žádost
    ],
)
def test_refresh_reason(data_dir, state, manual, due):
    from app import samples_refresh, settings

    minor = samples_refresh.minor_version(settings.APP_VERSION)
    state = {k: v.format(minor=minor, now=NOW.isoformat()) for k, v in state.items()}
    if manual:
        settings.SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        (settings.SAMPLES_DIR / samples_refresh.REQUEST_NAME).write_text("x")
    assert (samples_refresh.refresh_reason(state, now=NOW) is not None) is due


@pytest.mark.parametrize(
    ("raw", "hour", "inside"),
    [("1-5", 1, True), ("1-5", 5, True), ("1-5", 6, False), ("22-4", 23, True), ("22-4", 3, True), ("22-4", 12, False), ("", 2, False)],
)
def test_night_window(raw, hour, inside):
    from app import samples_refresh

    assert samples_refresh.in_window(hour, samples_refresh.parse_hours(raw)) is inside


def _fake_job(jobs_dir: Path, job_id: str, *, lang: str = "cs", dtm: bool = True) -> dict:
    out = jobs_dir / job_id / "output"
    out.mkdir(parents=True)
    with zipfile.ZipFile(out / "podkladarna_output.zip", "w") as zf:
        zf.writestr("mapa.omap", "<map/>")
    if dtm:
        (jobs_dir / job_id / "work" / "dtm").mkdir(parents=True)
        (jobs_dir / job_id / "work" / "dtm" / "fences.json").write_text(json.dumps({"packages": ["ZPS_KR.zip"]}))
    return {"id": job_id, "status": "done", "options": {"lang": lang}}


@pytest.mark.parametrize(
    ("job_kw", "status", "sample", "error"),
    [
        ({}, "done", {"file": "les.zip", "lang": "cs", "dtm": True}, None),
        ({"dtm": False}, "done", {"file": "les.zip", "lang": "cs", "dtm": True}, "DTM"),
        ({"dtm": False}, "done", {"file": "les-en.zip", "lang": "en"}, "jazyce"),
        ({"dtm": False, "lang": "en"}, "done", {"file": "les-en.zip", "lang": "en"}, None),
        ({}, "done_with_errors", {"file": "les.zip", "lang": "cs", "dtm": True}, "stavu"),
    ],
)
def test_check_result_and_install(data_dir, job_kw, status, sample, error):
    from app import db, samples_refresh, settings

    job = _fake_job(db.JOBS_DIR, "job1", **job_kw)
    job["status"] = status
    if error:
        with pytest.raises(RuntimeError, match=error):
            samples_refresh.check_result(job, sample)
        return
    samples_refresh.install(samples_refresh.check_result(job, sample), {**sample, "label": "popisek"})
    assert zipfile.is_zipfile(settings.SAMPLES_DIR / sample["file"])
    manifest = json.loads((settings.SAMPLES_DIR / "samples.json").read_text(encoding="utf-8"))
    assert manifest == {sample["file"]: "popisek"}


def test_tick_round_keeps_failed_sample_and_finishes(data_dir, monkeypatch):
    """Nepovedená ukázka zůstane stará, zkusí se jednou denně nejvýš MAX_ATTEMPTS×; pak kolo skončí."""
    from app import samples_refresh, settings

    samples = [{"file": "sprint.zip", "bbox": [1, 2, 3, 4]}, {"file": "les.zip", "bbox": [1, 2, 3, 4]}]
    calls: list[str] = []

    def fake_refresh(sample):
        calls.append(sample["file"])
        if sample["file"] == "les.zip":
            raise RuntimeError("bez dat DTM")
        return "job-ok"

    monkeypatch.setattr(samples_refresh, "refresh_one", fake_refresh)
    monkeypatch.setattr(samples_refresh, "may_run_now", lambda **_k: True)
    now = NOW
    assert samples_refresh.tick(now=now, samples=samples) == "sprint.zip"
    assert samples_refresh.tick(now=now, samples=samples) == "les.zip"
    assert samples_refresh.tick(now=now, samples=samples) is None  # druhý pokus až další den
    for _ in range(samples_refresh.MAX_ATTEMPTS - 1):
        now += timedelta(days=1)
        assert samples_refresh.tick(now=now, samples=samples) == "les.zip"
    state = samples_refresh.read_state()
    assert "round" not in state
    assert state["version"] == settings.APP_VERSION
    assert state["last_round"]["results"]["les.zip"]["ok"] is False
    assert calls.count("les.zip") == samples_refresh.MAX_ATTEMPTS
    assert samples_refresh.tick(now=now, samples=samples) is None  # ukázky aktuální


def test_tick_waits_for_busy_server(data_dir, monkeypatch):
    from app import samples_refresh

    monkeypatch.setattr(samples_refresh, "server_idle", lambda: False)
    monkeypatch.setattr(samples_refresh, "refresh_one", lambda s: pytest.fail("server počítá"))
    assert samples_refresh.tick(now=NOW, samples=[{"file": "sprint.zip", "bbox": [1, 2, 3, 4]}]) is None


def test_sample_job_is_private_and_outside_stats(client, monkeypatch):
    import app.main as main
    from app import db, samples_refresh

    monkeypatch.setattr(main, "query_sm5_sheets", lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}])
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: pytest.fail("ukázka nemá limit"))
    monkeypatch.setattr(main.worker, "enqueue", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "queue_position", lambda *_a, **_k: 0)
    data = dict(samples_refresh.form_fields({"file": "sprint.zip", "name": "Ukázka", "bbox": [14.40, 50.08, 14.42, 50.09], "map_scale": 4000}))
    r = client.post("/api/jobs?lang=cs", data=data, headers={samples_refresh.TOKEN_HEADER: samples_refresh._token()})
    assert r.status_code == 200
    job = db.get_job(r.json()["id"])
    assert job["options"]["sample"] is True and job["options"]["private"] is True
    assert job["options"]["dtm_sprint"] is True
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM job_origin WHERE job_id = ?", (job["id"],)).fetchone()[0] == 0
    # Neplatný token = běžný job (s limitem).
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    r = client.post("/api/jobs", data=data, headers={samples_refresh.TOKEN_HEADER: "x" * 64})
    assert r.status_code == 200
    assert not r.json()["options"].get("sample")
