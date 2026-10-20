import json

from app.pipeline import dtm_import as di
from app.pipeline import dtm_sidewalks as ds


def _rect(x0, y0, x1, y1):
    return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]


def test_unify_drops_osm_inside_dtm_and_keeps_remainder():
    dtm = [_rect(0, 0, 30, 2)]
    inside = [(2.0, 1.0), (28.0, 1.0)]  # celé uvnitř DTM chodníku → duplicitní
    extra = [(30.0, 1.0), (30.0, 21.0)]  # navazuje na konec DTM, DTM ho nezná
    geom = ds.unify_sidewalks(dtm, [inside, extra])
    assert geom.geom_type == "Polygon"  # zbytek se napojil na DTM
    assert geom.bounds[3] > 20.0  # zbytek z OSM je v ploše
    assert geom.area < 30 * 2 + 20 * 1.4 + 5


def test_unify_without_osm_remainder_is_dtm_only():
    geom = ds.unify_sidewalks([_rect(0, 0, 30, 2)], [[(2.0, 1.0), (28.0, 1.0)]])
    assert abs(geom.area - 60.0) < 1.0


def test_unify_filters_short_remainders_and_small_holes():
    ring = _rect(0, 0, 20, 20)
    ring.append([(5, 5), (5.5, 5), (5.5, 5.5), (5, 5.5), (5, 5)])  # díra 0,25 m²
    geom = ds.unify_sidewalks([ring], [[(20.0, 10.0), (21.5, 10.0)]])  # zbytek 1,5 m → zahodit
    assert not list(geom.interiors)
    assert geom.bounds[2] < 20.5


def test_unify_osm_only_when_no_dtm_polygon():
    geom = ds.unify_sidewalks([], [[(0.0, 0.0), (20.0, 0.0)]])
    assert abs(geom.area - 20 * ds.OSM_WIDTH_M) < 3.0


def test_unify_drops_parallel_osm_line_beside_dtm_but_keeps_extension():
    dtm = [_rect(0, 0, 40, 2)]
    parallel = [(2.0, 3.5), (38.0, 3.5)]  # posunutý OSM chodník 1,5 m od plochy
    away = [(40.0, 1.0), (40.0, 31.0)]  # pokračování kolmo pryč
    geom = ds.unify_sidewalks(dtm, [parallel])
    assert abs(geom.area - 80.0) < 1.0  # souběžná čára se nekreslí
    geom = ds.unify_sidewalks(dtm, [parallel, away])
    assert geom.bounds[3] > 30.0 and geom.area < 80 + 30 * 1.4 + 5


def test_clip_outside_cuts_footway_inside_dtm():
    cover = ds.dtm_union([_rect(10, -2, 20, 2)]).buffer(ds.OSM_COVER_M)
    pieces = ds.clip_outside([(0.0, 0.0), (40.0, 0.0)], cover)
    assert len(pieces) == 2
    assert max(x for x, _y in pieces[0]) < 10 and min(x for x, _y in pieces[1]) > 20


def test_osm_paved_area_overlapping_dtm_sidewalk_is_merged_not_drawn_twice(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "sidewalks.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1000, -960, -990)]]}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()

    def area(kind, x0, y0, x1, y1):
        ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
        return {
            "type": "Feature",
            "properties": {"source": "osm", "kind": kind, "oom_code": "501"},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }

    feats = [area("pedestrian_area", -980, -995, -950, -970), area("parking", -900, -900, -880, -880)]
    (work / "osm_paths" / "features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-985.0, ref_y=-999.0, grivation_deg=0.0)
    names = [p.name for p in op.build_osm_feature_parts(work, **kw)]
    assert "OSM pěší zóny (501 zpevněná)" not in names  # překrývá chodník → sloučeno s ním
    assert "OSM parkoviště (501 zpevněná)" in names  # samostatná plocha zůstává
    parts = di.build_dtm_sidewalk_parts(work, **kw)
    assert parts[0].count == 1  # jedna sloučená plocha


def test_road_parts_replace_osm_road_lines_and_keep_widths(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "roads.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1004, -940, -996)]]}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()

    def line(hw, a, b):
        return {
            "type": "Feature",
            "properties": {"highway": hw},
            "geometry": {"type": "LineString", "coordinates": [a, b]},
        }

    feats = [
        line("road_3", [-1000, -1000], [-940, -1000]),  # uvnitř DTM plochy → duplicitní
        line("road_3", [-940, -1000], [-900, -1000]),  # pokračování za plochou → doplní se
        line("bridge:road_3", [-960, -1010], [-960, -990]),  # most zůstává čárou
        line("path", [-1000, -980], [-940, -980]),
    ]
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-960.0, ref_y=-1000.0, grivation_deg=0.0)
    parts = di.build_dtm_road_parts(work, **kw)
    assert [p.name for p in parts] == ["Silnice (DTM + OSM)"] and parts[0].count == 1  # jedna sloučená plocha
    assert di.is_surface_road("road_2") and not di.is_surface_road("bridge:road_2")
    paths = op.build_osm_path_parts(work, **kw)
    assert paths and paths[0].count == 4  # most + pěšina + 2 hrany lávky 512.100; silnice nahradila plocha
    assert di.build_dtm_road_parts(work, **{**kw, "preset_id": "forest_10000"}) == []


def test_sidewalk_line_crossing_road_is_not_drawn_over_the_road(tmp_path, monkeypatch):
    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "roads.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1004, -940, -996)]]}),
        encoding="utf-8",
    )
    (work / "dtm" / "sidewalks.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1010, -990, -1005)]]}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()
    crossing = {
        "type": "Feature",
        "properties": {"highway": "sidewalk"},
        "geometry": {"type": "LineString", "coordinates": [[-960, -1030], [-960, -970]]},  # přes silnici
    }
    (work / "osm_paths" / "paths_mixed.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [crossing]}), encoding="utf-8"
    )
    seen = {}
    real = ds.unify_sidewalks

    def spy(polys, lines, **kw):
        seen["lines"] = lines
        return real(polys, lines, **kw)

    monkeypatch.setattr(ds, "unify_sidewalks", spy)
    di.build_dtm_sidewalk_parts(
        work, preset_id="sprint_4000", scale=4000, ref_x=-960.0, ref_y=-1000.0, grivation_deg=0.0
    )
    assert len(seen["lines"]) == 2  # před a za silnicí; úsek v silnici (-1004…-996) odpadl
    assert all(not (-1004 < y < -996) for ln in seen["lines"] for _x, y in ln)


def test_dtm_buildings_replace_osm_twins_and_keep_osm_only_buildings(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "buildings.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1000, -990, -990)]]}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()

    def bld(x0, y0, x1, y1):
        ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
        return {
            "type": "Feature",
            "properties": {"source": "osm", "kind": "building", "oom_code": "521"},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }

    feats = [bld(-999.5, -999.5, -989.5, -989.5), bld(-960, -960, -950, -950)]  # dvojče posunuté o 0,5 m + nová
    (work / "osm_paths" / "features.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    merged = di.merge_dtm_buildings(work, feats)
    sources = sorted(f["properties"]["source"] for f in merged)
    assert sources == ["dtm", "osm"]  # OSM dvojče zahozeno, nová OSM budova zůstala
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-980.0, ref_y=-980.0, grivation_deg=0.0)
    parts = op.build_osm_feature_parts(work, **kw)
    assert [p.name for p in parts] == ["Budovy (DTM + OSM)"] and parts[0].count == 2


def test_touching_dtm_buildings_are_fused_into_one_object_and_shp_uses_dtm(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "buildings.json").write_text(
        json.dumps(
            {
                "polygons": [
                    [[list(p) for p in ring] for ring in _rect(-1000, -1000, -990, -990)],
                    [[list(p) for p in ring] for ring in _rect(-989.95, -1000, -980, -990)],  # škvíra 5 cm
                    [[list(p) for p in ring] for ring in _rect(-960, -960, -950, -950)],  # samostatná
                ]
            }
        ),
        encoding="utf-8",
    )
    merged = di.merge_dtm_buildings(work, [])
    assert sorted(f["properties"]["source"] for f in merged) == ["dtm", "dtm"]  # 2 objekty místo 3
    # budovy do SHP / filtru srázů: stejné skládání jako v .omap
    (work / "osm_paths").mkdir()
    (work / "osm_paths" / "buildings.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"kind": "building"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[-999.5, -999.5], [-990, -999.5], [-990, -990], [-999.5, -990], [-999.5, -999.5]]],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert len(di.dtm_mask_wkbs(work)) == 3  # maska zbytkových ploch obsahuje budovy z DTM


def test_small_paved_parts_are_separate_501_0_areas(tmp_path):
    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "small_paved.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(0, 0, 3, 2)]]}), encoding="utf-8"
    )
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    parts = di.build_dtm_small_paved_parts(work, **kw)
    assert [p.name for p in parts] == ["DTM – malé zpevněné stavby (501.0)"] and parts[0].count == 1
    assert di.build_dtm_small_paved_parts(work, **{**kw, "preset_id": "forest_10000"}) == []


def test_steps_keep_only_osm_steps_outside_dtm_stairs_and_sidewalks():
    stair_zone = ds.dtm_union([_rect(10, -1, 12, 1)]).buffer(ds.STAIRS_ZONE_M)
    cover = ds.dtm_union([_rect(0, -1, 40, 1)]).buffer(0.5)  # chodník z DTM
    # OSM čára schodů přes celý chodník (0–40) a dál za ním (40–60)
    pieces = ds.steps_keep([(0.0, 0.0), (60.0, 0.0)], stair_zone, cover)
    assert len(pieces) == 1
    assert min(x for x, _y in pieces[0]) > 40.0 and abs(max(x for x, _y in pieces[0]) - 60.0) < 0.1
    # bez ploch chodníků zůstane vše kromě schodiště z DTM
    pieces = ds.steps_keep([(0.0, 0.0), (60.0, 0.0)], stair_zone, None)
    assert len(pieces) == 2


def test_stairs_axis_follows_reference_path_and_centroid():
    from shapely.geometry import LineString

    stairs = _rect(10, -1, 12, 1)  # čtvercové schodiště 2 × 2 m
    path = LineString([(0.0, 0.0), (30.0, 0.0)])  # cesta ve směru x
    axis = ds.stairs_axis(stairs, [[path], []])
    (x0, y0), (x1, y1) = axis
    assert abs(y1 - y0) < 1e-6 and abs(y0) < 1e-6  # vodorovně, středem schodiště
    assert abs((x1 - x0) - (2.0 + 2 * ds.STAIRS_AXIS_EXTRA_M)) < 1e-6
    # bez cesty v okolí: po delší straně
    axis = ds.stairs_axis(_rect(0, 0, 6, 2))
    (x0, y0), (x1, y1) = axis
    assert abs(y1 - y0) < 1e-6 and abs(x1 - x0) > 6.0
    assert ds.stairs_axis(_rect(0, 0, 0.3, 0.3)) is None


def test_long_curved_stairs_get_centerline_along_the_whole_curve():
    import math

    from shapely.geometry import LineString, Polygon

    # zahnuté schodiště: oblouk 90° o poloměru 12 m (délka ≈ 18,8 m) široký 2,6 m
    arc = LineString([(12 * math.cos(a / 20 * math.pi / 2), 12 * math.sin(a / 20 * math.pi / 2)) for a in range(21)])
    poly = arc.buffer(1.3, cap_style=2)
    rings = [list(poly.exterior.coords)]
    axis = ds.stairs_axis(rings)
    assert axis is not None and len(axis) > 3  # více bodů, ne přímka o dvou
    length = LineString(axis).length
    assert 18.0 < length < 20.5  # po celém oblouku
    # konce leží poblíž konců oblouku (zahnutí se neztratilo)
    ends = sorted([axis[0], axis[-1]])
    assert min(math.dist(e, (12.0, 0.0)) for e in ends) < 1.6  # do půl šířky od konce
    assert min(math.dist(e, (0.0, 12.0)) for e in ends) < 1.6
    assert isinstance(Polygon(rings[0]).area, float)


def test_dtm_stairs_parts_draw_every_stair_and_osm_steps_only_outside(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "stairs.json").write_text(
        json.dumps(
            {
                "polygons": [
                    [[list(p) for p in ring] for ring in _rect(-1000, -1000, -996, -998)],  # bez OSM čáry
                    [[list(p) for p in ring] for ring in _rect(-970, -1000, -966, -998)],  # na OSM čáře
                ]
            }
        ),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()
    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"highway": "steps"},
                "geometry": {"type": "LineString", "coordinates": [[-980, -999], [-940, -999]]},
            }
        ],
    }
    (work / "osm_paths" / "paths_mixed.geojson").write_text(json.dumps(gj), encoding="utf-8")
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-980.0, ref_y=-999.0, grivation_deg=0.0)
    parts = di.build_dtm_stairs_parts(work, **kw)
    assert [p.name for p in parts] == ["DTM – schodiště (532.7)"] and parts[0].count == 2
    paths = op.build_osm_path_parts(work, **kw)
    assert paths and paths[0].count == 2  # OSM čára přerušená schodištěm z DTM: před ním a za ním


def test_dtm_cover_geometry_clips_zabaged_lines(tmp_path):
    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "roads.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(0, -4, 40, 4)]]}), encoding="utf-8"
    )
    cover = di.dtm_cover_geometry(work)
    assert cover is not None and cover.contains(__import__("shapely.geometry").geometry.Point(20, 0))
    pieces = ds.clip_outside([(-30.0, 0.0), (80.0, 0.0)], cover)
    assert len(pieces) == 2  # část uvnitř plochy se nekreslí


def test_is_surface_sidewalk_excludes_bridges_and_steps():
    assert di.is_surface_sidewalk("sidewalk")
    assert not di.is_surface_sidewalk("steps")
    assert not di.is_surface_sidewalk("path")


def test_sidewalks_parts_and_osm_paths_skip_sidewalks(tmp_path):
    from app.pipeline import osm_paths as op

    work = tmp_path
    (work / "dtm").mkdir()
    (work / "dtm" / "sidewalks.json").write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in _rect(-1000, -1000, -970, -998)]]}),
        encoding="utf-8",
    )
    (work / "osm_paths").mkdir()
    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"highway": "sidewalk"},
                "geometry": {"type": "LineString", "coordinates": [[-1000, -999], [-970, -999]]},
            },
            {
                "type": "Feature",
                "properties": {"highway": "path"},
                "geometry": {"type": "LineString", "coordinates": [[-1000, -980], [-970, -980]]},
            },
        ],
    }
    (work / "osm_paths" / "paths_mixed.geojson").write_text(json.dumps(gj), encoding="utf-8")
    kw = dict(preset_id="sprint_4000", scale=4000, ref_x=-985.0, ref_y=-999.0, grivation_deg=0.0)
    parts = di.build_dtm_sidewalk_parts(work, **kw)
    assert [p.name for p in parts] == ["Chodníky (DTM + OSM)"] and parts[0].count == 1
    paths = op.build_osm_path_parts(work, **kw)
    assert paths and paths[0].count == 1  # jen pěšina, chodník nahradila plocha
    assert di.build_dtm_sidewalk_parts(work, **{**kw, "preset_id": "forest_10000"}) == []
