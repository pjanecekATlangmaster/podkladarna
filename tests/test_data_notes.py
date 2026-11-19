"""Upozornění na stáří a druh dat země před buildem (``app.data_notes``)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import data_notes, i18n
from app.countries import load_countries

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "code, keys",
    [
        ("CZ", []),  # český build beze změny: žádná hláška
        ("BE", ["old"]),
        ("IT", ["old"]),
        ("FI", ["coarse"]),
        ("PL", ["photo"]),
        ("DE-BY", ["photo"]),
        ("DE-BB", ["photo"]),
        ("DE-BW", ["photo"]),
        ("LU", []),
        ("NL", []),
    ],
)
def test_note_keys(code, keys):
    assert data_notes.note_keys(load_countries()[code]) == keys


def test_catalog_keys_and_params():
    cs = json.loads((ROOT / "configs/i18n/cs.json").read_text(encoding="utf-8"))
    en = json.loads((ROOT / "configs/i18n/en.json").read_text(encoding="utf-8"))
    keys = [f"api.data_note.{k}" for k in (*data_notes.NOTE_KEYS, *data_notes.FOREST_ONLY, "summary", "summary_years")]
    keys += ["web.js.data_note.lead", "web.js.data_note.tail"]
    for key in keys:
        assert key in cs and key in en, key
        assert set(re.findall(r"{\w+}", cs[key])) == set(re.findall(r"{\w+}", en[key])), key
    # Každý klíč z countries.yaml má text.
    for country in load_countries().values():
        for key in country.data_notes:
            assert key in data_notes.NOTE_KEYS, (country.code, key)


def test_probe_notes_languages_and_sprint():
    be = load_countries()["BE"]
    info = i18n.localize(data_notes.probe_notes(be), "en")
    assert "2013–2015" in info["summary"] and "2013–2015" in info["notes"][0]["text"]
    assert info["notes"][0]["forest_only"] is False
    assert data_notes.probe_notes(load_countries()["CZ"]) is None
    pl = load_countries()["PL"]
    assert data_notes.text_lines(pl, "cs", sprint=True) == []
    assert "snímků" in data_notes.text_lines(pl, "cs")[0]
    assert "aerial" in data_notes.text_lines(pl, "en")[0]


def test_meta_lines_cz_and_old_meta_empty():
    assert data_notes.meta_lines({"country": "CZ", "preset_id": "les_5m"}, "cs") == []
    assert data_notes.meta_lines({}, "en") == []
    assert data_notes.meta_lines({"country": "IT", "preset_id": "sprint_2m"}, "en")[0].startswith("Age of the data (2014)")


def test_area_probe_carries_notes():
    from app import area_probe

    it = load_countries()["IT"]
    out = area_probe.probe(load_countries()["CZ"], (14.40, 50.05, 14.41, 50.06), deadline_s=0.01, include_dtm=False)
    assert out["data_notes"] is None
    assert data_notes.probe_notes(it)["notes"][0]["key"] == "old"


def test_omap_notes(monkeypatch):
    from app.pipeline import build_oom_map
    from app.pipeline.country_profile import JOB_COUNTRY_ENV

    monkeypatch.setenv(JOB_COUNTRY_ENV, "CZ")
    cz = build_oom_map._notes("cs", "x", "101", "102")
    assert cz.endswith("[rok]") or "Mapa z tohoto podkladu musí uvést" in cz.splitlines()[-1]
    monkeypatch.setenv(JOB_COUNTRY_ENV, "FI")
    fi = build_oom_map._notes("en", "x", "101", "102", sprint=True)
    assert fi.splitlines()[-1].startswith("Terrain has only 2 m resolution")
