from __future__ import annotations

import math

import pytest

from app.pipeline.cliff_merge import merge_cliff_ticks, polyline_to_strip_ring
from app.pipeline.oom_symbol_map import symbol_index_for_code


def _tick(x0: float, y0: float, x1: float, y1: float):
    return ((x0, y0), (x1, y1))


def _wall(n: int, *, x0: float = 0.0, y: float = 0.0, step: float = 1.0):
    """Stěna: KP kreslí čárku kolmo na spád, tedy podél stěny; nahusto za sebou."""
    return [_tick(x0 + i * step, y, x0 + i * step + 2.9, y) for i in range(n)]


def _field(width_m: float, height_m: float, *, x0: float = 0.0, y0: float = 0.0):
    """Skalní pole: čárky nahusto a v obou směrech (2D útvar, ne stěna)."""
    ticks = []
    step = 1.5
    nx = int(width_m / step)
    ny = int(height_m / step)
    for i in range(nx):
        for j in range(ny):
            x, y = x0 + i * step, y0 + j * step
            if (i + j) % 2 == 0:
                ticks.append(_tick(x, y, x + 1.4, y + 0.3))
            else:
                ticks.append(_tick(x, y, x + 0.3, y + 1.4))
    return ticks


def _length(pts) -> float:
    return sum(
        math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
        for i in range(1, len(pts))
    )


def _ring_area(pts) -> float:
    s = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def test_collinear_ticks_merge_to_one_line():
    got = merge_cliff_ticks(
        [_tick(i * 3.0, 0.0, i * 3.0 + 2.9, 0.0) for i in range(8)],
        as_polygons=False,
    )
    assert len(got.lines) == 1
    assert not got.polygons
    assert _length(got.lines[0]) >= 18.0


def test_parallel_cliffs_stay_two_lines():
    a = [_tick(i * 3.0, 0.0, i * 3.0 + 2.9, 0.0) for i in range(5)]
    b = [_tick(i * 3.0, 12.0, i * 3.0 + 2.9, 12.0) for i in range(5)]
    got = merge_cliff_ticks(a + b, as_polygons=False)
    assert len(got.lines) == 2


def test_isolated_tick_stays_short_line():
    got = merge_cliff_ticks([_tick(0.0, 0.0, 2.9, 0.0)], as_polygons=False)
    assert len(got.lines) == 1
    assert len(got.lines[0]) == 2
    assert not got.polygons


def test_perpendicular_neighbor_does_not_join():
    along = [_tick(i * 3.0, 0.0, i * 3.0 + 2.9, 0.0) for i in range(4)]
    across = _tick(4.5, -1.45, 4.5, 1.45)
    got = merge_cliff_ticks(along + [across], as_polygons=False)
    assert len(got.lines) == 2


def test_long_wall_never_becomes_polygon():
    """Souvislá stěna není plocha; jako skála se zahodí (žádná samostatná 201)."""
    got = merge_cliff_ticks(_wall(60), as_polygons=True)
    assert not got.polygons
    assert not got.lines


def test_double_wall_band_still_no_polygon():
    """I dvojitý pás (terasa) je 1D útvar – pořád ne plocha (a 201 linie pryč)."""
    ticks = _wall(40, y=0.0) + _wall(40, y=4.0)
    got = merge_cliff_ticks(ticks, as_polygons=True)
    assert not got.polygons
    assert not got.lines


def test_rock_field_becomes_polygon_close_to_real_extent():
    got = merge_cliff_ticks(_field(21.0, 21.0), as_polygons=True)
    assert got.polygons
    assert not got.lines
    area = sum(_ring_area(p) for p in got.polygons)
    # Reálný rozsah ~21×21 m; obrys po buňkách nesmí nafouknout o víc než ~30 %.
    assert 250.0 <= area <= 21.0 * 21.0 * 1.3


def test_two_rock_fields_stay_separate_polygons():
    """Dvě pole 60 m od sebe nesmí splynout do jedné obálky."""
    ticks = _field(18.0, 18.0) + _field(18.0, 18.0, x0=60.0)
    got = merge_cliff_ticks(ticks, as_polygons=True)
    assert len(got.polygons) == 2
    assert not got.lines
    assert sum(_ring_area(p) for p in got.polygons) <= 2 * 18.0 * 18.0 * 1.3


def test_l_shaped_field_keeps_concavity():
    """Záliv v L nesmí být vyplněný – jinak plocha přeteče přes realitu."""
    ticks = _field(30.0, 12.0) + _field(12.0, 30.0)
    got = merge_cliff_ticks(ticks, as_polygons=True)
    assert got.polygons
    assert not got.lines
    area = sum(_ring_area(p) for p in got.polygons)
    bbox = 30.0 * 30.0
    assert area <= bbox * 0.75


def test_wall_touching_field_drops_wall_line():
    """Stěna vybíhající z pole se zahodí; plocha se na ni nenatáhne."""
    ticks = _field(18.0, 18.0) + _wall(40, x0=21.0, y=9.0)
    got = merge_cliff_ticks(ticks, as_polygons=True)
    assert got.polygons
    assert not got.lines
    # Plocha nesmí zasahovat daleko za pole do stěny.
    assert max(x for poly in got.polygons for x, _ in poly) <= 34.0


def test_dense_field_stays_lines_for_earth_bank():
    got = merge_cliff_ticks(_field(21.0, 21.0), as_polygons=False)
    assert not got.polygons
    assert got.lines


def test_compact_leftover_lines_promote_to_area():
    """Řidší „kapsa“ mimo hlavní footprint → plocha; zbytek 201 pryč."""
    # Hlavní pole + oddělená menší kapsa ~15×15 m (po přísnějším min-area).
    ticks = _field(24.0, 24.0) + _field(15.0, 15.0, x0=40.0, y0=40.0)
    got = merge_cliff_ticks(ticks, as_polygons=True, min_line_m=12.0)
    assert len(got.polygons) >= 2
    assert not got.lines


def test_aggressive_params_still_reject_wall_blobs():
    """Agresivnější open/min-area nesmí udělat z jednoduché stěny blob."""
    wall = merge_cliff_ticks(_wall(80), as_polygons=True)
    assert not wall.polygons and not wall.lines
    band = merge_cliff_ticks(_wall(50, y=0.0) + _wall(50, y=4.0), as_polygons=True)
    assert not band.polygons and not band.lines


def test_standalone_rock_lines_always_discarded():
    """Samostatná 201 (i dlouhá) se při as_polygons vždy zahodí."""
    got = merge_cliff_ticks(_wall(80), as_polygons=True, min_line_m=0.0)
    assert not got.polygons
    assert not got.lines


def test_object_count_drops_far_below_tick_count():
    ticks = _field(24.0, 24.0) + _wall(60, y=-40.0)
    got = merge_cliff_ticks(ticks, as_polygons=True)
    assert not got.lines
    assert len(got.polygons) < len(ticks) / 10


def test_min_line_length_scales_with_map():
    from app.pipeline.cliff_merge import min_line_length_m

    assert min_line_length_m(4000) == pytest.approx(4.8)
    assert min_line_length_m(10000) == pytest.approx(12.0)
    assert min_line_length_m(15000) == pytest.approx(18.0)
    assert min_line_length_m(10000, earth=True) == pytest.approx(50.0)
    assert min_line_length_m(4000, earth=True) == pytest.approx(20.0)


def test_filter_short_earth_banks_matches_dem_threshold():
    """ZABAGED StupenSraz: krátké úseky pryč, dlouhé 104 zůstanou (@ 10k ≈ 50 m)."""
    from app.pipeline.cliff_merge import filter_short_earth_banks

    short = [(0.0, 0.0), (40.0, 0.0)]  # 40 m < 50 m
    long = [(0.0, 0.0), (55.0, 0.0)]  # 55 m ≥ 50 m
    kept = filter_short_earth_banks([short, long], scale=10000)
    assert kept == [long]
    assert filter_short_earth_banks([short], scale=4000) == [short]  # earth min ≈ 20 m


def test_filter_overlapping_earth_banks_keeps_longer():
    """Kratší 104 podél delšího → zahodit kratší (104×104)."""
    from app.pipeline.cliff_merge import filter_overlapping_earth_banks

    long = [(0.0, 0.0), (80.0, 0.0)]
    short_on_top = [(10.0, 1.0), (40.0, 1.0)]  # ~30 m, 1 m offset → v bufferu
    parallel_far = [(0.0, 30.0), (60.0, 30.0)]  # mimo buffer
    kept, n = filter_overlapping_earth_banks([short_on_top, long, parallel_far])
    assert n == 1
    assert long in kept
    assert parallel_far in kept
    assert short_on_top not in kept


def test_filter_earth_bank_lines_drops_tangled_zabaged():
    """StupenSraz: stejná zamotaná metrika jako DEM 104."""
    from app.pipeline.cliff_merge import filter_earth_bank_lines

    straight = [(0.0, 0.0), (60.0, 0.0)]
    # Smyčka: path ≫ chord → polyline_is_simple_bank False.
    loop = [
        (0.0, 0.0),
        (20.0, 0.0),
        (20.0, 20.0),
        (0.0, 20.0),
        (0.0, 1.0),
        (40.0, 1.0),
    ]
    kept = filter_earth_bank_lines([straight, loop], scale=10000)
    assert kept == [straight]


def test_min_line_drops_stubs_but_keeps_real_cliffs():
    """Kompromis proti šumu: krátké nálezy pryč, dlouhá stěna zůstane celá."""
    wall = _wall(60, y=0.0)
    stubs = [_tick(200.0 + i * 40.0, 300.0, 202.9 + i * 40.0, 300.0) for i in range(12)]
    got = merge_cliff_ticks(wall + stubs, as_polygons=False, min_line_m=12.0)
    assert len(got.lines) == 1
    assert _length(got.lines[0]) >= 55.0


def test_min_line_zero_keeps_everything():
    ticks = [_tick(200.0 + i * 40.0, 300.0, 202.9 + i * 40.0, 300.0) for i in range(5)]
    assert len(merge_cliff_ticks(ticks, as_polygons=False).lines) == 5


def test_min_line_does_not_touch_rock_areas():
    """Délkový práh se týká jen linií – plocha musí zůstat; 201 linie pryč."""
    got = merge_cliff_ticks(_field(21.0, 21.0), as_polygons=True, min_line_m=12.0)
    assert got.polygons
    assert not got.lines


def test_nearby_collinear_ticks_merge_across_wider_gap():
    """Sousední úsečky ~5 m od sebe → jedna linie (ne několik krátkých ticků)."""
    ticks = [
        _tick(0.0, 0.0, 2.9, 0.0),
        _tick(5.2, 0.0, 8.1, 0.0),
        _tick(10.4, 0.0, 13.3, 0.0),
        _tick(15.6, 0.0, 18.5, 0.0),
    ]
    got = merge_cliff_ticks(ticks, as_polygons=False, min_line_m=0.0)
    assert len(got.lines) == 1
    assert _length(got.lines[0]) >= 14.0


def test_isolated_short_tick_dropped_by_default_min_line():
    """Jedna osamělá čárka pod prahem ~1,2 mm na mapě (12 m @ 1:10k) zmizí."""
    from app.pipeline.cliff_merge import min_line_length_m

    got = merge_cliff_ticks(
        [_tick(0.0, 0.0, 2.9, 0.0)],
        as_polygons=False,
        min_line_m=min_line_length_m(10000),
    )
    assert got.lines == []


def test_trace_rings_splits_diagonally_touching_lobes():
    from app.pipeline.cliff_merge import _trace_cell_rings

    rings = _trace_cell_rings({(0, 0), (1, 1)}, 3.0)
    assert len(rings) == 2
    for ring in rings:
        assert _ring_area(ring) == 9.0


def test_trace_ring_keeps_hole_as_separate_clockwise_ring():
    from app.pipeline.cliff_merge import _signed_ring_area, _trace_cell_rings

    donut = {(i, j) for i in range(3) for j in range(3)} - {(1, 1)}
    rings = _trace_cell_rings(donut, 3.0)
    assert len(rings) == 2
    assert sorted(round(_signed_ring_area(r), 3) for r in rings) == [-9.0, 81.0]


def test_dense_symbol_fallback_exists():
    from app.pipeline.oom_symbol_map import (
        KP_CLIFF_DENSE_CODE,
        resolve_rock_area_code,
        symbol_index_for_code,
    )

    assert resolve_rock_area_code("forest_10000", 10000) == "201.2"
    assert resolve_rock_area_code("sprint_2m", 4000) == "206"
    assert resolve_rock_area_code("mtbo_10000", 10000) == "206"
    assert symbol_index_for_code("sprint_2m", 4000, KP_CLIFF_DENSE_CODE) is not None
    assert symbol_index_for_code("forest_10000", 10000, KP_CLIFF_DENSE_CODE) is not None


def test_symbol_206_exists_in_both_sets():
    assert symbol_index_for_code("sprint_2m", 4000, "206") is not None
    assert symbol_index_for_code("forest_10000", 10000, "206") is not None


def test_polyline_to_strip_ring_makes_closed_area():
    ring = polyline_to_strip_ring([(0.0, 0.0), (10.0, 0.0)], half_width_m=1.5)
    assert ring is not None
    assert ring[0] == ring[-1]
    assert len(ring) >= 5
    assert _ring_area(ring) == pytest.approx(30.0, abs=0.5)


def _ring_self_intersects(pts) -> bool:
    from app.pipeline.cliff_merge import _ring_is_simple

    body = pts[:-1] if len(pts) >= 2 and pts[0] == pts[-1] else pts
    return not _ring_is_simple(body)


def test_rock_area_rings_are_not_self_intersecting():
    """Shluk ticků → plocha s čistým okrajem (následující body se nekříží)."""
    for ticks in (
        _field(21.0, 21.0),
        _field(30.0, 12.0) + _field(12.0, 30.0),
        _field(24.0, 24.0) + _wall(40, x0=30.0, y=12.0),
        _field(18.0, 18.0) + _field(18.0, 18.0, x0=60.0),
    ):
        got = merge_cliff_ticks(ticks, as_polygons=True)
        assert got.polygons
        for ring in got.polygons:
            assert len(ring) >= 3
            assert not _ring_self_intersects(ring)


def test_simplify_ring_safe_rejects_bowtie():
    """Douglas–Peucker na prstenci nesmí vrátit self-intersecting bowtie."""
    from app.pipeline.cliff_merge import _ring_is_simple, _simplify_ring_safe

    # C-tvar: agresivní DP by propojil ramena a vytvořil průsečík.
    ring = [
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, 2.0),
        (2.0, 2.0),
        (2.0, 8.0),
        (10.0, 8.0),
        (10.0, 10.0),
        (0.0, 10.0),
    ]
    safe = _simplify_ring_safe(ring, tol=9.0)
    assert _ring_is_simple(safe)
    assert not _ring_self_intersects(safe)


def test_zigzag_strip_falls_back_to_simple_hull():
    """Zigzagová střednice nesmí dát křížící se pás 206."""
    zig = [(float(i) * 2.0, (i % 2) * 10.0) for i in range(10)]
    ring = polyline_to_strip_ring(zig, half_width_m=1.5)
    assert ring is not None
    assert not _ring_self_intersects(ring)


def test_ring_is_simple_detects_bowtie():
    from app.pipeline.cliff_merge import _ring_is_simple

    bowtie = [(0.0, 0.0), (2.0, 2.0), (0.0, 2.0), (2.0, 0.0)]
    assert not _ring_is_simple(bowtie)
    square = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
    assert _ring_is_simple(square)


def test_rock_field_has_few_smooth_vertices():
    """Footprint má plynulý obvod (málo vrcholů), ne zigzag po koncích ticků."""
    field = _field(24.0, 24.0)
    got = merge_cliff_ticks(field, as_polygons=True)
    assert len(got.polygons) == 1
    assert not got.lines
    ring = got.polygons[0]
    assert 3 <= len(ring) <= 24
    assert not _ring_self_intersects(ring)
    # Méně objektů než vstupních ticků – ideálně 1 plocha.
    assert len(got.polygons) < len(field) / 5


def test_reject_tangled_drops_loopy_bank():
    from app.pipeline.cliff_merge import merge_cliff_ticks, polyline_is_simple_bank

    # Spirálovitá / smyčková lomená čára – sinuozita vysoko.
    loop = [
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, 8.0),
        (2.0, 8.0),
        (2.0, 2.0),
        (12.0, 2.0),
        (12.0, 12.0),
        (0.0, 12.0),
    ]
    assert not polyline_is_simple_bank(loop)
    straight = [(float(i) * 3.0, 0.0) for i in range(8)]
    assert polyline_is_simple_bank(straight)
    # Merge: rovná stěna projde, umělá smyčka z ticků kolem dokola ne.
    wall = _wall(40)
    tangled_ticks = [
        _tick(100.0 + math.cos(a) * 6.0, 100.0 + math.sin(a) * 6.0,
              100.0 + math.cos(a + 0.4) * 6.0, 100.0 + math.sin(a + 0.4) * 6.0)
        for a in [i * 0.35 for i in range(20)]
    ]
    got = merge_cliff_ticks(
        wall + tangled_ticks, as_polygons=False, min_line_m=0.0, reject_tangled=True
    )
    assert len(got.lines) >= 1
    assert all(polyline_is_simple_bank(line) for line in got.lines)
    assert max(_length(line) for line in got.lines) >= 35.0


def test_gentle_bend_bank_is_kept():
    from app.pipeline.cliff_merge import polyline_is_simple_bank

    bend = [(0.0, 0.0), (10.0, 0.0), (18.0, 3.0), (25.0, 4.0)]
    assert polyline_is_simple_bank(bend)


def test_crooked_45m_bank_is_rejected():
    """Přísnější filtr: ~45 m křivý sráz (typicky falešný DEM) pryč."""
    from app.pipeline.cliff_merge import polyline_is_simple_bank

    # Path ≫ chord: mírný meandr přes ~45 m – dřív (sinuosity 1.85) prošel.
    crooked = [
        (0.0, 0.0),
        (8.0, 6.0),
        (16.0, -2.0),
        (24.0, 7.0),
        (32.0, -1.0),
        (40.0, 5.0),
        (45.0, 0.0),
    ]
    assert not polyline_is_simple_bank(crooked)
    # Dlouhá skoro rovná stěna stále OK.
    straight_45 = [(float(i), 0.1 * math.sin(i * 0.2)) for i in range(46)]
    assert polyline_is_simple_bank(straight_45)


def test_rock_scarp_overlap_scarp_wins():
    """Překryv → vždy sráz; i krátký 104 přebije velkou skálu."""
    from app.pipeline.cliff_merge import resolve_rock_scarp_overlaps

    small_rock = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    long_scarp = [(-20.0, 5.0), (30.0, 5.0)]
    rocks, scarps, n = resolve_rock_scarp_overlaps([small_rock], [long_scarp])
    assert n == 1
    assert rocks == []
    assert len(scarps) == 1

    # Dřív „delší skála vyhrála“ – teď scarp wins i proti velké ploše.
    big_rock = [(0.0, 0.0), (50.0, 0.0), (50.0, 50.0), (0.0, 50.0)]
    short_scarp = [(10.0, 25.0), (30.0, 25.0)]
    rocks, scarps, n = resolve_rock_scarp_overlaps([big_rock], [short_scarp])
    assert n == 1
    assert rocks == []
    assert len(scarps) == 1


def test_rock_scarp_no_overlap_keeps_both():
    from app.pipeline.cliff_merge import resolve_rock_scarp_overlaps

    rock = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    scarp = [(100.0, 0.0), (140.0, 0.0)]
    rocks, scarps, n = resolve_rock_scarp_overlaps([rock], [scarp])
    assert n == 0
    assert len(rocks) == 1 and len(scarps) == 1


def test_rock_blocker_overlap_suppresses_rock():
    from app.pipeline.cliff_merge import filter_rocks_overlapping_blockers

    rock = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    far = [(100.0, 0.0), (120.0, 0.0), (120.0, 20.0), (100.0, 20.0)]
    building = [(5.0, 5.0), (15.0, 5.0), (15.0, 15.0), (5.0, 15.0)]
    path = [(-5.0, 10.0), (25.0, 10.0)]
    kept, n = filter_rocks_overlapping_blockers(
        [rock, far], [building], [path], line_buffer_m=2.5
    )
    assert n == 1
    assert kept == [far]


def test_dense_contours_suppress_rock_and_scarp():
    """Strmý svah (husté 5m vrstevnice) → skála i 104 pryč."""
    from app.pipeline.cliff_merge import filter_by_dense_contours

    # grade ≈ 2.0 → spacing = 5/2 = 2.5 m < 1.0*5 = 5 m
    def elev(x, y):
        return -2.0 * x

    rock = [(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)]
    scarp = [(0.0, 5.0), (40.0, 5.0)]
    flat_rock = [(1000.0, 0.0), (1030.0, 0.0), (1030.0, 20.0), (1000.0, 20.0)]

    def elev_flat_zone(x, y):
        if x >= 900:
            return 0.0
        return -2.0 * x

    lines, polys, n = filter_by_dense_contours(
        [scarp], [rock, flat_rock], elev_flat_zone, interval_m=5.0
    )
    assert n >= 2
    assert scarp not in lines
    assert rock not in polys
    assert flat_rock in polys


def test_dense_contours_fires_at_unit_spacing_frac():
    """grade≈1.05 → spacing≈4.76 < 1.0×5; dřív (0.8×) by neprošlo."""
    from app.pipeline.cliff_merge import (
        DENSE_CONTOUR_SPACING_FRAC,
        filter_by_dense_contours,
    )

    assert DENSE_CONTOUR_SPACING_FRAC >= 1.0

    def elev(x, y):
        return -1.05 * x

    scarp = [(0.0, 0.0), (40.0, 0.0)]
    lines, polys, n = filter_by_dense_contours([scarp], [], elev, interval_m=5.0)
    assert n == 1
    assert lines == []
    assert polys == []


def test_short_scarp_in_dense_zone_dropped():
    """Krátký 104 se 2 vzorky – dřív len<3 → False a sráz zůstal."""
    from app.pipeline.cliff_merge import filter_by_dense_contours

    def elev(x, y):
        return -2.0 * x

    short = [(0.0, 0.0), (3.0, 0.0)]
    lines, _, n = filter_by_dense_contours([short], [], elev, interval_m=5.0)
    assert n == 1
    assert lines == []


def test_cliffs_crossing_buildings_discarded():
    from app.pipeline.cliff_merge import filter_cliffs_crossing_buildings

    building = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    through_line = [(-5.0, 10.0), (25.0, 10.0)]
    outside_line = [(50.0, 0.0), (80.0, 0.0)]
    through_rock = [(5.0, 5.0), (15.0, 5.0), (15.0, 15.0), (5.0, 15.0)]
    outside_rock = [(60.0, 0.0), (80.0, 0.0), (80.0, 20.0), (60.0, 20.0)]

    lines, polys, n = filter_cliffs_crossing_buildings(
        [through_line, outside_line],
        [through_rock, outside_rock],
        [building],
    )
    assert n == 2
    assert lines == [outside_line]
    assert polys == [outside_rock]


def test_tight_bank_filter_rejects_mild_zigzag():
    """Přísnější sinuozita/zatáčky – jemný zigzag už neprojde."""
    from app.pipeline.cliff_merge import polyline_is_simple_bank

    zigzag = [
        (0.0, 0.0),
        (5.0, 8.0),
        (10.0, 0.0),
        (15.0, 8.0),
        (20.0, 0.0),
        (25.0, 8.0),
        (30.0, 0.0),
        (35.0, 8.0),
        (40.0, 0.0),
    ]
    assert not polyline_is_simple_bank(zigzag)

