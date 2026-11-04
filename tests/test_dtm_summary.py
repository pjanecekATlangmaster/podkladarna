"""Souhrn DTM pro uživatele (dtm_summary): stavy, úroveň a klíčová slova vět."""

import json

import pytest

from app.pipeline import dtm_summary

SPRINT_ONLY = {"sprint": True, "les": False, "mtbo": False}
ALL_OFF = {"sprint": False, "les": False}
FOREST_OFF = {"les": False, "mtbo": False}


def _write(work, *, units=None, incomplete=(), fences=3, buildings=2):
    dtm = work / "dtm"
    dtm.mkdir(parents=True)
    data = {
        "packages": ["a.zip", "b.zip"],
        "incomplete_opl": list(incomplete),
        "fences": [{"code": "x", "coords": [[0, 0], [1, 1]]}] * fences,
    }
    if units is not None:
        data["units"] = units
    (dtm / "fences.json").write_text(json.dumps(data), encoding="utf-8")
    (dtm / "buildings.json").write_text(json.dumps({"polygons": [[]] * buildings}), encoding="utf-8")


def _unit(code, name, has_data=True, kind="OBEC"):
    return {"kind": kind, "code": code, "name": name, "has_data": has_data}


@pytest.mark.parametrize(
    "maps, dtm_on, setup, failed, state, level, words",
    [
        (ALL_OFF, False, None, False, "off", "info", ["vypnutá"]),
        (FOREST_OFF, False, None, False, "off", "info", ["automaticky"]),
        (FOREST_OFF, False, "vypnuto", False, "off", "info", ["vypnutá"]),
        (SPRINT_ONLY, True, None, False, "pending", "info", ["Les a MTBO", "automaticky"]),
        (SPRINT_ONLY, True, {"units": [_unit(1, "Praha"), _unit(2, "Lety")]}, False, "used", "ok", ["sprint", "2 obce", "ploty 3"]),
        (
            SPRINT_ONLY, True,
            {"units": [_unit(1, "Praha"), _unit(2, "Lety", False), _unit(3, "Černošice")]},
            False, "weak", "warn", ["2 z 3", "chybí: Lety"],
        ),
        (
            SPRINT_ONLY, True,
            {"units": [_unit(7, "Jihlava")], "incomplete": ["OBEC_7"]},
            False, "weak", "warn", ["Jihlava", "neúplné"],
        ),
        (SPRINT_ONLY, True, {"units": [_unit(1, "Praha")], "fences": 0, "buildings": 0}, False, "weak", "warn", ["neobsahuje"]),
        (SPRINT_ONLY, True, "bez souboru", False, "unavailable", "warn", ["není k dispozici"]),
        (SPRINT_ONLY, True, None, True, "failed", "warn", ["selhala"]),
    ],
)
def test_summary_states(tmp_path, maps, dtm_on, setup, failed, state, level, words):
    summary = dtm_summary.initial_summary(maps, dtm_on, auto=setup != "vypnuto")
    if setup is None and not failed and state == "pending":
        result = summary
    else:
        if isinstance(setup, dict):
            _write(
                tmp_path,
                units=setup["units"],
                incomplete=[f"{k}" for k in setup.get("incomplete", [])],
                fences=setup.get("fences", 3),
                buildings=setup.get("buildings", 2),
            )
        result = summary if state == "off" else dtm_summary.finalize_summary(summary, tmp_path, failed=failed)
    assert result["state"] == state
    assert result["level"] == level
    text = " ".join(result["lines"])
    for word in words:
        assert word in text


def test_readme_block_and_roundtrip(tmp_path):
    assert dtm_summary.readme_block(None) == ""
    summary = dtm_summary.initial_summary(SPRINT_ONLY, True)
    dtm_summary.write_summary(tmp_path / "work", summary)
    assert dtm_summary.read_summary(tmp_path)["state"] == "pending"
    block = dtm_summary.readme_block(summary)
    first, underline = block.splitlines()[:2]
    assert first == "Digitální technická mapa (DTM)"
    assert set(underline) == {"-"}
