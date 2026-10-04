"""Testy filtru tvaru vegetace (hard_floor / soft_min / pás L+aspect)."""

from __future__ import annotations

import pytest
from shapely.geometry import box

from app.pipeline.veg_size_filter import (
    REASON_COMPACT,
    REASON_SMALL_AREA,
    keep_veg_polygon,
    resolve_veg_size_profile,
    soft_min_for_code,
    thresholds_for,
)


def test_soft_min_matches_legacy_go_defaults():
    assert soft_min_for_code("401") == pytest.approx(12.0)
    assert soft_min_for_code("406") == pytest.approx(25.0)
    assert soft_min_for_code("408") == pytest.approx(25.0)
    assert soft_min_for_code("410") == pytest.approx(25.0)


def test_default_thresholds_from_proposal():
    y = thresholds_for("401", "default")
    assert y.hard_floor_m2 == pytest.approx(6.0)
    assert y.soft_min_m2 == pytest.approx(12.0)
    assert y.band_length_min_m == pytest.approx(20.0)
    assert y.band_aspect_min == pytest.approx(4.0)
    g = thresholds_for("408", "default")
    assert g.hard_floor_m2 == pytest.approx(12.0)
    assert g.soft_min_m2 == pytest.approx(25.0)
    assert g.band_length_min_m == pytest.approx(25.0)
    assert g.band_aspect_min == pytest.approx(5.0)


def test_strict_thresholds_from_proposal():
    y = thresholds_for("401", "strict")
    assert y.hard_floor_m2 == pytest.approx(8.0)
    assert y.soft_min_m2 == pytest.approx(20.0)
    g = thresholds_for("406", "strict")
    assert g.hard_floor_m2 == pytest.approx(15.0)
    assert g.soft_min_m2 == pytest.approx(40.0)


def test_compact_3x3_dropped_401():
    # 3×3 m kompakt 9 m² — pod soft, není pás
    geom = box(0, 0, 3, 3)
    keep, reason = keep_veg_polygon("401", geom, profile="default")
    assert keep is False
    assert reason == REASON_COMPACT


def test_band_2x30_kept_401_and_green():
    geom = box(0, 0, 2, 30)
    keep_y, _ = keep_veg_polygon("401", geom, profile="default")
    keep_g, _ = keep_veg_polygon("408", geom, profile="default")
    assert keep_y is True
    assert keep_g is True


def test_short_strip_under_soft_dropped_401():
    # 1.5×7.9 ≈ 11.85 m² (< soft 12), L≈7.9 < 20 → pryč (krátký pásek)
    # Přesně 12 m² už padá do soft_min → nechat (viz proposal rule ≥ soft).
    geom = box(0, 0, 1.5, 7.9)
    keep, reason = keep_veg_polygon("401", geom, profile="default")
    assert keep is False
    assert reason == REASON_COMPACT


def test_hard_floor_fragments():
    geom = box(0, 0, 2, 2)  # 4 m² < hard 6
    keep, reason = keep_veg_polygon("401", geom, profile="default")
    assert keep is False
    assert reason == REASON_SMALL_AREA


def test_above_soft_always_kept_even_compact():
    geom = box(0, 0, 5, 5)  # 25 m²
    assert keep_veg_polygon("401", geom, profile="default")[0] is True
    assert keep_veg_polygon("408", geom, profile="default")[0] is True


def test_green_compact_24m2_dropped_default():
    # 4×6 m = 24 m², aspect 1.5 — mezi 12–25, není pás
    geom = box(0, 0, 4, 6)
    keep, reason = keep_veg_polygon("408", geom, profile="default")
    assert keep is False
    assert reason == REASON_COMPACT


def test_band_between_hard_and_soft_kept_401():
    # 2×8 m = 16 m²? Wait soft is 12 — need 6–12 band. Use 2×5 = 10 m², L=5 < 20 → drop
    # True band in 6–12: 1.5×7 = 10.5, L=7 < 20 → drop
    # 2×11 = 22 ≥ soft → always keep. For band: need area < 12.
    # 1.2 × 9 = 10.8, L=9 < 20 → drop
    # Can't have L≥20 and area < 12 with width≥1: 20*w < 12 → w < 0.6
    geom = box(0, 0, 0.5, 22)  # 11 m², L=22, aspect=44
    keep, reason = keep_veg_polygon("401", geom, profile="default")
    assert keep is True
    assert reason is None


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


def test_forest_can_opt_into_strict():
    assert (
        resolve_veg_size_profile(
            {"veg_size_profile": "strict"},
            preset_id="forest_5m",
            map_scale=10000,
        )
        == "strict"
    )
    assert resolve_veg_size_profile({}, preset_id="forest_5m") == "default"


def test_strict_removes_more_than_default():
    # 5×5 = 25 m² — default green soft 25 → keep; strict soft 40 → compact drop
    geom = box(0, 0, 5, 5)
    assert keep_veg_polygon("408", geom, profile="default")[0] is True
    keep_s, reason = keep_veg_polygon("408", geom, profile="strict")
    assert keep_s is False
    assert reason == REASON_COMPACT
