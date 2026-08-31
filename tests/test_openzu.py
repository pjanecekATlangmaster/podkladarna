from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from app.pipeline.fetch_openzu import FetchError, parse_bbox, query_sm5_sheets


def test_parse_bbox_ok():
    assert parse_bbox("14.4,50.0,14.5,50.1") == (14.4, 50.0, 14.5, 50.1)


def test_parse_bbox_rejects_outside_cz():
    with pytest.raises(FetchError, match="mimo Česko"):
        parse_bbox("0,0,1,1")


def test_parse_bbox_rejects_inverted():
    with pytest.raises(FetchError, match="inverted"):
        parse_bbox("14.5,50.1,14.4,50.0")


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
    assert "PRAH77" in body["label"]


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
            "source_mode": "map",
            "bbox": "14.40,50.08,14.42,50.09",
            "run_vectors": "false",
        },
    )
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["options"]["source_mode"] == "map"
    assert job["options"]["sm5_sheets"] == ["PRAH77"]
    assert job["options"]["bbox_wgs84"][0] == pytest.approx(14.40)
    log = client.get(f"/api/jobs/{job['id']}/log").json()["lines"]
    text = "\n".join(x["line"] for x in log)
    assert "rezim=map" in text
    assert "PRAH77" in text


def test_create_job_map_without_bbox(client):
    r = client.post(
        "/api/jobs",
        data={"name": "bez-vyrezu", "preset_id": "sprint_2m", "source_mode": "map"},
    )
    assert r.status_code == 400
    assert "bbox" in r.json()["detail"].lower() or "výřez" in r.json()["detail"].lower()


def test_query_http_error(monkeypatch):
    def boom(*a, **k):
        raise HTTPError("http://x", 500, "fail", hdrs={}, fp=BytesIO())

    monkeypatch.setattr("app.pipeline.fetch_openzu.urllib.request.urlopen", boom)
    with pytest.raises(FetchError, match="HTTP 500"):
        query_sm5_sheets(14.4, 50.08, 14.42, 50.09)
