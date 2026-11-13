from __future__ import annotations

import pytest

from app.pipeline.fetch_openzu import (
    REF_PNG_ESTIMATE_MINUTES,
    _map_minutes,
    estimate_georef_minutes,
    estimate_job_parts,
)

BBOX = [14.40, 50.08, 14.42, 50.09]


@pytest.mark.parametrize(
    "options, scale, dtm, share, georef, refs",
    [
        ({"map_scale": 4000, "map_types": ["les", "mtbo"]}, 4000, True, 1.0, False, False),
        ({"map_scale": 4000, "dtm_sprint": False}, 4000, False, 1.0, False, False),
        ({"map_scale": 10000, "map_types": ["les"]}, 10000, True, 0.5, False, False),
        ({"map_scale": 10000, "map_types": ["les", "mtbo"], "dtm_forest": False}, 10000, False, 1.0, False, False),
        ({"map_types": ["les", "mtbo"], "output_georef": True}, 10000, True, 1.0, True, False),
        ({"map_scale": 4000, "output_georef": True, "output_references": True}, 4000, True, 1.0, True, True),
    ],
)
def test_estimate_job_parts(options, scale, dtm, share, georef, refs):
    area = 2.0
    prep, maps = estimate_job_parts(options, area)
    # bez listů: 3,5 pevné + 1 stažení + 1,25 ořez + 0,5 · plocha
    assert prep == pytest.approx(3.5 + 1 + 1.25 + 0.5 * area + (REF_PNG_ESTIMATE_MINUTES if refs else 0))
    expected = _map_minutes(area, scale, dtm) * share
    if georef:
        expected += estimate_georef_minutes(area, scale) * share
    assert maps == pytest.approx(expected)


def _insert(conn, i, *, ratio=0.4, est_prep=10.0, est_maps=4.0, created="2999-12-01T00:00:00+00:00"):
    maps_s = est_maps * ratio * 60
    conn.execute(
        "INSERT INTO job_origin (job_id, created_at, status, usage_counted, build_seconds, "
        "maps_seconds, est_prep_min, est_maps_min) VALUES (?, ?, 'done', 1, ?, ?, ?, ?)",
        (f"j{i}", created, maps_s + est_prep * ratio * 60, maps_s, est_prep, est_maps),
    )


def test_create_job_stores_estimate_and_usage_stores_maps_seconds(data_dir):
    import app.db as db

    opts = {"bbox_wgs84": BBOX, "map_types": ["les"], "client_ip": "1.2.3.4"}
    job = db.create_job("a", "forest_10000", opts)
    reused = db.create_job("b", "forest_10000", {**opts, "reused_from": job["id"]})
    nobbox = db.create_job("c", "forest_10000", {"map_types": ["les"]})
    db.record_job_usage(
        job["id"], "done", {"total_seconds": 600, "steps": [{"label": "sestavení mapy a ZIP", "seconds": 120}]}
    )
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM job_origin WHERE job_id = ?", (job["id"],)).fetchone()
        assert row["est_prep_min"] > 0 and row["est_maps_min"] > 0
        assert row["maps_seconds"] == 120
        for other in (reused, nobbox):
            r = conn.execute("SELECT * FROM job_origin WHERE job_id = ?", (other["id"],)).fetchone()
            assert r["est_prep_min"] is None and r["est_maps_min"] is None


@pytest.mark.parametrize(
    "n_jobs, ratio, expected",
    [
        (4, 0.4, 1.0),  # málo dat: dnešní vzorec
        (6, 0.4, 0.4 * 1.15),
        (6, 0.05, 0.25 * 1.15),  # spodní mez
        (6, 3.0, 1.5),  # horní mez
    ],
)
def test_estimate_calibration(data_dir, n_jobs, ratio, expected):
    import app.db as db

    with db.connect() as conn:
        for i in range(n_jobs):
            _insert(conn, i, ratio=ratio)
        conn.commit()
    cal = db.estimate_calibration(now=db.datetime(3000, 1, 1, tzinfo=db.timezone.utc))
    assert cal["jobs"] == n_jobs
    assert cal["prep"] == pytest.approx(expected)
    assert cal["maps"] == pytest.approx(expected)


def test_estimate_calibration_ignores_old_and_can_be_disabled(data_dir, monkeypatch):
    import app.db as db

    with db.connect() as conn:
        for i in range(6):
            _insert(conn, i, created="2000-01-01T00:00:00+00:00")
        conn.commit()
    assert db.estimate_calibration()["jobs"] == 0  # starší než 90 dní
    with db.connect() as conn:
        for i in range(10, 16):
            _insert(conn, i, created=db._utcnow())
        conn.commit()
    db.reset_estimate_calibration_cache()
    assert db.estimate_calibration()["prep"] == pytest.approx(0.4 * 1.15)
    monkeypatch.setattr(db, "ESTIMATE_CALIBRATION", False)
    assert db.estimate_calibration() == {"prep": 1.0, "maps": 1.0, "jobs": 0}


def test_api_sheets_uses_calibration(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main, "query_sm5_sheets", lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}]
    )
    params = {"bbox": "14.40,50.08,14.42,50.09"}
    plain = client.get("/api/sheets", params=params).json()
    assert plain["estimate_calibration"] == {"prep": 1.0, "maps": 1.0, "jobs": 0}
    assert "posledních" not in (plain["estimate_note"] or "")

    monkeypatch.setattr(main.db, "estimate_calibration", lambda: {"prep": 0.5, "maps": 0.5, "jobs": 12})
    body = client.get("/api/sheets", params=params).json()
    assert body["estimate_calibration"]["jobs"] == 12
    assert body["estimate_minutes"] < plain["estimate_minutes"]
    assert body["estimate_base_minutes"]["plain"] < plain["estimate_base_minutes"]["plain"]
    assert body["estimate_map_minutes"]["10000"]["nodtm"] == pytest.approx(
        plain["estimate_map_minutes"]["10000"]["nodtm"] / 2, abs=0.01
    )
    assert body["estimate_note"].endswith("Odhad podle posledních 12 jobů na tomto serveru.")
