"""Tvar hranic porostů: vyhlazení bez posunu hranice a bez přelití přes překážky."""

import pytest
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from app.pipeline.job_options import resolve_contour_smooth, resolve_veg_smooth
from app.pipeline.veg_smooth import SmoothBarriers, _curve_area_gain, smooth_area


def _stair_diagonal(n: int = 40, step: float = 1.0):
    """Čtverec, jehož pravý horní okraj tvoří diagonální schody po 1 m."""
    pts = [(0.0, 0.0), (100.0, 0.0)]
    x, y = 100.0, 0.0
    for _ in range(n):
        y += step
        pts.append((x, y))
        x -= step
        pts.append((x, y))
    pts.append((0.0, y))
    pts.append((0.0, 0.0))
    return pts


def test_smooth_reduces_vertices():
    ring = _stair_diagonal()
    out = smooth_area(ring, [], 2.0)
    assert len(out) == 1
    assert len(out[0][0]) < len(ring) / 3


def test_smooth_does_not_grow_into_barrier():
    ring = _stair_diagonal()
    poly = Polygon(ring)
    # cesta těsně podél diagonály schodů – zjednodušení by do ní zasáhlo
    road = LineString([(100, 0), (60, 40)]).buffer(1.5)
    free = Polygon(*smooth_area(ring, [], 2.0)[0])
    guarded = Polygon(*smooth_area(ring, [], 2.0, SmoothBarriers([road]))[0])
    base = poly.intersection(road).area
    assert guarded.intersection(road).area <= base + 1e-6
    assert free.intersection(road).area >= guarded.intersection(road).area


def test_smooth_drops_island_behind_barrier():
    ring = [(0, 0), (50, 0), (50, 50), (0, 50), (0, 0)]
    out = smooth_area(ring, [], 2.0, SmoothBarriers([]))
    assert len(out) == 1


def test_resolve_veg_smooth_default_and_values():
    assert resolve_veg_smooth({}) == 1
    assert resolve_veg_smooth({"veg_smooth": "3"}) == 3
    assert resolve_veg_smooth({"veg_smooth": "ROUND"}) == 1  # starý přepínač
    assert resolve_veg_smooth({"veg_smooth": "off"}) == 0
    assert resolve_veg_smooth({"veg_smooth": "9"}) == 1
    assert resolve_veg_smooth({"veg_smooth": "x"}) == 1
    assert resolve_veg_smooth({"veg_smooth": "0"}) == 0  # vypnuto
    assert resolve_veg_smooth({"veg_smooth": 0}) == 0


@pytest.mark.parametrize(
    "opts, expected",
    [({}, 0), (None, 0), ({"contour_smooth": "1"}, 1), ({"contour_smooth": 1}, 1),
     ({"contour_smooth": "0"}, 0), ({"contour_smooth": "2"}, 0), ({"contour_smooth": "x"}, 0)],
)
def test_resolve_contour_smooth(opts, expected):
    assert resolve_contour_smooth(opts) == expected


def test_higher_level_removes_small_notches():
    # čtverec 80 m s úzkou zátokou (3 m) – stupeň 2 ji zalije
    ring = [(0, 0), (80, 0), (80, 80), (41, 80), (41, 40), (39, 40), (39, 80), (0, 80), (0, 0)]
    notch = Point(40, 60)
    lvl1 = [Polygon(*p) for p in smooth_area(ring, [], 1)]
    lvl2 = [Polygon(*p) for p in smooth_area(ring, [], 2)]
    assert not any(p.contains(notch) for p in lvl1)
    assert any(p.contains(notch) for p in lvl2)


@pytest.mark.parametrize("as_curves", [False, True])
def test_smooth_level1_keeps_boundary(as_curves):
    # stupeň 1: plocha ±1 %, obrys se od původního vzdálí nejvýš o max_shift (1,5 m) + půl pixelu
    # a tolerance DP; mez 2,2 m je rozumná horní hranice, ne přesná hodnota.
    ring = _stair_diagonal()
    orig = Polygon(ring)
    out = smooth_area(ring, [], 1, as_curves=as_curves)
    shaped = unary_union([Polygon(*p) for p in out])
    # s křivkami se plocha dorovná i o jejich zaoblení – měří se kreslená plocha
    drawn = shaped.area + (_curve_area_gain(shaped) if as_curves else 0.0)
    assert drawn == pytest.approx(orig.area, rel=0.01)
    if not as_curves:
        assert shaped.exterior.hausdorff_distance(orig.exterior) <= 2.2


def test_smooth_level3_keeps_narrow_area():
    ring = [(0, 0), (30, 0), (30, 8), (0, 8), (0, 0)]  # 8 m < 2 × 6 m
    out = smooth_area(ring, [], 3)
    assert out
    assert sum(Polygon(*p).area for p in out) > 100


def test_shaped_parts_by_mode():
    from app.pipeline.vegetation_gdal import SPRINT_SIMPLIFY_M, _shaped_parts

    poly = Polygon(_stair_diagonal())
    wkb = poly.wkb

    parts, curves = _shaped_parts(wkb, "406", 2, None, True)
    assert curves is False
    ring = next(p[1] for p in parts if p[0] == "line")
    expected = poly.simplify(SPRINT_SIMPLIFY_M, preserve_topology=True)
    assert Polygon(ring).symmetric_difference(expected).area < 1e-6

    parts, curves = _shaped_parts(wkb, "406", 0, None, False)
    assert curves is False
    ring = next(p[1] for p in parts if p[0] == "line")
    assert list(ring) == list(poly.exterior.coords)

    parts, curves = _shaped_parts(wkb, "406", 1, None, False)
    assert curves is True


def test_resolve_use_dtm_defaults_and_values():
    from app.pipeline.job_options import resolve_use_dtm

    assert resolve_use_dtm({}, "sprint_2m") is True
    assert resolve_use_dtm({}, "forest_10000") is False  # od 2.43.0 les a MTBO výchozí OSM + ZABAGED
    assert resolve_use_dtm({}, "mtbo_15000") is False
    assert resolve_use_dtm({"dtm_forest": "0"}, "forest_10000") is False  # záložní kresba OSM + ZABAGED
    assert resolve_use_dtm({"dtm_sprint": "0"}, "sprint_2m") is False
    assert resolve_use_dtm({"dtm_sprint": False}, "sprint_2m") is False
    assert resolve_use_dtm({"dtm_forest": "1"}, "forest_7500") is True
    assert resolve_use_dtm({"dtm_forest": "on"}, "mtbo_10000") is True
    assert resolve_use_dtm({"dtm_forest": True, "dtm_sprint": False}, "sprint_2m") is False
    assert resolve_use_dtm({"dtm_sprint": "x"}, "sprint_2m") is True


def test_shaped_parts_memo_shared_by_maps(tmp_path, monkeypatch):
    """Les a MTBO jednoho jobu vyhlazují tentýž vegetation.shp jen jednou; jiný soubor / stupeň počítá znovu."""
    import app.pipeline.vegetation_gdal as vg

    rows = [({"cls": 4, "code": "410"}, Polygon(_stair_diagonal()).wkb)]
    monkeypatch.setattr(vg, "_iter_vege_rows", lambda _shp: rows)
    calls = []
    real = vg._shaped_parts
    monkeypatch.setattr(vg, "_shaped_parts", lambda *a: calls.append(a[2]) or real(*a))
    (tmp_path / "vegetation").mkdir()
    shp = tmp_path / "vegetation" / "vegetation.shp"
    shp.write_bytes(b"placeholder")
    kw = dict(scale=10000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0, smooth_level=1)
    forest = vg.build_vegetation_parts(tmp_path, preset_id="forest_10000", **kw)
    mtbo = vg.build_vegetation_parts(tmp_path, preset_id="mtbo_10000", **kw)
    assert forest and mtbo and calls == [1]
    assert [p.objects_xml for p in vg.build_vegetation_parts(tmp_path, preset_id="forest_10000", **kw)] == [
        p.objects_xml for p in forest
    ]
    vg.build_vegetation_parts(tmp_path, preset_id="forest_10000", **{**kw, "smooth_level": 2})
    shp.write_bytes(b"placeholder, nova data")
    vg.build_vegetation_parts(tmp_path, preset_id="forest_10000", **kw)
    assert calls == [1, 2, 1]
