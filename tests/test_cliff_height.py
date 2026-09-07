from __future__ import annotations

import pytest

from app.pipeline.cliff_height import (
    MAJOR_DROP_M,
    drop_is_mappable,
    filter_by_drop,
    measure_drop,
)


def _step_terrain(height: float, at_x: float = 0.0):
    """Vodorovná plošina, na x = at_x schod dolů o `height`."""

    def elev(x: float, y: float) -> float:
        return 0.0 if x < at_x else -height

    return elev


def _slope_terrain(grade: float):
    """Rovnoměrný svah bez jakéhokoli schodu."""

    def elev(x: float, y: float) -> float:
        return -grade * x

    return elev


def _slope_with_step(grade: float, height: float, at_x: float = 0.0):
    def elev(x: float, y: float) -> float:
        return -grade * x - (height if x >= at_x else 0.0)

    return elev


def _line_along_y(length: float = 30.0, x: float = 0.0):
    return [(x, -length / 2), (x, length / 2)]


def test_measures_plain_step():
    got = measure_drop(_line_along_y(), _step_terrain(3.0))
    assert got.measured
    assert got.drop_m == pytest.approx(3.0, abs=0.2)
    assert got.stations >= 5


def test_uniform_slope_reads_as_no_step():
    """Klíčové: samotný svah nesmí vypadat jako sráz."""
    got = measure_drop(_line_along_y(), _slope_terrain(0.5))
    assert got.measured
    assert got.drop_m < 0.3


def test_step_on_slope_keeps_only_the_step():
    got = measure_drop(_line_along_y(), _slope_with_step(0.4, 2.5))
    assert got.measured
    assert got.drop_m == pytest.approx(2.5, abs=0.4)


def test_small_step_is_below_threshold():
    got = measure_drop(_line_along_y(), _step_terrain(0.5))
    assert got.measured
    assert not drop_is_mappable(got)


def test_big_step_counts_as_major():
    got = measure_drop(_line_along_y(), _step_terrain(6.0))
    assert got.drop_m >= MAJOR_DROP_M


def test_no_dem_measures_nothing_and_keeps_line():
    got = measure_drop(_line_along_y(), None)
    assert not got.measured
    assert drop_is_mappable(got)


def test_nodata_holes_do_not_crash():
    def elev(x, y):
        return None if abs(x) > 1.0 else 0.0

    got = measure_drop(_line_along_y(), elev)
    assert not got.measured


def test_degenerate_line_is_not_measured():
    assert not measure_drop([(0.0, 0.0)], _step_terrain(3.0)).measured
    assert not measure_drop([(0.0, 0.0), (0.0, 0.0)], _step_terrain(3.0)).measured


def test_filter_drops_noise_and_keeps_real_cliff():
    real = _line_along_y(x=0.0)
    noise = [(50.0, -15.0), (50.0, 15.0)]

    def elev(x, y):
        # Schod 3 m u x = 0, u x = 50 nic.
        return 0.0 if x < 0.0 else -3.0

    kept, stats = filter_by_drop([real, noise], elev)
    assert kept == [real]
    assert stats["zahozeno"] == 1
    assert stats["vyrazne"] == 1


def test_filter_without_dem_keeps_everything():
    lines = [_line_along_y(), _line_along_y(x=20.0)]
    kept, stats = filter_by_drop(lines, None)
    assert kept == lines
    assert stats["nezmereno"] == 2
