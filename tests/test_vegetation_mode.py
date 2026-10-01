"""Testy režimu vegetace mixed vs kp."""

from __future__ import annotations

from app.pipeline.package_oom import (
    VEGETATION_MODE_KP,
    VEGETATION_MODE_MIXED,
    resolve_vegetation_mode,
)


def test_resolve_vegetation_mode():
    assert resolve_vegetation_mode("kp") == VEGETATION_MODE_KP
    assert resolve_vegetation_mode("mixed") == VEGETATION_MODE_MIXED
    assert resolve_vegetation_mode("") == VEGETATION_MODE_MIXED
    assert resolve_vegetation_mode("nope") == VEGETATION_MODE_MIXED
