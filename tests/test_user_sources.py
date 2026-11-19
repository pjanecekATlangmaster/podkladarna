"""Vlastní výšková data zadavatele (app.user_sources): kontrola souborů, životnost a API jen mimo Česko."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pytest

from app import user_sources
from app.pipeline import country_profile, elevation_raster

LYON = (4.825, 45.754, 4.840, 45.762)
TIFF = b"II*\x00" + b"\x00" * 64
UTM31_WKT = None


def _wkt(epsg: int) -> str:
    from pyproj import CRS

    return CRS.from_epsg(epsg).to_wkt()


def _gdalinfo(**over):
    """Výstup ``gdalinfo -json`` pro rastr 1 m pokrývající Lyon (EPSG:2154)."""
    x0, y0, x1, y1 = user_sources.bbox_in_crs(LYON, _wkt(2154))
    info = {
        "driverShortName": "GTiff",
        "files": ["dtm.upload"],
        "size": [int(x1 - x0) + 400, int(y1 - y0) + 400],
        "geoTransform": [x0 - 200, 1.0, 0.0, y1 + 200, 0.0, -1.0],
        "coordinateSystem": {"wkt": _wkt(2154)},
        "bands": [{"computedMin": 160.0, "computedMax": 310.0}],
    }
    info.update(over)
    return info


def _stub_gdal(monkeypatch, info):
    class _Res:
        stdout = json.dumps(info)

    monkeypatch.setattr(user_sources, "_gdal_tool", lambda name: name)
    monkeypatch.setattr(user_sources, "_run_gdal", lambda cmd: _Res())


def test_save_upload_checks_header_and_size(tmp_path):
    dest = tmp_path / "a.upload"
    assert user_sources.save_upload(io.BytesIO(TIFF), dest, max_bytes=1000) == len(TIFF)
    with pytest.raises(user_sources.UserSourceError) as exc:
        user_sources.save_upload(io.BytesIO(b"<VRTDataset>" + b"x" * 10), tmp_path / "b.upload", max_bytes=1000)
    assert exc.value.key == "api.user_sources.not_geotiff"
    with pytest.raises(user_sources.UserSourceError) as exc:
        user_sources.save_upload(io.BytesIO(TIFF + b"\x00" * 2_000_000), tmp_path / "c.upload", max_bytes=1_000_000)
    assert exc.value.key == "api.user_sources.too_big" and not (tmp_path / "c.upload").exists()


@pytest.mark.parametrize(
    "over, key",
    [
        ({}, None),
        ({"driverShortName": "VRT"}, "not_geotiff"),  # VRT by četl cizí soubory / síť
        ({"files": ["dtm.upload", "dtm.upload.ovr"]}, "not_geotiff"),  # žádné soubory vedle
        ({"size": [30_000, 100]}, "dimensions"),
        ({"geoTransform": [0, 1.0, 0.2, 0, 0.0, -1.0]}, "rotated"),
        ({"coordinateSystem": {"wkt": ""}}, "no_crs"),
        ({"coordinateSystem": {"wkt": "EPSG:4326"}}, "not_projected"),
        ({"bands": [{"computedMin": -32768.0, "computedMax": 300.0}]}, "values"),  # NoData bez značky
        ({"bands": [{"computedMin": None, "computedMax": None}]}, "values"),
    ],
)
def test_inspect_rejects_bad_rasters(monkeypatch, tmp_path, over, key):
    info = _gdalinfo(**over)
    if over.get("coordinateSystem", {}).get("wkt") == "EPSG:4326":
        info["coordinateSystem"]["wkt"] = _wkt(4326)
    _stub_gdal(monkeypatch, info)
    path = tmp_path / "dtm.upload"
    path.write_bytes(TIFF)
    if key is None:
        result, meta = user_sources.inspect(path, LYON)
        assert result.epsg == "EPSG:2154" and result.coverage == pytest.approx(1.0)
        assert "dtm" not in result.log_text()  # do logu jen technické údaje
        return
    with pytest.raises(user_sources.UserSourceError) as exc:
        user_sources.inspect(path, LYON)
    assert exc.value.key == f"api.user_sources.{key}"


def test_inspect_rejects_low_coverage_and_pixel(monkeypatch, tmp_path):
    path = tmp_path / "dtm.upload"
    path.write_bytes(TIFF)
    info = _gdalinfo()
    info["size"] = [info["size"][0] // 3, info["size"][1]]
    _stub_gdal(monkeypatch, info)
    with pytest.raises(user_sources.UserSourceError) as exc:
        user_sources.inspect(path, LYON)
    assert exc.value.key == "api.user_sources.coverage" and exc.value.params["share"] < 90
    info = _gdalinfo()
    info["geoTransform"] = [*info["geoTransform"][:1], 10.0, 0.0, info["geoTransform"][3], 0.0, -10.0]
    _stub_gdal(monkeypatch, info)
    with pytest.raises(user_sources.UserSourceError) as exc:
        user_sources.inspect(path, LYON)
    assert exc.value.key == "api.user_sources.resolution"


def test_coverage_share():
    assert user_sources.coverage_share((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(0.5)
    assert user_sources.coverage_share((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0


def test_lifecycle_attach_cleanup_and_prune(data_dir):
    from app import settings

    staging = user_sources.new_staging_dir()
    for kind in user_sources.KINDS:
        (staging / kind).mkdir()
        (staging / kind / f"{kind}.tif").write_bytes(TIFF)
    job_dir = settings.JOBS_DIR / "abc123"
    (job_dir / "work" / "dem" / "src").mkdir(parents=True)
    dest = user_sources.attach(staging, job_dir)
    assert (dest / "dtm" / "dtm.tif").is_file() and not staging.exists()
    # Zdroj výšek jobu míří do složky jobu, nikdy do cache.
    assert "cache" not in str(dest.relative_to(settings.DATA_ROOT))
    assert user_sources.cleanup_job(job_dir) is True
    assert not dest.exists() and not (job_dir / "work" / "dem" / "src").exists()
    assert user_sources.cleanup_job(job_dir) is False

    old = user_sources.new_staging_dir()
    fresh = user_sources.new_staging_dir()
    past = time.time() - user_sources.STAGING_MAX_AGE_S - 10
    os.utime(old, (past, past))
    assert user_sources.prune_staging() == 1
    assert not old.exists() and fresh.exists()

    # Pojistka: data jobu, který už neběží (nebo v DB není), denní úklid smaže.
    (job_dir / user_sources.SOURCES_DIR_NAME / "dtm").mkdir(parents=True)
    assert user_sources.prune_finished_jobs() == 1


def test_user_upload_provider_reads_job_dir(data_dir, monkeypatch):
    from app import settings

    job_dir = settings.JOBS_DIR / "abc123"
    seen = {}
    monkeypatch.setattr(
        elevation_raster,
        "local_provider",
        lambda spec: (lambda b, o, log=None: seen.setdefault("spec", spec)),
    )
    elevation_raster.PROVIDERS["user_upload"](None, job_dir / "work" / "dem" / "src", None)
    assert Path(seen["spec"].split(":", 1)[1]) == (job_dir / "user_sources").resolve()


@pytest.mark.parametrize("country, expected", [("FR", "user_upload"), ("CZ", "cz_openzu")])
def test_elevation_source_only_outside_cz(monkeypatch, country, expected):
    monkeypatch.setenv("COUNTRIES_BETA", "FR")
    opts = {"country": country, "user_sources": True}
    assert country_profile.elevation_source(opts) == expected
    assert country_profile.elevation_source({"country": country}) != "user_upload"


def _post(client, files, **data):
    body = {"preset_id": "sprint_2m", "output_mode": "png", "name": "lyon", "bbox": ",".join(map(str, LYON))}
    body.update(data)
    return client.post("/api/jobs", data=body, files=files)


FILES = [("user_dtm", ("a.tif", TIFF, "image/tiff")), ("user_dsm", ("b.tif", TIFF, "image/tiff"))]


def test_api_rejects_when_disabled_or_in_czech(client, monkeypatch):
    import app.main as main

    monkeypatch.setenv("COUNTRIES_BETA", "FR")
    monkeypatch.setenv("SITES", "testserver=cs:free:*")
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setitem(elevation_raster.COVERAGE, "fr_lidarhd", lambda _b: 1.0)
    monkeypatch.setattr(elevation_raster, "_COVERAGE_CACHE", {})
    monkeypatch.setattr(user_sources, "enabled", lambda: False)
    r = _post(client, FILES, user_sources_consent="1", private="1", notify_email="a@b.cz")
    assert r.status_code == 400 and r.json().get("code") == "api.job.upload_unsupported"


def test_api_creates_private_job_and_skips_coverage_block(client, monkeypatch):
    import app.main as main

    monkeypatch.setenv("COUNTRIES_BETA", "FR")
    monkeypatch.setenv("SITES", "testserver=cs:free:*")
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setitem(elevation_raster.COVERAGE, "fr_lidarhd", lambda _b: 0.1)  # bez vlastních dat by odmítla
    monkeypatch.setattr(elevation_raster, "_COVERAGE_CACHE", {})
    monkeypatch.setattr(user_sources, "enabled", lambda: True)
    monkeypatch.setattr(main.worker, "enqueue", lambda *_a, **_k: None)
    monkeypatch.setattr(main.worker, "queue_position", lambda *_a, **_k: 0)

    def fake_prepare(staging, bbox):
        info = user_sources.RasterInfo("EPSG:2154", 1.0, 1200, 900, 1.0, 3.0)
        for kind in user_sources.KINDS:
            assert (staging / f"{kind}.upload").read_bytes() == TIFF
            (staging / kind).mkdir()
            (staging / kind / f"{kind}.tif").write_bytes(TIFF)
        return {kind: info for kind in user_sources.KINDS}

    monkeypatch.setattr(user_sources, "prepare", fake_prepare)

    assert _post(client, FILES, private="1", notify_email="a@b.cz").json()["code"] == "api.user_sources.consent_required"
    assert _post(client, FILES[:1], user_sources_consent="1").json()["code"] == "api.user_sources.both_required"
    assert _post(client, FILES, user_sources_consent="1").json()["code"] == "api.user_sources.private_required"

    r = _post(client, FILES, user_sources_consent="1", private="1", notify_email="a@b.cz")
    assert r.status_code == 200, r.text
    job = r.json()
    from app import db, settings

    full = db.get_job(job["id"])
    assert full["options"]["user_sources"] is True and full["options"]["private"] is True
    assert "area_probe" not in full["options"]
    assert (settings.JOBS_DIR / job["id"] / "user_sources" / "dsm" / "dsm.tif").is_file()
    log = (settings.JOBS_DIR / job["id"] / "log.txt").read_text(encoding="utf-8")
    assert "vlastní data zadavatele" in log and "a.tif" not in log
    assert not any(user_sources.staging_root().iterdir())
    # Druhý stejný výřez se se soukromými daty neslučuje.
    r2 = _post(client, FILES, user_sources_consent="1", private="1", notify_email="a@b.cz")
    assert r2.status_code == 200 and r2.json()["id"] != job["id"]


def test_api_reports_invalid_file(client, monkeypatch):
    import app.main as main

    monkeypatch.setenv("COUNTRIES_BETA", "FR")
    monkeypatch.setenv("SITES", "testserver=cs:free:*")
    monkeypatch.setattr(main, "check_create_job", lambda *_a, **_k: None)
    monkeypatch.setitem(elevation_raster.COVERAGE, "fr_lidarhd", lambda _b: 1.0)
    monkeypatch.setattr(elevation_raster, "_COVERAGE_CACHE", {})
    monkeypatch.setattr(user_sources, "enabled", lambda: True)
    bad = [("user_dtm", ("a.vrt", b"<VRTDataset/>", "text/xml")), FILES[1]]
    r = _post(client, bad, user_sources_consent="1", private="1", notify_email="a@b.cz")
    assert r.status_code == 400 and r.json()["code"] == "api.user_sources.not_geotiff"
    assert "DTM" in r.json()["detail"]
    assert not any(user_sources.staging_root().iterdir())


def test_job_worker_always_cleans_up(data_dir, monkeypatch):
    from app import job_worker, settings

    job_dir = settings.JOBS_DIR / "abc123"
    (job_dir / "user_sources" / "dtm").mkdir(parents=True)
    monkeypatch.setattr(job_worker, "JOBS_DIR", settings.JOBS_DIR)
    monkeypatch.setattr(job_worker, "_run_job", lambda _id: 1)
    monkeypatch.setattr(job_worker.db, "append_log", lambda *_a: None)
    assert job_worker.run_job("abc123") == 1
    assert not (job_dir / "user_sources").exists()
