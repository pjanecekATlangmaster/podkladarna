from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from app.pipeline.fetch_openzu import (
    FetchError,
    bbox_exceeds_limit,
    bbox_size_km,
    parse_bbox,
    query_sm5_sheets,
)


def test_parse_bbox_ok():
    assert parse_bbox("14.4,50.0,14.5,50.1") == (14.4, 50.0, 14.5, 50.1)


def test_parse_bbox_rejects_outside_cz():
    with pytest.raises(FetchError, match="mimo Česko"):
        parse_bbox("0,0,1,1")
    with pytest.raises(FetchError, match="mimo Česko"):
        parse_bbox("16.35,48.18,16.40,48.22")
    with pytest.raises(FetchError, match="mimo Česko"):
        parse_bbox("19.5,50.0,19.6,50.1")


def test_parse_bbox_rejects_inverted():
    with pytest.raises(FetchError, match="inverted"):
        parse_bbox("14.5,50.1,14.4,50.0")


def test_bbox_size_small_ok():
    width_km, height_km = bbox_size_km(14.40, 50.08, 14.42, 50.09)
    assert 1.0 < width_km < 2.0
    assert 0.8 < height_km < 1.5
    assert not bbox_exceeds_limit(14.40, 50.08, 14.42, 50.09)


def test_bbox_size_over_5km():
    # ~14 × 22 km, pořád v obálce Česka
    assert bbox_exceeds_limit(14.0, 49.5, 14.2, 49.7)


def test_estimate_minutes_accounts_for_dmpok(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from app import settings
    from app.download_cache import write_meta
    from app.pipeline.fetch_openzu import dmpok_cached_mapnoms, estimate_minutes, estimate_note

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    names = ["PRAH03", "PRAH04", "PRAH13", "PRAH14"]
    assert estimate_minutes(names) == 5 * 2 + 4 * 5

    folder = tmp_path / "lidar" / "sm5" / "PRAH03"
    folder.mkdir(parents=True)
    (folder / "DMPOK.laz").write_bytes(b"x" * 2000)
    write_meta(folder, downloaded_at=datetime.now(timezone.utc).isoformat())
    assert dmpok_cached_mapnoms(names) == {"PRAH03"}
    assert estimate_minutes(names) == 5 * 2 + 3 * 5
    assert estimate_note(names).startswith("Stahuje se DMP OK")

    for mapnom in names:
        f = tmp_path / "lidar" / "sm5" / mapnom
        f.mkdir(parents=True, exist_ok=True)
        (f / "DMPOK.laz").write_bytes(b"x" * 2000)
        write_meta(f, downloaded_at=datetime.now(timezone.utc).isoformat())
    assert estimate_minutes(names) == 5 * 2
    assert estimate_note(names) == "DMP OK v cache."


def test_query_sm5_sheets_parses_arcgis(monkeypatch):
    payload = {
        "features": [
            {"attributes": {"MAPNOM": "PRAH77", "MAPNAME": "Praha 7-7"}},
            {"attributes": {"MAPNOM": "prah77", "MAPNAME": "dup"}},
            {"attributes": {"MAPNOM": "PRAH78", "MAPNAME": "Praha 7-8"}},
        ]
    }

    class FakeResp:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("app.pipeline.fetch_openzu.urllib.request.urlopen", lambda *a, **k: FakeResp())
    sheets = query_sm5_sheets(14.4, 50.08, 14.42, 50.09)
    assert [s["mapnom"] for s in sheets] == ["PRAH77", "PRAH78"]


def test_api_sheets(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main,
        "query_sm5_sheets",
        lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}],
    )
    r = client.get("/api/sheets", params={"bbox": "14.40,50.08,14.42,50.09"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["sheets"][0]["mapnom"] == "PRAH77"
    assert body["estimate_minutes"] == 9
    assert body["estimate_note"]
    assert "PRAH77" in body["label"]
    assert body["too_large"] is False
    assert body["max_km"] == 5.0
    assert body["width_km"] < 5
    assert body["height_km"] < 5


def test_api_sheets_rejects_bad_bbox(client):
    r = client.get("/api/sheets", params={"bbox": "1,2,3"})
    assert r.status_code == 400


def test_create_job_from_map_bbox(client, monkeypatch):
    import app.db as db
    import app.main as main

    monkeypatch.setattr(
        main,
        "query_sm5_sheets",
        lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}],
    )
    monkeypatch.setattr(
        main.worker,
        "enqueue",
        lambda job_id: db.update_job(job_id, status="queued", phase="waiting"),
    )
    r = client.post(
        "/api/jobs",
        data={
            "name": "sance-mapa",
            "preset_id": "sprint_2m",
            "bbox": "14.40,50.08,14.42,50.09",
        },
    )
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["options"]["source_mode"] == "map"
    assert job["options"]["run_vectors"] is True
    assert job["options"]["sm5_sheets"] == ["PRAH77"]
    assert job["options"]["bbox_wgs84"][0] == pytest.approx(14.40)
    log = client.get(f"/api/jobs/{job['id']}/log").json()["lines"]
    text = "\n".join(x["line"] for x in log)
    assert "listy=PRAH77" in text


def test_api_sheets_too_large(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main,
        "query_sm5_sheets",
        lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}],
    )
    r = client.get("/api/sheets", params={"bbox": "14.0,49.5,14.2,49.7"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["too_large"] is True
    assert body["too_large_reason"] == "size"
    assert body["estimate_minutes"] is None
    assert "5" in body["hint"]
    assert "moc velký" in body["hint"]


def test_create_job_map_too_large(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main,
        "query_sm5_sheets",
        lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}],
    )
    r = client.post(
        "/api/jobs",
        data={
            "name": "moc-velky",
            "preset_id": "sprint_2m",
            "source_mode": "map",
            "bbox": "14.0,49.5,14.2,49.7",
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"].lower()
    assert "moc velk" in detail
    assert "5" in r.json()["detail"]


def test_create_job_map_without_bbox(client):
    r = client.post(
        "/api/jobs",
        data={"name": "bez-vyrezu", "preset_id": "sprint_2m"},
    )
    assert r.status_code == 400
    assert "bbox" in r.json()["detail"].lower() or "výřez" in r.json()["detail"].lower()


def test_create_job_rejects_zabaged_upload(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main,
        "query_sm5_sheets",
        lambda *a, **k: [{"mapnom": "PRAH77", "name": "Praha 7-7"}],
    )
    r = client.post(
        "/api/jobs",
        data={
            "name": "zabaged-upload",
            "preset_id": "sprint_2m",
            "bbox": "14.40,50.08,14.42,50.09",
        },
        files=[
            ("zabaged_file", ("Zabaged.zip", b"PK\x03\x04", "application/zip")),
        ],
    )
    assert r.status_code == 400
    assert "upload" in r.json()["detail"].lower()


def test_query_http_error(monkeypatch):
    def boom(*a, **k):
        raise HTTPError("http://x", 500, "fail", hdrs={}, fp=BytesIO())

    monkeypatch.setattr("app.pipeline.fetch_openzu.urllib.request.urlopen", boom)
    with pytest.raises(FetchError, match="HTTP 500"):
        query_sm5_sheets(14.4, 50.08, 14.42, 50.09)


def test_cached_dmp_laz_prefers_dmpok(tmp_path, monkeypatch):
    from app import settings
    from app.download_cache import write_meta
    from app.pipeline.fetch_openzu import _cached_dmp_laz

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    folder = tmp_path / "lidar" / "sm5" / "PRAH77"
    folder.mkdir(parents=True)
    dmpok = folder / "DMPOK.laz"
    dmpok.write_bytes(b"x" * 2000)
    write_meta(folder, downloaded_at="2026-01-01T00:00:00+00:00")

    got = _cached_dmp_laz("PRAH77", log=None)
    assert got == dmpok


def test_cached_dmp_laz_falls_back_to_dmp1g(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from app import settings
    from app.download_cache import write_meta
    from app.pipeline.fetch_openzu import FetchError, _cached_dmp_laz

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    folder = tmp_path / "lidar" / "sm5" / "PRAH77"
    folder.mkdir(parents=True)
    dmp1g = folder / "DMP1G.laz"
    dmp1g.write_bytes(b"y" * 2000)
    write_meta(folder, downloaded_at=datetime.now(timezone.utc).isoformat())

    def fail_dmpok(*args, **kwargs):
        raise FetchError("HTTP 404")

    monkeypatch.setattr("app.pipeline.fetch_openzu._cached_laz", fail_dmpok)
    got = _cached_dmp_laz("PRAH77", log=None)
    assert got == dmp1g
