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
    # směr na východ → pravá strana je jih (-y); čárky jsou vpravo (TICKS_ON_LEFT=False)
    olive_north = {"olive": [_rect(-5, 0.1, 65, 10)], "public": [_rect(-5, -10, 65, -0.1)]}
    flipped = df.orient_fences([("518", list(FENCE_EAST))], olive_north)
    assert flipped[0][1] == list(reversed(FENCE_EAST))  # oliva vlevo, chodník vpravo → otočit
    same = df.orient_fences(
        [("518", list(FENCE_EAST))], {"olive": [_rect(-5, -10, 65, -0.1)], "public": [_rect(-5, 0.1, 65, 10)]}
    )
    assert same[0][1] == FENCE_EAST  # oliva vpravo → beze změny
    # jen chodník vlevo a nic vpravo → čárky vpravo míří pryč od chodníku → beze změny
    only_public_left = df.orient_fences([("518", list(FENCE_EAST))], {"olive": [], "public": [_rect(-5, 0.1, 65, 10)]})
    assert only_public_left[0][1] == FENCE_EAST


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

def test_filter_dubious_fences_drops_unknown_boundary_without_private_neighbour():
    garden = {"olive": [_rect(-5, 0.1, 65, 10)], "building": []}
    # skutečný plot u zahrady, zaměřený → zůstane; neznámý původ u zahrady → zůstane
    real = df.Fence(((0.0, 0.1), (60.0, 0.1)), "2", 0, True, "1", "a")
    old_near = df.Fence(((0.0, 0.0), (60.0, 0.0)), "2", 0, True, "99", "b")
    # neznámý původ, hranice jiného objektu, daleko od soukromé plochy → vynechat
    phantom = df.Fence(((0.0, -50.0), (30.0, -50.0)), "2", 0, True, "99", "c")
    # navazující úsek téže čáry (acq 99, bez hranice) → vynechat kaskádou
    tail = df.Fence(((30.0, -50.0), (60.0, -50.0)), "2", 0, False, "99", "d")
    kept, removed = df.filter_dubious_fences([real, old_near, phantom, tail], garden)
    assert {f.oid for f in removed} == {"c", "d"}
    assert {f.oid for f in kept} == {"a", "b"}
    # bez ploch se nic nevynechává
    assert df.filter_dubious_fences([phantom], {"olive": [], "building": []})[1] == []


def test_filter_dubious_fences_keeps_unknown_fence_touching_construction_border():
    garden = {"olive": [_rect(-5, 0.1, 65, 10)], "building": []}
    far_house = [(0.0, -50.0), (0.0, -45.0)]  # hranice stavby daleko od zahrady
    from_house = df.Fence(((0.0, -45.0), (40.0, -45.0)), "2", 0, True, "99", "h")  # plot od rohu stavby
    alone = df.Fence(((0.0, -80.0), (40.0, -80.0)), "2", 0, True, "99", "x")  # nikde nic
    kept, removed = df.filter_dubious_fences([from_house, alone], garden)
    assert {f.oid for f in removed} == {"h", "x"}  # bez hranic staveb oba pryč
    kept, removed = df.filter_dubious_fences([from_house, alone], garden, construction=[far_house])
    assert {f.oid for f in kept} == {"h"} and {f.oid for f in removed} == {"x"}


def test_fence_gap_connectors_join_facing_free_ends_only():
    from shapely.geometry import box

    left = ("518", [(0.0, 0.0), (20.0, 0.0)])
    right = ("518", [(24.0, 0.2), (50.0, 0.2)])  # mezera 4 m, souběžný směr
    assert df.fence_gap_connectors([left, right]) == [("518", [(20.0, 0.0), (24.0, 0.2)])]
    far = ("518", [(40.0, 0.0), (60.0, 0.0)])  # mezera 20 m
    assert df.fence_gap_connectors([left, far]) == []
    across = ("518", [(24.0, 0.0), (24.0, 20.0)])  # kolmý plot: konce na sebe nemíří
    assert df.fence_gap_connectors([left, across]) == []
    hedge = ("416", [(24.0, 0.2), (50.0, 0.2)])  # jiný druh plotu
    assert df.fence_gap_connectors([left, hedge]) == []
    assert df.fence_gap_connectors([left, right], forbidden=box(21, -5, 23, 5)) == []  # spojka by prošla budovou / cestou
    touching = ("518", [(20.0, 0.0), (20.0, 10.0)])  # konec levého plotu už se dotýká jiného → není volný
    assert df.fence_gap_connectors([left, right, touching]) == []
    # nutná podmínka: spojka u kreslené olivy
    olive = box(10, -10, 30, -0.5)  # okraj olivy 0,5 m pod spojkou
    assert len(df.fence_gap_connectors([left, right], near=olive)) == 1
    assert df.fence_gap_connectors([left, right], near=box(10, -30, 30, -10)) == []  # oliva daleko
    from shapely.geometry import Polygon

    assert df.fence_gap_connectors([left, right], near=Polygon()) == []  # žádná oliva = žádná spojka


def test_olive_geometry_includes_zabaged_settlement_layers(tmp_path):
    import io

    import shapefile

    import app.pipeline.dtm_import as di

    def layer(zf, stem, ring):
        shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
        with shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON) as w:
            w.field("id", "N")
            w.poly([ring])
            w.record(1)
        for ext, buf in (("shp", shp), ("shx", shx), ("dbf", dbf)):
            zf.writestr(f"{stem}.{ext}", buf.getvalue())

    with zipfile.ZipFile(tmp_path / "zabaged_clean.zip", "w") as zf:
        layer(zf, "OvocnySadZahrada", [(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)])
        layer(zf, "VodniPlocha", [(100, 100), (100, 110), (110, 110), (110, 100), (100, 100)])  # není olivová
    olive = di._olive_geometry(tmp_path, [])
    assert abs(olive.area - 100) < 1e-6  # jen zahrada, voda ne
    assert di._olive_geometry(tmp_path / "neexistuje", []).is_empty


def test_sprint_joins_fences_across_gap_into_one_line_with_ticks_toward_olive(tmp_path, monkeypatch):
    import io

    import shapefile

    import app.pipeline.dtm_import as di
    from app.pipeline.dtm_import import symbol_index_for_code

    (tmp_path / "dtm").mkdir()
    # dva ploty na jedné přímce s mezerou 4 m; oliva (ZABAGED zahrada) leží na pravé straně (y < 0) ve směru zleva doprava
    fences = [
        {"code": "518", "coords": [[-1000.0, 0.0], [-980.0, 0.0]]},
        {"code": "518", "coords": [[-976.0, 0.0], [-950.0, 0.0]]},
    ]
    (tmp_path / "dtm" / "fences.json").write_text(json.dumps({"fences": fences}), encoding="utf-8")
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON) as w:
        w.field("id", "N")
        w.poly([[(-1010, -20), (-1010, 0.3), (-940, 0.3), (-940, -20), (-1010, -20)]])
        w.record(1)
    with zipfile.ZipFile(tmp_path / "zabaged_clean.zip", "w") as zf:
        for ext, buf in (("shp", shp), ("shx", shx), ("dbf", dbf)):
            zf.writestr(f"OvocnySadZahrada.{ext}", buf.getvalue())
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-975.0, ref_y=0.0, grivation_deg=0.0)
    oriented = []
    real_orient = di.dtm_fences.orient_fences

    def spy(lines, areas, **kwargs):
        out = real_orient(lines, areas, **kwargs)
        oriented.extend(out)
        return out

    monkeypatch.setattr(di.dtm_fences, "orient_fences", spy)
    parts = di.build_dtm_fence_parts(tmp_path, **kw)
    fence_part = [p for p in parts if p.name.startswith("Ploty")][0]
    assert fence_part.count == 1  # dva ploty + spojka = jedna linie
    assert len(oriented) == 1 and len(oriented[0][1]) >= 3  # slitá linie vede přes obě části i mezeru
    start, end = oriented[0][1][0], oriented[0][1][-1]
    assert not di.dtm_fences.TICKS_ON_LEFT and end[0] - start[0] > 40  # oliva vpravo od směru kreslení → čárky do olivy
    assert f'symbol="{symbol_index_for_code("sprint_4000", 4000, "518")}"' in fence_part.objects_xml


def test_cut_fences_by_buildings_removes_fence_on_outline_but_keeps_others():
    import app.pipeline.dtm_import as di
    from shapely.geometry import Polygon

    zone = Polygon([(0, 0), (20, 0), (20, 10), (0, 10)]).buffer(di.FENCE_BUILDING_TOL_M)
    on_outline = ("518", [(0.0, 0.0), (20.0, 0.0)])  # celý po obrysu budovy → pryč
    away = ("518", [(0.0, 30.0), (40.0, 30.0)])  # daleko → beze změny
    runs_off = ("518", [(20.0, 5.0), (60.0, 5.0)])  # od budovy pryč → zůstane jen venkovní část
    out = di.cut_fences_by_buildings([on_outline, away, runs_off], zone)
    assert away in out and on_outline not in out
    pieces = [ln for _c, ln in out if ln not in (away[1],)]
    assert len(pieces) == 1 and min(p[0] for p in pieces[0]) >= 20.4


def test_filter_dubious_fences_drops_unknown_fence_inside_single_area():
    park = {"olive": [_rect(-100, -100, 100, 100)], "open": [], "building": [_rect(500, 500, 510, 510)]}
    inner = df.Fence(((0.0, 0.0), (40.0, 0.0)), "2", 0, False, "99", "p")
    surveyed = df.Fence(((0.0, 10.0), (40.0, 10.0)), "2", 0, False, "1", "s")
    kept, removed = df.filter_dubious_fences([inner, surveyed], park)
    assert [f.oid for f in removed] == ["p"] and [f.oid for f in kept] == ["s"]
    # budova v okolí (do 15 m) → plot se ponechá
    near_house = dict(park, building=[_rect(5, 8, 12, 14)])
    assert df.filter_dubious_fences([inner], near_house)[1] == []
    # hranice dvou ploch (jiná plocha na druhé straně) → ponechat
    two = {"olive": [_rect(-100, 0.1, 100, 100)], "open": [_rect(-100, -100, 100, -0.1)], "building": []}
    assert df.filter_dubious_fences([inner], two)[1] == []


def test_orient_fences_setback_moves_fence_from_road_toward_olive():
    olive_north = {"olive": [_rect(-5, 0.1, 65, 10)], "public": [_rect(-5, -10, 65, -0.1)]}
    out = df.orient_fences([("518", list(FENCE_EAST))], olive_north, setback_m=1.5)
    ys = {round(y, 2) for _, y in out[0][1]}
    assert ys == {1.5}  # posun k olivě (sever), čárky dál ke chodníku/olivě dle orientace
    south = {"olive": [_rect(-5, -10, 65, -0.1)], "public": [_rect(-5, 0.1, 65, 10)]}
    out = df.orient_fences([("416", list(FENCE_EAST))], south, setback_m=1.5)
    assert {round(y, 2) for _, y in out[0][1]} == {-1.5}
    # bez cesty (jen olivy po obou stranách) se neposouvá
    both = {"olive": [_rect(-5, 0.1, 65, 10), _rect(-5, -10, 65, -0.1)], "public": []}
    assert df.orient_fences([("518", list(FENCE_EAST))], both, setback_m=1.5)[0][1] == FENCE_EAST
    # výchozí setback 0 = beze změny polohy
    assert {y for _, y in df.orient_fences([("518", list(FENCE_EAST))], olive_north)[0][1]} == {0.0}


def _square_ring(x0, y0, size):
    return [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size), (x0, y0)]


def test_drop_fences_inside_enclosures_keeps_outer_ring_and_outside_lines():
    ring = ("518", _square_ring(0, 0, 40))
    inner_line = ("518", [(10.0, 10.0), (30.0, 10.0)])
    inner_ring = ("518", _square_ring(10, 10, 8))
    outside = ("518", [(60.0, 0.0), (90.0, 0.0)])
    crossing = ("518", [(20.0, 20.0), (80.0, 20.0)])  # vyčnívá ven → ponechat
    kept, n = df.drop_fences_inside_enclosures([ring, inner_line, inner_ring, outside, crossing])
    assert n == 2
    assert [c for _k, c in kept] == [ring[1], outside[1], crossing[1]]


def test_fence_faces_find_enclosure_made_of_several_lines_with_t_junction():
    lines = [
        ("518", [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0)]),
        ("518", [(40.0, 40.0), (0.0, 40.0), (0.0, 0.0)]),
        ("518", [(20.0, 0.0), (20.0, -15.0)]),  # vyčnívá ven – žádná plocha
    ]
    faces = df.fence_faces(lines)
    assert len(faces) == 1 and abs(faces[0].area - 1600.0) < 1e-6


def test_drop_inside_removes_dangling_line_in_face_but_keeps_divider_between_faces():
    box = [("518", [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0)]), ("518", [(40.0, 40.0), (0.0, 40.0), (0.0, 0.0)])]
    dangling = ("518", [(10.0, 10.0), (20.0, 10.0)])
    divider = ("518", [(20.0, 0.0), (20.0, 40.0)])  # spojuje dvě strany → hranice dvou ploch
    kept, n = df.drop_fences_inside_enclosures(box + [dangling, divider])
    assert n == 1 and dangling not in kept and divider in kept


def test_private_enclosure_rings_fill_closed_fences_by_size_and_olive():
    ring = ("518", _square_ring(0, 0, 40))
    rings = df.private_enclosure_rings([ring], {"olive": []})
    assert len(rings) == 1 and rings[0][0] == rings[0][-1]  # uzavřené oplocení se vyplní i bez zahrady
    assert df.private_enclosure_rings([("518", [(0.0, 0.0), (30.0, 0.0)])], {"olive": []}) == []  # nezavřený
    narrow = ("518", [(0.0, 0.0), (40.0, 0.0), (40.0, 1.5), (0.0, 1.5), (0.0, 0.0)])
    assert df.private_enclosure_rings([narrow], {"olive": []}) == []  # úzký pruh
    big = ("518", _square_ring(0, 0, 200))  # 40 000 m2 > limit
    assert df.private_enclosure_rings([big], {"olive": []}) == []
    assert len(df.private_enclosure_rings([big], {"olive": [_rect(-5, -5, 205, 205)]})) == 1  # zahrada → vyplnit


def test_avoid_paths_pushes_fence_away_from_black_path_only_where_needed():
    fence = ("518", [(0.0, 0.5), (40.0, 0.5)])  # 0,5 m od osy pěšiny
    far = ("518", [(0.0, 20.0), (40.0, 20.0)])
    path = [([(-10.0, 0.0), (50.0, 0.0)], 1.5)]
    (_c1, moved), (_c2, untouched) = df.avoid_paths([fence, far], path)
    assert min(y for _x, y in moved) >= 1.4  # odsunuto na stranu, kde plot byl
    assert untouched == far[1]


def test_avoid_paths_keeps_closed_ring_closed():
    ring = ("518", _square_ring(0, 0, 20))
    path = [([(-5.0, 10.0), (-5.0, 11.0)], 8.0)]
    (_c, new), = df.avoid_paths([ring], path)
    assert new[0] == new[-1]


def test_private_enclosure_rings_plot_closed_by_road_edge_with_house():
    # plot ze tří stran, čtvrtou stranu (u cesty) uzavírá okraj zpevněné plochy; uvnitř budova
    fence = ("518", [(0.0, 0.0), (0.0, 30.0), (20.0, 30.0), (20.0, 0.0)])
    road = _rect(-30, -20, 60, 0.0)  # cesta pod parcelou, horní okraj y=0
    areas = {"olive": [], "public": [road], "building": [_rect(5, 10, 15, 20)], "open": []}
    rings = df.private_enclosure_rings([fence], areas)
    assert len(rings) == 1
    xs = [x for x, _y in rings[0]]
    ys = [y for _x, y in rings[0]]
    assert min(xs) == 0.0 and max(xs) == 20.0 and min(ys) == 0.0 and max(ys) == 30.0
    # bez budovy (hřiště, prázdná plocha) se nevyplňuje
    assert df.private_enclosure_rings([fence], dict(areas, building=[])) == []
    # velký areál (nad limit) se nevyplňuje
    big = ("518", [(0.0, 0.0), (0.0, 400.0), (400.0, 400.0), (400.0, 0.0)])
    big_areas = {"olive": [], "public": [_rect(-30, -20, 460, 0.0)], "building": [_rect(100, 100, 120, 120)], "open": []}
    assert df.private_enclosure_rings([big], big_areas) == []


def test_private_enclosure_rings_bridge_small_gaps_but_not_large_ones():
    def ring_with_gap(gap):
        return [
            ("518", [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0)]),
            ("518", [(40.0, 40.0), (0.0, 40.0), (0.0, gap)]),  # poslední strana nedotažená o ``gap``
        ]

    assert len(df.private_enclosure_rings(ring_with_gap(3.0), {"olive": []})) == 1  # mezera 3 m → uzavřeno
    assert df.private_enclosure_rings(ring_with_gap(15.0), {"olive": []}) == []  # velká mezera = vstup
    # překlenutí se nekreslí: čáry zůstávají, jak jsou
    assert len(df.gap_bridges(ring_with_gap(3.0))) >= 1
