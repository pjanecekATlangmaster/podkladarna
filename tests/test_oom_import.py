from __future__ import annotations

from app.pipeline.oom_import import projected_to_map_coord
from app.pipeline.oom_symbol_map import oom_code_for_vectorconf_rule, symbol_index_for_code
from app.pipeline.oom_vectorconf import load_vectorconf, match_feature


def test_projected_to_map_coord_scale_4000():
    # 1 m východně od ref → 250 nativních jednotek (0,25 mm na papíře při 1:4000)
    x, y = projected_to_map_coord(1.0, 0.0, ref_x=0.0, ref_y=0.0, scale=4000)
    assert x == 250
    assert y == 0


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
