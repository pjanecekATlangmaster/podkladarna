"""Closed relief: 101.1 (large moderate pits), 103 mid-interval only."""

from app.pipeline.cliff_height import classify_closed_ring_relief
from app.pipeline.contours_gdal import (
    closed_relief_thresholds,
    place_closed_contour_ticks,
    slope_tick_rotation_rad,
    _inset_ring,
)


def _bowl_elev(x: float, y: float) -> float:
    dx, dy = x - 50.0, y - 50.0
    r = (dx * dx + dy * dy) ** 0.5
    return -2.5 if r < 25.0 else 0.0


def _knoll_elev(height: float):
    def elev(x: float, y: float) -> float:
        dx, dy = x - 50.0, y - 50.0
        r = (dx * dx + dy * dy) ** 0.5
        return height if r < 25.0 else 0.0

    return elev


def _square(cx: float, cy: float, half: float) -> list[tuple[float, float]]:
    return [
        (cx - half, cy - half),
        (cx + half, cy - half),
        (cx + half, cy + half),
        (cx - half, cy + half),
        (cx - half, cy - half),
    ]


def test_classify_large_depression():
    ring = _square(50, 50, 40)
    relief = classify_closed_ring_relief(
        ring, _bowl_elev, min_depression_m=1.2, min_elevation_m=2.0
    )
    assert relief is not None
    assert relief.kind == "depression"
    assert 1.2 <= relief.relief_m <= 3.5


def test_classify_mid_interval_elevation():
    ring = _square(50, 50, 40)
    relief = classify_closed_ring_relief(
        ring, _knoll_elev(3.0), min_depression_m=1.2, min_elevation_m=2.5
    )
    assert relief is not None
    assert relief.kind == "elevation"


def test_shallow_noise_skipped():
    def elev(x: float, y: float) -> float:
        dx, dy = x - 50.0, y - 50.0
        r = (dx * dx + dy * dy) ** 0.5
        return -0.5 if r < 25.0 else 0.0

    assert (
        classify_closed_ring_relief(
            _square(50, 50, 40), elev, min_depression_m=1.2, min_elevation_m=2.0
        )
        is None
    )


def test_ticks_point_inward_for_depression():
    ring = _square(50, 50, 40)
    ticks = place_closed_contour_ticks(ring, toward_centroid=True, spacing_m=50.0)
    assert ticks
    for x, y, tx, ty in ticks:
        assert (tx - x) * (50 - x) + (ty - y) * (50 - y) > 0


def test_inset_ring_is_smaller():
    ring = _square(50, 50, 40)
    inset = _inset_ring(ring, factor=0.55)
    assert abs(inset[0][0] - 50) < abs(ring[0][0] - 50)


def test_slope_tick_rotation_points_down_map_y():
    assert abs(slope_tick_rotation_rad(0, 0, 0, -10)) < 1e-9


def test_thresholds_forest_5m_area_first():
    min_dep, max_dep, dep_len, form_lo, form_hi, form_len = closed_relief_thresholds(
        5.0
    )
    assert min_dep >= 1.2
    assert max_dep >= 3.5
    assert dep_len >= 65.0
    assert form_lo >= 2.5  # ~0.55*5
    assert form_hi <= 5.0
    assert form_lo < form_hi
    assert form_len >= 140.0


def test_thresholds_sprint_2m_stricter_formline_band():
    _a, _b, _c, form_lo, form_hi, form_len = closed_relief_thresholds(2.0)
    assert 1.0 <= form_lo < form_hi <= 2.0
    assert form_len >= 140.0
