"""Zeleň ze zrnitosti porostu mimo Česko (veg_texture)."""

import numpy as np
import pytest

from app.pipeline import veg_texture as T


def _scene(kind: str):
    """CHM 200 × 200 m po 1 m a třídy porostu (vše bílé, uprostřed případně zeleň)."""
    chm = np.full((200, 200), 22.0, dtype=np.float32)
    cl = np.zeros((200, 200), dtype=np.uint8)
    yy, xx = np.indices((60, 60))
    rough = np.where((yy % 7 < 3) & (xx % 7 < 3), 2.0, 22.0).astype(np.float32)  # mezery 3 × 3 m po 7 m
    if kind in ("rough", "rough_green"):
        chm[70:130, 70:130] = rough
    if kind == "rough_green":
        cl[70:130, 70:130] = T.MID
    if kind == "field_rough":
        chm[:] = 0.3
        cl[:] = 1  # 401
        chm[90:110, 90:110] = np.where((yy[:20, :20] % 7 < 3) & (xx[:20, :20] % 7 < 3), 22.0, 0.3)  # solitéry
    return chm, cl


@pytest.mark.parametrize(
    "kind, expect",
    [
        ("smooth", T.WHITE),  # vzrostlý stejnorodý les zůstává bílý
        ("rough", T.LIGHT),  # mezerovitý porost v lese → 406
        ("rough_green", T.MID),  # zeleň z výšky porostu + zrnitost → 408
        ("field_rough", 1),  # solitéry v poli bez lesního okolí: 401 zůstává
    ],
)
def test_texture_green(kind, expect):
    chm, cl = _scene(kind)
    out, shares = T.add_texture_green(cl, chm, 1.0)
    centre = out[85:115, 85:115]
    assert np.mean(centre == expect) > 0.9
    assert set(shares) == {"406", "408", "410"}
