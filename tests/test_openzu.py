from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from app.pipeline.fetch_openzu import (
    FetchError,
    VECTOR_FETCH_BUFFER_M,
    bbox_exceeds_limit,
    bbox_size_km,
    expand_bbox_wgs84,
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


def test_bbox_area_limit_allows_five_by_five_shape():
    """Limit je plocha ~25 km² (obsah výřezu), ne tvrdý strop na jednu stranu."""
    from app.pipeline.fetch_openzu import MAX_BBOX_AREA_KM2, bbox_area_km2

    # ~14 × 22 km – daleko přes limit plochy
    assert bbox_exceeds_limit(14.0, 49.5, 14.2, 49.7)
    # Malý výřez OK
    assert not bbox_exceeds_limit(14.40, 50.08, 14.42, 50.09)

    lon0, lat0 = 14.42, 50.08

    def box(dlon, dlat):
        return (lon0 - dlon / 2, lat0 - dlat / 2, lon0 + dlon / 2, lat0 + dlat / 2)

    # ~4,9 × 4,9 km kolem WGS (obsah ≈ 24 km²)
    box5 = box(0.0685, 0.0440)
    w5, h5 = bbox_size_km(*box5)
    assert 4.7 <= w5 <= 5.1
    assert 4.7 <= h5 <= 5.1
    assert bbox_area_km2(*box5) <= MAX_BBOX_AREA_KM2
    assert not bbox_exceeds_limit(*box5)

    # ~6 × 6 km – přes 25 km² (dřívější limit 36 km²)
    box6 = box(0.0839, 0.0539)
    assert bbox_area_km2(*box6) > MAX_BBOX_AREA_KM2
    assert bbox_exceeds_limit(*box6)

    # ~7 × 3,4 km – pod 25 km²
    box73 = box(0.0979, 0.0306)
    assert bbox_area_km2(*box73) <= MAX_BBOX_AREA_KM2
    assert not bbox_exceeds_limit(*box73)


def test_bbox_size_over_area_limit():
    # ~14 × 22 km, pořád v obálce Česka
    assert bbox_exceeds_limit(14.0, 49.5, 14.2, 49.7)


def test_expand_bbox_wgs84_adds_buffer():
    bbox = (14.40, 50.08, 14.42, 50.09)
    expanded = expand_bbox_wgs84(bbox, VECTOR_FETCH_BUFFER_M)
    assert expanded[0] < bbox[0]
    assert expanded[1] < bbox[1]
    assert expanded[2] > bbox[2]
    assert expanded[3] > bbox[3]
    assert expand_bbox_wgs84(bbox, 0) == bbox


def test_estimate_minutes_accounts_for_dmpok(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from app import settings
    from app.download_cache import write_meta
    from app.pipeline.fetch_openzu import dmpok_cached_mapnoms, estimate_minutes, estimate_note

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    names = ["PRAH03", "PRAH04", "PRAH13", "PRAH14"]
    # 3,5 pevné + stažení 1/list + ořez 1,25/list + 2 mapy les a MTBO po 0,5 (plocha 0)
    assert estimate_minutes(names) == 14

    folder = tmp_path / "lidar" / "sm5" / "PRAH03"
    folder.mkdir(parents=True)
    (folder / "DMPOK.laz").write_bytes(b"x" * 2000)
    write_meta(folder, downloaded_at=datetime.now(timezone.utc).isoformat())
    assert dmpok_cached_mapnoms(names) == {"PRAH03"}
    assert estimate_minutes(names) == 13
    assert estimate_note(names).startswith("Stahuje se DMP OK")

    for mapnom in names:
        f = tmp_path / "lidar" / "sm5" / mapnom
        f.mkdir(parents=True, exist_ok=True)
        (f / "DMPOK.laz").write_bytes(b"x" * 2000)
        write_meta(f, downloaded_at=datetime.now(timezone.utc).isoformat())
    assert estimate_minutes(names) == 10
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
    # 1 nestažený list, ~1,6 km²: 3,5 + 1 + 1,25 + 0,5·1,6 + les a MTBO 2·(0,5 + 0,4·1,6) → 9 (+1 reference)
    assert body["estimate_minutes"] == 9
    assert body["estimate_minutes_with_refs"] == 9 + 1
    # bez map 7; GUI přičte mapy podle měřítka a DTM (sprint z DTM 0,5 + 2·1,6)
    assert body["estimate_base_minutes"] == {"plain": 7, "refs": 8}
    assert body["estimate_map_minutes"]["4000"]["dtm"] == pytest.approx(3.68, abs=0.01)
    assert body["estimate_map_minutes"]["10000"]["nodtm"] == pytest.approx(2.27, abs=0.01)
    assert body["estimate_georef_minutes"]["10000"] == 2
    assert body["estimate_georef_minutes"]["4000"] == 1
    assert body["estimate_note"]
    assert "PRAH77" in body["label"]
    assert body["too_large"] is False
    assert body["max_km"] == 5.0
    assert body["max_area_km2"] == 25.0
    assert body["max_sheets"] == 20
    assert body["width_km"] < 5
    assert body["height_km"] < 5


def test_estimate_minutes_adds_reference_pngs(tmp_path, monkeypatch):
    from app import settings
    from app.pipeline.fetch_openzu import REF_PNG_ESTIMATE_MINUTES, estimate_minutes

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    names = ["PRAH03"]
    base = estimate_minutes(names)
    with_refs = estimate_minutes(names, include_references=True)
    assert with_refs == base + REF_PNG_ESTIMATE_MINUTES


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
    assert "listy SM5 PRAH77" in text


def test_create_job_uses_client_sm5_sheets_without_arcgis(client, monkeypatch):
    import app.db as db
    import app.main as main

    def boom(*_a, **_k):
        raise AssertionError("query_sm5_sheets se nemá volat, když UI pošle listy")

    monkeypatch.setattr(main, "query_sm5_sheets", boom)
    monkeypatch.setattr(
        main.worker,
        "enqueue",
        lambda job_id: db.update_job(job_id, status="queued", phase="waiting"),
    )
    r = client.post(
        "/api/jobs",
        data={
            "name": "rychly",
            "preset_id": "sprint_2m",
            "bbox": "14.40,50.08,14.42,50.09",
            "sm5_sheets": "PRAH77,PRAH78",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["options"]["sm5_sheets"] == ["PRAH77", "PRAH78"]


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
    assert "25" in body["hint"]
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
    assert "25" in r.json()["detail"]


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
    from datetime import datetime, timezone

    from app import settings
    from app.download_cache import write_meta
    from app.pipeline.fetch_openzu import _cached_dmp_laz

    monkeypatch.setattr(settings, "DOWNLOADS_DIR", tmp_path)
    folder = tmp_path / "lidar" / "sm5" / "PRAH77"
    folder.mkdir(parents=True)
    dmpok = folder / "DMPOK.laz"
    dmpok.write_bytes(b"x" * 2000)
    # Čerstvá meta – stará by spustila live download z openzu (CI hang).
    write_meta(folder, downloaded_at=datetime.now(timezone.utc).isoformat())

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


def test_estimate_calibrated_on_nas_jobs(monkeypatch):
    """Kalibrace NAS (3.2.0): odhad mírně nad skutečností, ne dvojnásobek."""
    import app.pipeline.fetch_openzu as f

    def total(sheets, area, scale, use_dtm):
        names = [f"S{i}" for i in range(sheets)]
        monkeypatch.setattr(f, "dmpok_cached_mapnoms", lambda n: set(n))
        return f.estimate_minutes(names, area_km2=area, include_maps=False) + f.estimate_map_minutes(area, scale, use_dtm)

    # (odhad, skutečnost v min) → do +35 %
    for got, actual in (
        # sprint Final 2 (cf5b78724d57): příprava 16,7 min + mapa 26,8 min (3.2.0) + ZIP
        (total(6, 11.6, 4000, True), 44),
        # Retest 6 (9d6aa03b612c): iterace 23,5 min + ořez LiDAR ~6,5 min (Final 2, stejné listy)
        (total(6, 8.91, 10000, True), 30),
    ):
        assert 0.9 * actual <= got <= 1.35 * actual


def test_lidar_workers_from_cpus(monkeypatch):
    import app.pipeline.prepare_lidar as pl

    monkeypatch.delenv("PODKLADARNA_LIDAR_WORKERS", raising=False)
    for cpus, want in ((1, 2), (2, 2), (4, 3), (8, 7), (32, 8)):
        monkeypatch.setattr(pl, "available_cpus", lambda c=cpus: c)
        assert pl.lidar_workers() == want
    monkeypatch.setenv("PODKLADARNA_LIDAR_WORKERS", "5")
    assert pl.lidar_workers() == 5
