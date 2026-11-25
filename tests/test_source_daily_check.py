"""Třídění výpadků v denní kontrole zdrojů (scripts/source_daily_check.py)."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "source_daily_check", Path(__file__).resolve().parent.parent / "scripts" / "source_daily_check.py"
)
sdc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sdc)


def _s(ok=True, code=None, detail=None, planned=False):
    return {"ok": ok, "code": code, "detail": detail, "planned": planned, "down_since": None}


@pytest.mark.parametrize(
    "cloud, prod, expected",
    [
        (_s(), _s(), "ok"),
        (_s(False, None, "URLError: reset"), _s(), "jen_cloud"),
        (_s(), _s(False, None, "URLError: timed out"), "sit_a"),
        (_s(False, 429, "HTTP 429"), _s(), "rate_limit"),
        (_s(False, 404, "HTTP 404"), _s(False, 404, "HTTP 404"), "url"),
        (_s(False, 403, "HTTP 403"), _s(False, 403, "HTTP 403"), "pristup"),
        (_s(False, 503, "HTTP 503"), _s(False, 503, "plánovaná odstávka, HTTP 503", True), "planovane"),
        (_s(False, None, "certificate verify failed"), _s(False, None, "certificate verify failed"), "cert"),
        (_s(False, 500, "HTTP 500"), _s(False, 500, "HTTP 500"), "prechodne"),
        (_s(False, 404, "HTTP 404"), None, "url"),
        (None, None, "nezname"),
    ],
)
def test_classify_today(cloud, prod, expected):
    assert sdc.classify_today(cloud, prod) == expected


@pytest.mark.parametrize(
    "today, past_down, expected",
    [
        ("prechodne", [False, False], ("prechodne", 1)),
        ("prechodne", [True, True], ("trvale", 3)),
        ("prechodne", [True, False, True, False], ("opakovane", 1)),
        ("ok", [True, True], ("ok", 0)),
        ("url", [True], ("url", 2)),
    ],
)
def test_classify_history(today, past_down, expected):
    history = [{"down": {"x": {"cls": "prechodne"}} if d else {}} for d in past_down]
    assert sdc.classify(today, "x", history) == expected


@pytest.mark.parametrize(
    "job, cat",
    [
        ({"traceback": "Traceback\nRuntimeError: Výšková data (fr_lidarhd) nejsou k dispozici: HTTP Error 400"}, "chyba"),
        ({"source_outage": "fr_lidarhd", "error": "výpadek"}, "vypadek_zdroje"),
        ({"user_sources": True, "error": "neplatný soubor"}, "vlastni_data"),
        ({"traceback": "Traceback\nMemoryError"}, "pamet"),
        ({"log": ["CHYBA: [Errno 28] No space left on device"]}, "disk"),
    ],
)
def test_build_signature_category(job, cat):
    assert sdc.build_signature(job)[0] == cat


def test_group_builds_merges_same_cause_and_marks_new():
    jobs = [
        {"id": "a1", "status": "failed", "country": "FR", "traceback": "T\nRuntimeError: HTTP Error 400 job 123"},
        {"id": "a2", "status": "failed", "country": "FR", "traceback": "T\nRuntimeError: HTTP Error 400 job 456"},
        {"id": "w1", "status": "done_with_errors", "log": ["⚠ chybí ortofoto"]},
    ]
    groups = sdc.group_builds(jobs, [])
    assert len(groups) == 1 and groups[0]["jobs"] == ["a1", "a2"] and groups[0]["new"]
    again = sdc.group_builds(jobs, [{"groups": {groups[0]["id"]: {}}}])
    assert not again[0]["new"] and again[0]["days_seen"] == 1
