"""Closed relief: deprese → 101.1, vyvýšeniny → 103 (ne malé knolly)."""

from app.pipeline.cliff_height import classify_closed_ring_relief
from app.pipeline.contours_gdal import (
    contour_tick_thresholds,
    place_closed_contour_ticks,
    slope_tick_rotation_rad,
    _inset_ring,
)


def _bowl_elev(x: float, y: float) -> float:
    dx, dy = x - 50.0, y - 50.0
    r = (dx * dx + dy * dy) ** 0.5
    return -3.5 if r < 25.0 else 0.0


def _knoll_elev(x: float, y: float) -> float:
    dx, dy = x - 50.0, y - 50.0
    r = (dx * dx + dy * dy) ** 0.5
    return 5.0 if r < 25.0 else 0.0


def _flat_elev(x: float, y: float) -> float:
    return 10.0


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
        ring, _bowl_elev, min_depression_m=2.5, min_elevation_m=4.0
    )
    assert relief is not None
    assert relief.kind == "depression"
    assert relief.relief_m >= 2.5


def test_classify_large_elevation():
    ring = _square(50, 50, 40)
    relief = classify_closed_ring_relief(
        ring, _knoll_elev, min_depression_m=2.5, min_elevation_m=4.0
    )
    assert relief is not None
    assert relief.kind == "elevation"
    assert relief.relief_m >= 4.0


def test_classify_flat_skipped():
    ring = _square(50, 50, 40)
    assert (
        classify_closed_ring_relief(
            ring, _flat_elev, min_depression_m=2.5, min_elevation_m=4.0
        )
        is None
    )


def test_shallow_depression_skipped():
    def elev(x: float, y: float) -> float:
        dx, dy = x - 50.0, y - 50.0
        r = (dx * dx + dy * dy) ** 0.5
        return -1.0 if r < 25.0 else 0.0

    ring = _square(50, 50, 40)
    assert (
        classify_closed_ring_relief(
            ring, elev, min_depression_m=2.5, min_elevation_m=4.0
        )
        is None
    )


def test_small_knoll_height_under_threshold():
    def elev(x: float, y: float) -> float:
        dx, dy = x - 50.0, y - 50.0
        r = (dx * dx + dy * dy) ** 0.5
        return 2.0 if r < 12.0 else 0.0

    ring = _square(50, 50, 15)
    assert (
        classify_closed_ring_relief(
            ring, elev, min_depression_m=2.5, min_elevation_m=4.0
        )
        is None
    )


def test_ticks_point_inward_for_depression():
    ring = _square(50, 50, 40)
    ticks = place_closed_contour_ticks(ring, toward_centroid=True, spacing_m=50.0)
    assert ticks
    for x, y, tx, ty in ticks:
        # směrem k těžišti
        assert (tx - x) * (50 - x) + (ty - y) * (50 - y) > 0


def test_inset_ring_is_smaller():
    ring = _square(50, 50, 40)
    inset = _inset_ring(ring, factor=0.55)
    assert abs(inset[0][0] - 50) < abs(ring[0][0] - 50)


def test_slope_tick_rotation_points_down_map_y():
    # default tip (0,-1): rotace 0
    assert abs(slope_tick_rotation_rad(0, 0, 0, -10)) < 1e-9


def test_thresholds_conservative_for_5m():
    dep, elev, dep_len, elev_len = contour_tick_thresholds(5.0)
    assert dep >= 2.0
    assert elev >= 2.2
    assert dep_len >= 60.0
    assert elev_len >= 120.0
    # pořád nad skála-vs-deprese (1.2 m) a mimo drobné knolly
    assert dep > 1.2
    assert elev > 1.2
