"""Vyhledání balíků DTM kraje (DMVS) – bez sítě."""

from __future__ import annotations

import json
import time
import urllib.error

import pytest

import app.pipeline.dtm_sources as ds

SAMPLE = """\
https://isdtmk.dtm-praha-sck.cz/api/istem/vydej/download/path/803ca148-e41c-4f3a-9ee7-40171e36fd2c/VS_CZ010_MOMC_500143_20260930_ZPS_OPL.jvf.zip
https://isdtmk.dtm-praha-sck.cz/api/istem/vydej/download/path/2136bcd2-a966-4aee-a0fa-1183b548b313/VS_CZ010_MOMC_500143_20260930_ZPS_KR.jvf.zip
https://isdtmk.dtm-praha-sck.cz/api/istem/vydej/download/path/73308c98-7334-4482-baed-742c86a66dbc/VS_CZ010_MOMC_500143_20260930_ZPS_PB.jvf.zip
https://isdtmk.dtm-praha-sck.cz/api/istem/vydej/download/path/aaaa/VS_CZ010_MOMC_500143_20260831_ZPS_KR.jvf.zip
https://isdtmk.dtm-praha-sck.cz/api/istem/vydej/download/path/a1b7bc2a/VS_CZ010_MOMC_500119_20260930_ZPS_OPL.jvf.zip
https://isdtmk.dtm-praha-sck.cz/api/istem/vydej/download/path/36f20a84/VS_CZ010_MOMC_500119_20260930_ZPS_KR.jvf.zip
https://isdtmk.dtm-praha-sck.cz/api/istem/vydej/download/path/c8a01e32/VS_CZ020_OBEC_531057_20260930_ZPS_OPL.jvf.zip
https://isdtmk.dtm-praha-sck.cz/api/istem/vydej/download/path/7ac287cf/VS_CZ020_OBEC_531057_20260930_ZPS_KR.jvf.zip
https://isdtmk.dtm-praha-sck.cz/api/istem/vydej/download/path/pb/VS_CZ020_OBEC_531057_20260930_DI.jvf.zip
https://neco.example/smetí.txt
"""

PRAHA_UNITS = [
    ds.DtmUnit("MOMC", 500119, "Praha 4", "Hlavní město Praha"),
    ds.DtmUnit("MOMC", 500143, "Praha 5", "Hlavní město Praha"),
    ds.DtmUnit("OBEC", 554782, "Praha", "Hlavní město Praha"),
]


def test_parse_url_list_extracts_fields_and_skips_garbage():
    pk = ds.parse_url_list(SAMPLE)
    assert len(pk) == 9  # řádek se smetím se přeskočí
    first = pk[0]
    assert (first.kraj, first.unit_kind, first.unit_code) == ("CZ010", "MOMC", 500143)
    assert (first.date, first.set_name, first.fmt) == ("20260930", "ZPS_OPL", "jvf")
    assert first.filename == "VS_CZ010_MOMC_500143_20260930_ZPS_OPL.jvf.zip"


def test_latest_packages_picks_newest_and_prefers_momc():
    pk = ds.parse_url_list(SAMPLE)
    got = ds.latest_packages(pk, PRAHA_UNITS)
    keys = {(p.unit_code, p.set_name, p.date) for p in got}
    # starší vydání (20260831) se nepoužije, obec Praha se přeskočí (data jsou po MČ)
    assert keys == {
        (500119, "ZPS_OPL", "20260930"),
        (500119, "ZPS_KR", "20260930"),
        (500143, "ZPS_OPL", "20260930"),
        (500143, "ZPS_KR", "20260930"),
    }


def test_latest_packages_obec_without_momc_and_sets_filter():
    pk = ds.parse_url_list(SAMPLE)
    units = [ds.DtmUnit("OBEC", 531057, "Beroun", "Středočeský kraj")]
    got = ds.latest_packages(pk, units, sets=("ZPS_KR",))
    assert [(p.unit_kind, p.unit_code, p.set_name) for p in got] == [("OBEC", 531057, "ZPS_KR")]
    assert ds.latest_packages(pk, [ds.DtmUnit("OBEC", 1, "Nikde")]) == []
    assert ds.latest_packages(pk, units, fmt="shp") == []


def test_region_code_for_name_ignores_case_and_diacritics():
    regions = [
        {"kod": "CZ010", "nazev": "HLAVNÍ MĚSTO PRAHA"},
        {"kod": "CZ020", "nazev": "Středočeský kraj"},
        {"kod": "CZ063", "nazev": "Kraj Vysočina"},
    ]
    assert ds.region_code_for_name("Hlavní město Praha", regions) == "CZ010"
    assert ds.region_code_for_name("STREDOCESKY KRAJ", regions) == "CZ020"
    assert ds.region_code_for_name("Kraj Vysočina", regions) == "CZ063"
    assert ds.region_code_for_name("Wien", regions) is None


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(ds.settings, "DOWNLOADS_DIR", tmp_path)
    return tmp_path


def test_fetch_url_list_posts_filter_and_caches(downloads, monkeypatch):
    calls = []

    def fake_request(url, *, data=None, accept="*/*"):
        calls.append((url, json.loads(data.decode()) if data else None))
        return SAMPLE.encode("utf-8")

    monkeypatch.setattr(ds, "_request", fake_request)
    first = ds.fetch_url_list("CZ010", date_from="2026-09-30")
    assert len(first) == 9
    assert calls[0][0].endswith("/stavove/seznam-url")
    assert calls[0][1] == {"isDtmk": ["CZ010"], "datumOd": "2026-09-30"}
    # druhé volání jde z cache (žádná další síť)
    ds.fetch_url_list("CZ010", date_from="2026-09-30")
    assert len(calls) == 1
    # force = znovu na síť
    ds.fetch_url_list("CZ010", date_from="2026-09-30", force=True)
    assert len(calls) == 2


def test_fetch_url_list_refreshes_stale_cache_and_falls_back(downloads, monkeypatch):
    cache = downloads / "dtm" / "urls_CZ010.txt"
    cache.parent.mkdir(parents=True)
    cache.write_text(SAMPLE, encoding="utf-8")
    old = time.time() - 3 * 24 * 3600
    import os

    os.utime(cache, (old, old))
    calls = []

    def fail(url, *, data=None, accept="*/*"):
        calls.append(url)
        raise urllib.error.URLError("výpadek")

    monkeypatch.setattr(ds, "_request", fail)
    logs: list[str] = []
    got = ds.fetch_url_list("CZ010", log=logs.append)
    assert len(calls) == 1  # cache je stará → zkusí síť
    assert len(got) == 9  # …a při výpadku vrátí starou cache
    assert any("starší cache" in m for m in logs)
    # bez cache výpadek propadne
    cache.unlink()
    with pytest.raises(urllib.error.URLError):
        ds.fetch_url_list("CZ010")


def test_fetch_url_list_does_not_cache_empty_answer(downloads, monkeypatch):
    monkeypatch.setattr(ds, "_request", lambda *a, **k: b"")
    assert ds.fetch_url_list("CZ010") == []
    assert not (downloads / "dtm" / "urls_CZ010.txt").exists()


def test_find_packages_for_bbox_end_to_end(downloads, monkeypatch):
    monkeypatch.setattr(ds, "units_for_bbox", lambda bbox: PRAHA_UNITS)
    monkeypatch.setattr(
        ds, "fetch_regions", lambda: [{"kod": "CZ010", "nazev": "HLAVNÍ MĚSTO PRAHA"}]
    )
    monkeypatch.setattr(ds, "_request", lambda *a, **k: SAMPLE.encode("utf-8"))
    got = ds.find_packages_for_bbox((14.405, 50.046, 14.419, 50.054))
    assert {(p.unit_code, p.set_name) for p in got} == {
        (500119, "ZPS_OPL"),
        (500119, "ZPS_KR"),
        (500143, "ZPS_OPL"),
        (500143, "ZPS_KR"),
    }


def test_find_packages_for_bbox_outside_czechia_or_unknown_region(downloads, monkeypatch):
    monkeypatch.setattr(ds, "units_for_bbox", lambda bbox: [])
    assert ds.find_packages_for_bbox((16.35, 48.2, 16.37, 48.21)) == []
    monkeypatch.setattr(
        ds, "units_for_bbox", lambda bbox: [ds.DtmUnit("OBEC", 1, "X", "Neznámý kraj")]
    )
    monkeypatch.setattr(ds, "fetch_regions", lambda: [{"kod": "CZ010", "nazev": "HLAVNÍ MĚSTO PRAHA"}])
    logs: list[str] = []
    assert ds.find_packages_for_bbox((14, 50, 14.1, 50.1), log=logs.append) == []
    assert any("Neznámý kraj" in m for m in logs)

def test_download_package_caches_validates_and_cleans_up(downloads, monkeypatch):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x.jvf.xml", "<a/>")
    good = buf.getvalue()
    pkg = ds.parse_url_list(SAMPLE)[1]  # Praha 5 ZPS_KR
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        return Resp(good if len(calls) < 3 else b"neni zip")

    monkeypatch.setattr(ds.urllib.request, "urlopen", fake_urlopen)
    path = ds.download_package(pkg)
    assert path == ds.package_path(pkg) and path.read_bytes() == good
    assert ds.download_package(pkg) == path and len(calls) == 1  # z cache
    ds.download_package(pkg, force=True)
    assert len(calls) == 2
    # nevalidní odpověď: výjimka a žádný .part / poškozený soubor
    path.unlink()
    with pytest.raises(ValueError):
        ds.download_package(pkg)
    assert not path.exists() and not list(path.parent.glob("*.part"))
