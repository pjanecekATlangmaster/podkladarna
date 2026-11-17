"""Sprint: samostatný strom na zpevněné ploše jako bod 417 (CHM + ortofoto, 3.21.0)."""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import box

from app.pipeline.sprint_trees import detect_paved_trees

SIZE = 80  # m, CHM 1 m, ortofoto 0,5 m
CHM_GT = (0.0, 1.0, 0.0, float(SIZE), 0.0, -1.0)
ORTHO_GT = (0.0, 0.5, 0.0, float(SIZE), 0.0, -0.5)


def _disc(arr, cx, cy, r, value, res):
    yy, xx = np.mgrid[0 : arr.shape[0], 0 : arr.shape[1]]
    d = np.hypot((xx + 0.5) * res - cx, SIZE - (yy + 0.5) * res - cy)
    arr[d <= r] = value


@pytest.mark.parametrize(
    ("scene", "want"),
    [
        ("paved", 1),  # strom na dlažbě → bod ve středu koruny
        ("grass", 0),  # strom v trávě – kreslí ho vegetace / nic
        ("grove", 0),  # skupina stromů (porost) – ne solitér
        ("yard", 1),  # strom ve dvoře: detekce ho najde, zahodí ho až budova / zalitý dvůr v hotové mapě
        ("roof", 0),  # vysoký nezelený objekt (střecha, věž)
        ("existing", 0),  # strom už je z OSM / AOPK
        ("corner", 1),  # budovy ze dvou stran (Tilleho nám.) – podíl budov v okolí nerozhoduje
        ("facade", 2),  # dva stromy spojené nezeleným přesahem patra u fasády – každý samostatně (Tilleho nám.)
    ],
)
def test_detect_paved_trees(scene, want):
    chm = np.zeros((SIZE, SIZE), dtype=np.float32)
    green = np.zeros((SIZE * 2, SIZE * 2), dtype=bool)
    buildings = None
    _disc(chm, 40, 40, 4, 9.0, 1.0)  # koruna ~50 m², 9 m
    if scene != "roof":
        _disc(green, 40, 40, 4, True, 0.5)
    if scene == "grass":
        green[:] = True
    if scene == "grove":
        for cx, cy in ((51, 40), (29, 40), (40, 51)):  # sousední koruny 3 m od okraje
            _disc(chm, cx, cy, 4, 8.0, 1.0)
            _disc(green, cx, cy, 4, True, 0.5)
    if scene == "facade":
        _disc(chm, 40, 62, 4, 9.0, 1.0)
        _disc(green, 40, 62, 4, True, 0.5)
        chm[SIZE - 62 : SIZE - 40, 44:47] = 8.8  # pás 3 m bez zeleně
    if scene == "yard":
        buildings = box(20, 20, 60, 33).union(box(20, 47, 60, 60)).union(box(20, 20, 33, 60)).union(box(47, 20, 60, 60))
    if scene == "corner":
        buildings = box(48, 0, SIZE, SIZE).union(box(0, 48, SIZE, SIZE))
    existing = [(41.0, 40.0)] if scene == "existing" else None
    pts = detect_paved_trees(chm, CHM_GT, green, ORTHO_GT, buildings=buildings, existing=existing)
    assert len(pts) == want
    if want:
        x, y = min(pts, key=lambda p: p[1])
        assert abs(x - 40) < 1.5 and abs(y - 40) < 1.5


@pytest.mark.parametrize(
    ("block", "want"),
    [
        (None, 2),  # bez budovy a olivy oba stromy zůstanou
        ("521", 1),  # strom na budově (i v zalitém dvoře) pryč
        ("520", 1),  # strom v olivě (zahrada) pryč
        ("520_big", 0),  # i v areálu (nad 5 000 m²) stromy v olivě pryč (Petr 2026-11-17)
        ("520_edge", 1),  # značka zasahuje do olivy (bod 0,38 mm od ní) – pryč
        ("520_far", 2),  # značka olivy nedosáhne (bod 0,6 mm od ní)
    ],
)
def test_drop_trees_in_buildings_and_olive(block, want):
    from app.pipeline.oom_import import OomObjectPart, _point_object
    from app.pipeline.oom_symbol_map import symbol_index_for_code
    from app.pipeline.sprint_trees import TREE_PART_NAME, drop_trees_in_buildings_and_olive

    preset = "sprint_2m"
    tree = symbol_index_for_code(preset, 4000, "417")
    parts = [
        OomObjectPart(
            name=TREE_PART_NAME,
            objects_xml="\n".join([_point_object(tree, 500, 500), _point_object(tree, 5000, 5000)]),
            count=2,
        )
    ]
    if block:
        sym = symbol_index_for_code(preset, 4000, block.split("_")[0])
        # µm mapy 1:4000: čtverec 4 m pod stromem v (500, 500), areál 400 m, pás x < 120 / x < −100 vedle stromu
        x0, x1, e = {"big": (0, 100000, 100000), "edge": (-1000, 120, 1000), "far": (-1000, -100, 1000)}.get(block.split("_")[-1], (0, 1000, 1000))
        area = f'<object type="1" symbol="{sym}"><coords count="5">{x0} 0;{x1} 0;{x1} {e};{x0} {e};{x0} 0;</coords></object>'
        parts.insert(0, OomObjectPart(name="plocha", objects_xml=area, count=1))
    out = drop_trees_in_buildings_and_olive(parts, preset_id=preset, scale=4000)
    trees = [p for p in out if p.name == TREE_PART_NAME]
    assert sum(p.count for p in trees) == want


# Bílá díra po koruně u ulice (street_trees): silnice y 0–6 (osa y = 3, residential, půlka vozovky 3 m), trávník 401 nad ní
# se zálivem 8 × 8 m (x 16–24, y 6–14) otevřeným k ulici = bílá díra. Vrchol koruny v CHM podle případu.
@pytest.mark.parametrize(
    ("case", "fill", "trees"),
    [
        ("crown", "401", 1),  # díra zalitá převládajícím sousedem (401) a bod 418 ve vrcholu
        ("crown_on_road", "401", 0),  # značka by zasáhla vozovku: bod ne, díra se zalije
        ("big_hole", None, 0),  # velká bílá plocha s porostem (park) zůstává
        ("house_lawn", "401", 1),  # díra 380 m² u domu: koruna + holá zem (trávník) → louka 401 a bod (Most 2026-11-17)
        ("forest", None, 0),  # les a MTBO beze změny
    ],
)
def test_street_crowns(tmp_path, monkeypatch, case, fill, trees):
    import json

    from app.pipeline import street_trees, tree_rows
    from app.pipeline.oom_coords import projected_to_map_coord
    from app.pipeline.oom_import import OomObjectPart, _area_object_with_holes
    from app.pipeline.oom_symbol_map import symbol_index_for_code

    preset = "forest_10000" if case == "forest" else "sprint_2m"
    scale = 10000 if case == "forest" else 4000
    kw = dict(preset_id=preset, scale=scale, ref_x=0.0, ref_y=0.0, grivation_deg=0.0)
    hx0, hx1, hy1 = {"big_hole": (2, 38, 28), "house_lawn": (10, 34, 26)}.get(case, (16, 24, 14))

    def ring(pts):
        return [tuple(int(round(v)) for v in projected_to_map_coord(x, y, ref_x=0.0, ref_y=0.0, scale=scale, grivation_deg=0.0)) for x, y in pts]

    paved = symbol_index_for_code(preset, scale, "501.1") or symbol_index_for_code(preset, scale, "501")
    open_ = symbol_index_for_code(preset, scale, "401")
    road = ring([(0, 0), (40, 0), (40, 6), (0, 6), (0, 0)])
    lawn = ring([(0, 6), (hx0, 6), (hx0, hy1), (hx1, hy1), (hx1, 6), (40, 6), (40, 30), (0, 30), (0, 6)])
    parts = [
        OomObjectPart(name="ulice", objects_xml=_area_object_with_holes(paved, [road]), count=1),
        OomObjectPart(name="porost", objects_xml=_area_object_with_holes(open_, [lawn]), count=1),
    ]
    if case == "house_lawn":  # dům v rohu díry: jeho hrany jsou část obvodu díry, plocha se do díry nepočítá
        house = ring([(24, 16), (34, 16), (34, 26), (24, 26), (24, 16)])
        b = symbol_index_for_code(preset, scale, "521")
        parts.append(OomObjectPart(name="budovy", objects_xml=_area_object_with_holes(b, [house]), count=1))
    (tmp_path / "osm_paths").mkdir()
    (tmp_path / "osm_paths" / "paths_osm.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"highway": "residential"}, "geometry": {"type": "LineString", "coordinates": [[-5, 3], [45, 3]]}}
        ]}),
        encoding="utf-8",
    )
    chm = np.zeros((40, 50), dtype=np.float32)  # 1 m, x −5…45, y 35…−5
    ty = 6.5 if case == "crown_on_road" else 10.0
    _disc(chm, 20 + 5, SIZE - 35 + ty, 2.5, 8.0, 1.0)  # _disc počítá y od SIZE
    if case == "big_hole":
        chm[35 - 28 : 35 - 6, 5 + 2 : 5 + 38] = np.maximum(chm[35 - 28 : 35 - 6, 5 + 2 : 5 + 38], 6.0)  # porost parku
    monkeypatch.setattr(tree_rows, "_load_chm", lambda _w: (chm, (-5.0, 1.0, 0.0, 35.0, 0.0, -1.0)))

    out = street_trees.fill_street_crowns(parts, tmp_path, **kw)
    fills = [p for p in out if p.name == street_trees.FILL_PART_NAME]
    points = [p for p in out if p.name == street_trees.TREE_PART_NAME]
    if fill is None:
        assert not fills
    else:
        assert fills and f'symbol="{symbol_index_for_code(preset, scale, fill)}"' in fills[0].objects_xml
    assert sum(p.count for p in points) == trees
