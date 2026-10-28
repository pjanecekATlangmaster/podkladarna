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



# (případ, preset, kód hlavní, kód rampy, začátek rampy, OSM rampa k pruhu, očekávané y začátku)
# Rampa odbočující z pruhu rozdělené vozovky: OSM čára vede do kresby hlavní (pruh uvnitř pásu), ale její osu neprotne.
# mtbo_10000: 502.0 = 2,22 mm (půlka 11,1 m); forest_10000: 502 = 0,87 mm (půlka 4,35 m), 503 černá.
@pytest.mark.parametrize(
    "case, preset, main_code, ramp_code, start, osm_head, end_y",
    [
        ("rampa s výplní na okraj kresby a k ose", "mtbo_10000", "502.0", "502.2", (125.0, 14.0), [(100.0, 6.0), (112.0, 10.0)], 0.0),
        ("černá cesta končí na okraji kresby", "forest_10000", "502", "503", (125.0, 8.0), [(100.0, 2.0), (112.0, 4.0)], 4.35),
        ("mezera do kresby nad 1,5 mm", "mtbo_10000", "502.0", "502.2", (135.0, 18.0), [(100.0, 6.0), (115.0, 12.0)], None),
        # OSM pruh běží souběžně na hraně kresby 502.0 (y = 11,5–12), konec 0,9 m od kresby: kolmo k ose
        ("souběžný pruh na hraně kresby, OSM dál vede do ní: kolmo k ose", "mtbo_10000", "502.0", "502.2", (125.0, 12.0), [(40.0, 8.0), (60.0, 11.5), (100.0, 11.8)], "hook"),
        ("souběžný pruh 6 m od kresby zůstává", "mtbo_10000", "502.0", "502.2", (125.0, 17.5), [(40.0, 8.0), (60.0, 17.0), (100.0, 17.3)], None),
        ("slepá obslužná vedle ulice (OSM do kresby nevede) zůstává", "mtbo_10000", "502.0", "502.2", (125.0, 12.0), [(60.0, 11.5), (100.0, 11.8)], None),
    ],
)
def test_free_end_extended_into_band_without_axis_hit(case, preset, main_code, ramp_code, start, osm_head, end_y):
    ramp = [start, (160.0, 30.0), (190.0, 60.0)]
    pieces = {main_code: [MAIN], ramp_code: [ramp]}
    out = tidy_road_ends(pieces, [MAIN, [*osm_head, *ramp]], preset_id=preset, scale=10000)
    side = next(ln for ln in out[ramp_code] if ln[-1] == (190.0, 60.0))
    assert side[-3:] == ramp, case
    if end_y is None:
        assert side[0] == start, case
    elif end_y == "hook":
        assert side[0] == pytest.approx((start[0], 0.0), abs=0.05) and side[1] == start, case
    elif end_y == 0.0:
        assert side[0][1] == pytest.approx(0.0, abs=0.05), case  # kolmo k ose
        assert abs(side[1][1]) <= 11.1 + 1.0, case  # předtím vstup do kresby po OSM čáře
    else:
        assert side[0][1] == pytest.approx(end_y, abs=1.0), case  # na okraji kresby
