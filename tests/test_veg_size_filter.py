"""Testy vegetačního size filtru — od ≥2.2.17 vypnutý (vždy keep)."""

from __future__ import annotations

import pytest
from shapely.geometry import box

from app.pipeline.veg_size_filter import (
    VEG_SIZE_FILTER_ENABLED,
    keep_veg_polygon,
    resolve_veg_size_profile,
    soft_min_for_code,
    thresholds_for,
)


def test_veg_size_filter_disabled_by_default():
    assert VEG_SIZE_FILTER_ENABLED is False


def test_soft_min_legacy_thresholds_still_documented():
    """Prahy zůstávají v kódu pro A/B; pipeline je neaplikuje."""
    assert soft_min_for_code("401") == pytest.approx(12.0)
    assert soft_min_for_code("406") == pytest.approx(25.0)
    assert soft_min_for_code("408") == pytest.approx(25.0)
    assert soft_min_for_code("410") == pytest.approx(25.0)


def test_default_thresholds_from_proposal():
    y = thresholds_for("401", "default")
    assert y.hard_floor_m2 == pytest.approx(6.0)
    assert y.soft_min_m2 == pytest.approx(12.0)
    g = thresholds_for("408", "default")
    assert g.hard_floor_m2 == pytest.approx(12.0)
    assert g.soft_min_m2 == pytest.approx(25.0)


def test_keep_always_when_filter_off():
    """Kompaktní fleky i pod dřívějším hard_floor → nechat (úklid na uživateli)."""
    tiny = box(0, 0, 2, 2)  # 4 m²
    compact = box(0, 0, 3, 3)  # 9 m²
    green_compact = box(0, 0, 4, 6)  # 24 m²
    for code, geom in (
        ("401", tiny),
        ("401", compact),
        ("408", green_compact),
        ("406", tiny),
        ("410", compact),
    ):
        keep, reason = keep_veg_polygon(code, geom, profile="default")
        assert keep is True
        assert reason is None
        keep_s, reason_s = keep_veg_polygon(code, geom, profile="strict")
        assert keep_s is True
        assert reason_s is None


def test_empty_still_dropped():
    from shapely.geometry import Polygon

    empty = Polygon()
    keep, reason = keep_veg_polygon("401", empty, profile="default")
    assert keep is False
    assert reason is not None


def test_sprint_always_default_profile():
    assert (
        resolve_veg_size_profile(
            {"veg_size_profile": "strict"},
            preset_id="sprint_2_5m",
        )
        == "default"
    )
    assert (
        resolve_veg_size_profile(
            {"veg_size_profile": "strict"},
            map_scale=4000,
        )
        == "default"
    )


def test_forest_can_opt_into_strict_profile_name():
    """Profil se resolve'uje, ale keep_veg_polygon ho při off neaplikuje."""
    assert (
        resolve_veg_size_profile(
            {"veg_size_profile": "strict"},
            preset_id="forest_5m",
            map_scale=10000,
        )
        == "strict"
    )
    assert resolve_veg_size_profile({}, preset_id="forest_5m") == "default"


def test_legacy_filter_logic_when_reenabled(monkeypatch):
    """Když by se filtr znovu zapnul, dřívější pravidla pořád fungují."""
    monkeypatch.setattr(
        "app.pipeline.veg_size_filter.VEG_SIZE_FILTER_ENABLED", True
    )
    from app.pipeline.veg_size_filter import REASON_COMPACT, REASON_SMALL_AREA

    keep, reason = keep_veg_polygon("401", box(0, 0, 2, 2), profile="default")
    assert keep is False
    assert reason == REASON_SMALL_AREA
    keep, reason = keep_veg_polygon("401", box(0, 0, 3, 3), profile="default")
    assert keep is False
    assert reason == REASON_COMPACT
    assert keep_veg_polygon("401", box(0, 0, 5, 5), profile="default")[0] is True
