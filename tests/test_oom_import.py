from __future__ import annotations

from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import feature_props
from app.pipeline.oom_symbol_map import oom_code_for_vectorconf_rule, symbol_index_for_code
from app.pipeline.oom_vectorconf import load_vectorconf, match_feature


def test_feature_props_uses_getfield_not_items():
    class _Defn:
        def __init__(self, name: str) -> None:
            self._name = name

        def GetName(self) -> str:
            return self._name

    class _Feature:
        def __init__(self, fields: list[tuple[str, object]]) -> None:
            self._fields = fields

        def GetFieldCount(self) -> int:
            return len(self._fields)

        def GetFieldDefnRef(self, i: int) -> _Defn:
            return _Defn(self._fields[i][0])

        def GetField(self, i: int) -> object:
            return self._fields[i][1]

    feat = _Feature([("druhbud", "ano"), ("typulice_p", "ulice sjízdná v sídle")])
    props = feature_props(feat, layer_name="Cesta")
    assert props["druhbud"] == "ano"
    assert props["vrstva"] == "Cesta"


def test_projected_to_map_coord_at_ref():
    x, y = projected_to_map_coord(
        100.0,
        200.0,
        ref_x=100.0,
        ref_y=200.0,
        scale=4000,
        grivation_deg=13.0,
    )
    assert x == 0
    assert y == 0


def test_projected_to_map_coord_east_with_grivation():
    # 1 m východně, grivation 0 → 250 nativních jednotek na ose X
    x0, y0 = projected_to_map_coord(
        1.0, 0.0, ref_x=0.0, ref_y=0.0, scale=4000, grivation_deg=0.0
    )
    assert x0 == 250
    assert y0 == 0
    # Se grivací se osa mapy natočí – Y už není 0
    x1, y1 = projected_to_map_coord(
        1.0, 0.0, ref_x=0.0, ref_y=0.0, scale=4000, grivation_deg=13.0
    )
    assert x1 != 250 or y1 != 0
    assert abs(x1) > 200


def test_vectorconf_match_building():
    rules = load_vectorconf("zabaged.txt")
    rule = match_feature({"druhbud": "ano"}, rules)
    assert rule is not None
    assert rule.symbol_name == "building"
    assert rule.kp_code == "526"


def test_oom_code_building_maps_to_521():
    code = oom_code_for_vectorconf_rule(
        "building",
        "526",
        "Budova",
        preset_id="sprint_2m",
        scale=4000,
    )
    assert code == "521"
    assert symbol_index_for_code("sprint_2m", 4000, code) == 141


def test_oom_code_railway_maps_to_509_1():
    code = oom_code_for_vectorconf_rule(
        "railway",
        "515",
        "ZeleznicniTrat",
        preset_id="sprint_2m",
        scale=4000,
    )
    assert code == "509.1"
    assert symbol_index_for_code("sprint_2m", 4000, code) is not None


def test_oom_code_railway_forest_maps_to_509():
    code = oom_code_for_vectorconf_rule(
        "railway",
        "515",
        "ZeleznicniTrat",
        preset_id="forest_10000",
        scale=10000,
    )
    assert code == "509"
    assert symbol_index_for_code("forest_10000", 10000, code) is not None


def test_oom_code_road_forest_maps_to_503():
    code = oom_code_for_vectorconf_rule(
        "road-path",
        "503",
        "SilniceDalnice",
        preset_id="forest_10000",
        scale=10000,
    )
    assert code == "503"
    assert symbol_index_for_code("forest_10000", 10000, code) is not None


def test_oom_code_skupina_balvanu_maps_to_207():
    code = oom_code_for_vectorconf_rule(
        "blackline",
        "414",
        "SkupinaBalvanu",
        preset_id="sprint_2m",
        scale=4000,
    )
    assert code == "207"
    assert symbol_index_for_code("sprint_2m", 4000, code) is not None


def test_oom_code_parking_forest_maps_to_501_1():
    code = oom_code_for_vectorconf_rule(
        "parking",
        "529",
        "ParkovisteOdpocivka",
        preset_id="forest_10000",
        scale=10000,
    )
    assert code == "501.1"
    assert symbol_index_for_code("forest_10000", 10000, code) is not None


def test_oom_code_dxf_cliffs_small_preset_specific():
    from app.pipeline.oom_symbol_map import oom_code_for_dxf

    assert oom_code_for_dxf("cliffs_small.dxf", preset_id="sprint_2m") == "104"
    assert oom_code_for_dxf("cliffs_small.dxf", preset_id="forest_10000") == "104"
    assert oom_code_for_dxf("cliffs_large.dxf", preset_id="forest_10000") == "104"


def test_orient_polyline_tags_downhill_flips_when_needed():
    from app.pipeline.oom_import import orient_polyline_tags_downhill

    pts = [(0.0, 0.0), (10.0, 0.0)]

    def to_map(x, y):
        return (int(x * 100), int(y * 100))

    def elev_lower_on_plus_y(x, y):
        # Vlevo od 0→10 (+Y) je níž → bez otočení (tagy vlevo).
        return -y

    assert (
        orient_polyline_tags_downhill(pts, elev_at=elev_lower_on_plus_y, to_map=to_map)
        == pts
    )

    def elev_lower_on_minus_y(x, y):
        return y

    assert orient_polyline_tags_downhill(
        pts, elev_at=elev_lower_on_minus_y, to_map=to_map
    ) == list(reversed(pts))
