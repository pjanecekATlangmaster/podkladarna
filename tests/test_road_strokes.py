"""Les a MTBO: jeden symbol silnice na souvislý tah (``app.pipeline.road_strokes``)."""

import pytest

from app.pipeline import road_strokes as rs

MIN_RUN = 50.0  # 5 mm v 1:10 000


def _h(x0, x1, y=0.0):
    return [(float(x0), y), (float(x1), y)]


@pytest.mark.parametrize(
    "lines, codes, fixed, expected",
    [
        # M3 Lidl: 24 m 502.1 mezi dvěma dlouhými 502.2 → 502.2
        ([_h(0, 300), _h(300, 324), _h(324, 700)], ["502.2", "502.1", "502.2"], set(), ["502.2", "502.2", "502.2"]),
        # M2: 27 m 502.1 za pevnou osou 502.0 před křižovatkou → 502.0 (pevný soused vyhrává i proti delšímu)
        ([_h(0, 185), _h(185, 212), _h(212, 600)], ["502.0", "502.1", "502"], {0}, ["502.0", "502.0", "502"]),
        # krátký kus mezi dvěma různými dlouhými běhy bere delší
        ([_h(0, 100), _h(100, 130), _h(130, 400)], ["502.1", "503", "502"], set(), ["502.1", "502", "502"]),
        # dva dlouhé běhy: změna šířky v uzlu zůstává
        ([_h(0, 200), _h(200, 500)], ["502.1", "502"], set(), ["502.1", "502"]),
        # řetěz krátkých kusů téhož symbolu je jeden dlouhý běh
        ([_h(0, 30), _h(30, 60), _h(60, 90), _h(90, 110)], ["502", "502", "502", "502.1"], set(), ["502", "502", "502", "502"]),
        # celý krátký tah ze dvou symbolů se sjednotí (jedna čára)
        ([_h(0, 20), _h(20, 40)], ["502.1", "502"], set(), ["502", "502"]),
    ],
)
def test_short_run_takes_neighbour_symbol(lines, codes, fixed, expected):
    assert rs.stroke_codes(lines, codes, min_run_m=MIN_RUN, fixed=fixed) == expected


def test_stroke_continues_straight_through_junction_only():
    # hlavní 0→300 rovně, vedlejší odbočuje v x=150 kolmo: tah hlavní = 2 kusy, vedlejší samostatně
    lines = [_h(0, 150), _h(150, 300), [(150.0, 0.0), (150.0, 80.0)]]
    chains = rs.strokes(lines)
    assert sorted(sorted(c) for c in chains) == [[0, 1], [2]]
    # krátký kus 502.1 za vedlejší patří k tahu hlavní a převezme její 502
    lines = [_h(0, 150), _h(150, 180), _h(180, 400), [(150.0, 0.0), (150.0, 80.0)]]
    assert rs.stroke_codes(lines, ["502", "502.1", "502", "503"], min_run_m=MIN_RUN) == ["502", "502", "502", "503"]


def test_stub_only_narrows():
    # M4: kapka otočky 502.1 (40 m) na dlouhé 502.2 → 502.2; účelová spojka 502 na 502.1 zůstává 502
    main = [_h(0, 200), _h(200, 400)]
    loop = [[(200.0, 0.0), (207.0, 7.0), (200.0, 14.0), (193.0, 7.0), (200.0, 0.0)]]
    assert rs.stroke_codes(main + loop, ["502.2", "502.2", "502.1"], min_run_m=MIN_RUN) == ["502.2", "502.2", "502.2"]
    link = [[(200.0, 0.0), (200.0, 30.0)]]
    assert rs.stroke_codes(main + link, ["502.1", "502.1", "502"], min_run_m=MIN_RUN) == ["502.1", "502.1", "502"]


def test_ring_is_one_stroke():
    pts = [(100.0, 0.0), (0.0, 100.0), (-100.0, 0.0), (0.0, -100.0)]
    import math

    ring = []
    for k in range(4):
        a0, a1 = math.radians(90 * k), math.radians(90 * (k + 1))
        ring.append([(100 * math.cos(a0 + (a1 - a0) * t / 4), 100 * math.sin(a0 + (a1 - a0) * t / 4)) for t in range(5)])
    arm = [[(100.0, 0.0), (200.0, 0.0)]]
    chains = rs.strokes(ring + arm)
    assert sorted(sorted(c) for c in chains) == [[0, 1, 2, 3], [4]]
