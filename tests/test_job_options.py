from __future__ import annotations

from app.pipeline.job_options import (
    KP_CLIFF_SENSITIVITY,
    KP_VEGE_HEIGHT_DEFAULT,
    resolve_cliff_sensitivity,
    resolve_vege_height,
)


def test_resolve_vege_height_choices_and_fallback():
    assert resolve_vege_height({"kp_vege_height": 3.0}) == 3.0
    assert resolve_vege_height({"kp_vege_height": 2}) == 2.0
    assert resolve_vege_height({"kp_vege_height": 9.9}) == KP_VEGE_HEIGHT_DEFAULT
    assert resolve_vege_height({}) == KP_VEGE_HEIGHT_DEFAULT
    assert resolve_vege_height(None) == KP_VEGE_HEIGHT_DEFAULT


def test_resolve_cliff_sensitivity():
    assert resolve_cliff_sensitivity({"kp_cliff_sensitivity": "high"}) == "high"
    assert resolve_cliff_sensitivity({"kp_cliff_sensitivity": "nope"}) == "low"
    assert resolve_cliff_sensitivity({}) == "low"
    assert KP_CLIFF_SENSITIVITY["high"] == (1.4, 2.8)


def test_resolve_clean_open():
    from app.pipeline.job_options import resolve_clean_open

    assert resolve_clean_open({"clean_open": "none"}) == "none"
    assert resolve_clean_open({"clean_open": "x"}) == "big"
    assert resolve_clean_open(None) == "big"  # API a starší joby beze změny
