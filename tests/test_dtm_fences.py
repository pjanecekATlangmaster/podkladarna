"""Ploty z DTM: napojení úseků v rozích a načtení z JVF s cache."""

from __future__ import annotations

import json
import zipfile

import app.pipeline.dtm_fences as df
from app.pipeline.dtm_fences import Fence


def _f(coords, druh="2", level=0, boundary=False):
    return Fence(tuple(coords), druh, level, boundary)


def test_merge_joins_corner_and_straight_pieces_into_one_line():
    # tři úseky tvoří písmeno L; mezery 0,1 m jsou pod tolerancí
    lines = [
        [(0.0, 0.0), (10.0, 0.0)],
        [(10.05, 0.05), (10.0, 8.0)],
        [(10.0, 8.1), (4.0, 8.0)],
    ]
    out = df.merge_lines(lines)
    assert len(out) == 1
    pts = out[0]
    assert len(pts) == 4  # 2 + 1 + 1 body (spojované konce splynuly)
    assert {pts[0], pts[-1]} == {(0.0, 0.0), (4.0, 8.0)}


def test_merge_keeps_gap_larger_than_tolerance_and_t_junction_split():
    far = [[(0.0, 0.0), (10.0, 0.0)], [(11.0, 0.0), (20.0, 0.0)]]  # mezera 1 m
    assert len(df.merge_lines(far)) == 2
    # T: dvě ramena a stonek se potkají v (5, 0) → uzel se třemi úseky se nespojuje
    tee = [[(0.0, 0.0), (5.0, 0.0)], [(5.0, 0.0), (10.0, 0.0)], [(5.0, 0.0), (5.0, 6.0)]]
    assert len(df.merge_lines(tee)) == 3


def test_merge_removes_duplicates_and_zero_length_and_closes_ring():
    dup = [[(0.0, 0.0), (5.0, 0.0)], [(5.0, 0.0), (0.0, 0.0)], [(1.0, 1.0), (1.0, 1.0)]]
    assert len(df.merge_lines(dup)) == 1
    ring = [
        [(0.0, 0.0), (10.0, 0.0)],
        [(10.0, 0.0), (10.0, 10.0)],
        [(10.0, 10.0), (0.0, 10.0)],
        [(0.0, 10.0), (0.0, 0.0)],
    ]
    out = df.merge_lines(ring)
    assert len(out) == 1 and out[0][0] == out[0][-1]  # uzavřený plot = jeden kruh
    assert df.merge_lines([]) == []


def test_merged_fences_groups_by_code_and_skips_underground():
    fences = [
        _f([(0, 0), (10, 0)], druh="2"),            # drátěný
        _f([(10, 0), (10, 10)], druh="4"),          # zděný – stejný symbol 518 → spojí se
        _f([(10, 10), (20, 10)], druh="5"),         # živý plot → 416, zvlášť
        _f([(50, 50), (60, 50)], level=-1),         # pod povrchem → vynechat
    ]
    out = df.merged_fences(fences)
    codes = sorted(c for c, _ in out)
    assert codes == ["416", "518"]
    fence518 = next(ln for c, ln in out if c == "518")
    assert len(fence518) == 3  # (0,0) → (10,0) → (10,10)
    assert len(next(ln for c, ln in out if c == "416")) == 2


def _jvf_zip(tmp_path):
    plot = """
      <plotli:ZaznamObjektu><plotli:AtributyObjektu>
        <atr:SpolecneAtributyObjektuZPS><atr:UrovenUmisteniObjektuZPS>0</atr:UrovenUmisteniObjektuZPS></atr:SpolecneAtributyObjektuZPS>
        <atr:DruhPlotu>{druh}</atr:DruhPlotu><atr:HraniceJinehoObjektu>{hr}</atr:HraniceJinehoObjektu>
      </plotli:AtributyObjektu><plotli:GeometrieObjektu><gml:curveProperty>
        <gml:LineString srsName="EPSG:5514" srsDimension="3"><gml:posList>{pos}</gml:posList></gml:LineString>
      </gml:curveProperty></plotli:GeometrieObjektu></plotli:ZaznamObjektu>"""
    body = plot.format(druh="2", hr="true", pos="-1 -2 9 -3 -4 9") + plot.format(
        druh="5", hr="false", pos="0 0 1 5 5 1 9 9 1"
    )
    xml = (
        '<?xml version="1.0" encoding="utf-8"?><objtyp:JVFDTM xmlns:objtyp="objtyp" xmlns:plotli="plotli" '
        'xmlns:atr="atr" xmlns:gml="http://www.opengis.net/gml/3.2"><objtyp:DataJVFDTM><objtyp:Data>'
        f"<objtyp:Plot>{body}</objtyp:Plot></objtyp:Data></objtyp:DataJVFDTM></objtyp:JVFDTM>"
    )
    z = tmp_path / "VS_test_ZPS_KR.jvf.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("x.jvf.xml", xml)
    return z


def test_load_fences_reads_jvf_and_uses_cache(tmp_path, monkeypatch):
    z = _jvf_zip(tmp_path)
    fences = df.load_fences(z)
    assert len(fences) == 2
    assert fences[0].druh == "2" and fences[0].boundary is True
    assert fences[0].coords == ((-1.0, -2.0), (-3.0, -4.0))
    assert fences[1].code == "416" and len(fences[1].coords) == 3
    cache = z.with_name(z.name + df.CACHE_SUFFIX)
    assert cache.is_file() and len(json.loads(cache.read_text(encoding="utf-8"))) == 2
    # druhé načtení jde z cache (čtečka se nevolá)
    monkeypatch.setattr(df, "iter_records", lambda *a, **k: (_ for _ in ()).throw(AssertionError("cache")))
    assert df.load_fences(z) == fences


def test_fences_in_bounds_filters_by_extent():
    fs = [_f([(0, 0), (10, 0)]), _f([(100, 100), (110, 100)])]
    assert df.fences_in_bounds(fs, (-5, -5, 20, 20)) == [fs[0]]
    assert df.fences_in_bounds(fs, (500, 500, 600, 600)) == []

def _rect(x0, y0, x1, y1):
    return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]


FENCE_EAST = [(0.0, 0.0), (60.0, 0.0)]


def test_orient_fence_ticks_toward_olive_not_sidewalk():
    # směr na východ → levá strana je sever (+y)
    olive_north = {"olive": [_rect(-5, 0.1, 65, 10)], "public": [_rect(-5, -10, 65, -0.1)]}
    same = df.orient_fences([("518", list(FENCE_EAST))], olive_north)
    assert same[0][1] == FENCE_EAST  # oliva vlevo, chodník vpravo → beze změny
    flipped = df.orient_fences(
        [("518", list(FENCE_EAST))], {"olive": [_rect(-5, -10, 65, -0.1)], "public": [_rect(-5, 0.1, 65, 10)]}
    )
    assert flipped[0][1] == list(reversed(FENCE_EAST))  # oliva vpravo → otočit
    # jen chodník vlevo a nic vpravo → čárky mají mířit pryč od chodníku → otočit
    only_public_left = df.orient_fences([("518", list(FENCE_EAST))], {"olive": [], "public": [_rect(-5, 0.1, 65, 10)]})
    assert only_public_left[0][1] == list(reversed(FENCE_EAST))


def test_orient_fence_keeps_ties_hedges_and_respects_switch():
    both_public = {"olive": [], "public": [_rect(-5, 0.1, 65, 10), _rect(-5, -10, 65, -0.1)]}
    assert df.orient_fences([("518", list(FENCE_EAST))], both_public)[0][1] == FENCE_EAST
    assert df.orient_fences([("518", list(FENCE_EAST))], {"olive": [], "public": []})[0][1] == FENCE_EAST
    # živý plot (416) čárky nemá → neotáčí se
    olive_south = {"olive": [_rect(-5, -10, 65, -0.1)], "public": []}
    assert df.orient_fences([("416", list(FENCE_EAST))], olive_south)[0][1] == FENCE_EAST
    # přepínač strany čárek
    assert df.orient_fences([("518", list(FENCE_EAST))], olive_south, ticks_on_left=False)[0][1] == FENCE_EAST
    assert df.orient_fences([("518", list(FENCE_EAST))], olive_south, ticks_on_left=True)[0][1] == list(reversed(FENCE_EAST))


def test_load_orientation_areas_reads_opl_and_caches(tmp_path):
    import zipfile as zf_mod

    def zaznam(ring):
        pos = " ".join(f"{x} {y}" for x, y in ring)
        return (
            "<z:ZaznamObjektu><z:AtributyObjektu/><z:GeometrieObjektu><gml:surfaceProperty>"
            '<gml:Polygon srsName="EPSG:5514" srsDimension="2"><gml:exterior><gml:LinearRing>'
            f"<gml:posList>{pos}</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon>"
            "</gml:surfaceProperty></z:GeometrieObjektu></z:ZaznamObjektu>"
        )

    xml = (
        '<objtyp:JVFDTM xmlns:objtyp="objtyp" xmlns:z="z" xmlns:gml="http://www.opengis.net/gml/3.2">'
        "<objtyp:Data>"
        f"<objtyp:ZahradaPlocha>{zaznam(_rect(0, 0, 10, 10)[0])}</objtyp:ZahradaPlocha>"
        f"<objtyp:ChodnikPlocha>{zaznam(_rect(20, 0, 30, 10)[0])}</objtyp:ChodnikPlocha>"
        f"<objtyp:BudovaPlocha>{zaznam(_rect(40, 0, 50, 10)[0])}</objtyp:BudovaPlocha>"
        "</objtyp:Data></objtyp:JVFDTM>"
    )
    z = tmp_path / "VS_test_ZPS_OPL.jvf.zip"
    with zf_mod.ZipFile(z, "w") as zf:
        zf.writestr("x.jvf.xml", xml)
    areas = df.load_orientation_areas(z)
    assert len(areas["olive"]) == 1 and len(areas["public"]) == 1  # budova se nebere
    assert areas["olive"][0][0][0] == (0.0, 0.0)
    assert z.with_name(z.name + df.AREAS_CACHE_SUFFIX).is_file()
    assert df.load_orientation_areas(z) == areas  # z cache
