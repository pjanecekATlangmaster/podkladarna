"""Dotažení volných konců silnic a zahození pahýlů v lese a MTBO (``road_ends``)."""

import pytest

pytest.importorskip("shapely")

from shapely.geometry import box

from app.pipeline.road_ends import tidy_road_ends

MAIN = [(0.0, 0.0), (200.0, 0.0)]


def _side(y0: float) -> list[tuple[float, float]]:
    return [(100.0, y0), (100.0, 80.0)]


# (případ, kód hlavní, kód vedlejší, začátek vedlejší y, OSM vede až k hlavní, překážka, očekávaný konec y nebo None = beze změny)
# forest_10000: 502 = 0,87 mm (půlka 4,35 m), dotažení do 1,5 mm = 15 m mezery do kresby cíle.
@pytest.mark.parametrize(
    "case, main_code, side_code, y0, osm_full, blocker, end_y",
    [
        ("výplň do výplně k ose", "502", "502", 10.0, True, None, 0.0),
        ("černá 503 na okraj výplně", "502", "503", 10.0, True, None, 4.35),
        ("černá na černou k ose", "503", "503", 8.0, True, None, 0.0),
        ("mezera nad 1,5 mm", "502", "502", 25.0, True, None, None),
        ("přes budovu ne", "502", "502", 10.0, True, box(95, 5, 105, 8), None),
        ("OSM nepokračuje", "502", "502", 10.0, False, None, None),
        ("bez OSM opory přímé prodloužení", "502", "502", 10.0, None, None, 0.0),
    ],
)
def test_free_end_extended_to_road(case, main_code, side_code, y0, osm_full, blocker, end_y):
    if osm_full is None:
        osm = []
    else:
        osm = [MAIN, [(100.0, 0.0), (100.0, 80.0)] if osm_full else _side(y0)]
    pieces = {main_code: [MAIN]} if main_code == side_code else {main_code: [MAIN], side_code: []}
    pieces.setdefault(side_code, []).append(_side(y0))
    out = tidy_road_ends(pieces, osm, preset_id="forest_10000", scale=10000, blockers=blocker)
    side = next(ln for ln in out[side_code] if ln[-1] == (100.0, 80.0))
    if end_y is None:
        assert side[0] == (100.0, y0), case
    else:
        assert side[0][1] == pytest.approx(end_y, abs=0.05), case
        assert side[0][0] == pytest.approx(100.0, abs=0.01), case
    assert MAIN in out[main_code]


# (případ, pahýl, překážka, zůstane)
@pytest.mark.parametrize(
    "case, stub, blocker, kept",
    [
        ("slepý pahýl 10 m", [(50.0, 0.0), (50.0, 10.0)], None, False),
        ("pahýl k budově", [(50.0, 0.0), (50.0, 10.0)], box(45, 10.5, 55, 20), True),
        ("slepá ulice 30 m", [(50.0, 0.0), (50.0, 30.0)], None, True),
        ("krátká spojka mezi silnicemi", [(50.0, 0.0), (50.0, 10.0)], "spojka", True),
        ("spojka na ZABAGED ulici", [(50.0, 0.0), (50.0, 10.0)], "zabaged", True),
        ("osamělý úlomek u budovy", [(50.0, 6.0), (50.0, 12.0)], box(45, 12.5, 55, 20), False),
    ],
)
def test_short_dead_end_stub_dropped(case, stub, blocker, kept):
    roads = [MAIN]
    fixed = None
    if blocker == "spojka":
        roads.append([(0.0, 10.0), (200.0, 10.0)])
    elif blocker == "zabaged":
        fixed = {"503": [[(0.0, 10.0), (200.0, 10.0)]]}
    blocker = None if isinstance(blocker, str) else blocker
    pieces = {"502": roads, "503": [stub]}
    out = tidy_road_ends(pieces, [], preset_id="forest_10000", scale=10000, blockers=blocker, fixed_by_code=fixed)
    assert (stub in out["503"]) is kept, case
    assert all(r in out["502"] for r in roads)


# (případ, pěšina, překážka, výřez, zůstane) – pěšiny jen pravidlo pahýlu (``stub_by_code``), nedotahují se
@pytest.mark.parametrize(
    "case, path, blocker, bounds, kept",
    [
        ("zbytek schodů 7 m ve vzduchu", [(50.0, 30.0), (50.0, 37.0)], None, None, False),
        ("pahýl pěšiny 10 m od silnice", [(50.0, 0.0), (50.0, 10.0)], None, None, False),
        ("pěšina 10 m od silnice k ploše", [(50.0, 0.0), (50.0, 10.0)], box(45, 10.5, 55, 20), None, True),
        ("zbytek schodů 7 m od ničeho k ploše", [(50.0, 30.0), (50.0, 37.0)], box(45, 37.5, 55, 45), None, False),
        ("pěšina 10 m končí na kresbě silnice", [(50.0, 4.0), (50.0, 14.0)], "spojka", None, True),
        ("pěšina 10 m k hranici výřezu", [(50.0, 0.0), (50.0, 10.0)], None, (0.0, -50.0, 200.0, 10.0), True),
        ("pěšina 20 m", [(50.0, 0.0), (50.0, 20.0)], None, None, True),
    ],
)
def test_short_path_stub_dropped_but_not_extended(case, path, blocker, bounds, kept):
    roads = [MAIN]
    if blocker == "spojka":
        roads.append([(0.0, 14.0), (200.0, 14.0)])  # pěšina končí na okraji kresby 502 (půlka 4,35 m) – napojená
    blocker = None if isinstance(blocker, str) else blocker
    out = tidy_road_ends(
        {"502": roads}, [], preset_id="forest_10000", scale=10000, blockers=blocker, stub_by_code={"505": [path]}, bounds=bounds
    )
    assert (path in out["505"]) is kept, case
    assert all(r in out["502"] for r in roads)
    # volný konec pěšiny se nedotahuje (jen silnice)
    assert all(ln == path for ln in out["505"])

