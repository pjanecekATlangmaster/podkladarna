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
    if kind in ("under", "tall_rough"):
        # under: vzrostlý les s mlázím 2–6 m v pruzích (podíl ~0,4); tall_rough: drsná smrčina 14–26 m bez nízkého patra
        lo, hi = (4.0, 22.0) if kind == "under" else (14.0, 26.0)
        chm[70:130, 70:130] = np.where((yy % 5 < 2), lo, hi).astype(np.float32)
    if kind == "low_pines":
        # nízké borovice: koruny 7 m v pruzích mezi korunami 12 m (Mira) – v pásmu 1–8 m, ale nejsou podrost
        chm[:] = np.where((np.indices((200, 200))[0] % 5 < 2), 7.0, 12.0)
    if kind == "rough_all":
        # celý výřez drsný (koruny 14–26 m bez nízkého patra): zrnitost by zazelenala celý les → pojistka understory
        chm[:] = np.where((np.indices((200, 200))[0] % 5 < 2), 14.0, 26.0)
    if kind == "field_rough":
        chm[:] = 0.3
        cl[:] = 1  # 401
        chm[90:110, 90:110] = np.where((yy[:20, :20] % 7 < 3) & (xx[:20, :20] % 7 < 3), 22.0, 0.3)  # solitéry
    if kind == "field_bushes":
        # garigue: louka s drobnými keři 3 × 3 m po 12 m (pod MIN_AREA_M2) – čištění je zahodí, nesmí zůstat bílé
        chm[:] = 0.3
        cl[:] = 1
        bush = (np.indices((200, 200))[0] % 12 < 3) & (np.indices((200, 200))[1] % 12 < 3)
        chm[bush] = 2.5
        cl[bush] = T.LIGHT
    return chm, cl


@pytest.mark.parametrize(
    "kind, expect, mode, ratio",
    [
        ("smooth", T.WHITE, "laser", None),  # vzrostlý stejnorodý les zůstává bílý
        ("rough", T.LIGHT, "laser", None),  # mezerovitý porost v lese → 406
        ("rough_green", T.MID, "laser", None),  # zeleň z výšky porostu + zrnitost → 408
        ("field_rough", 1, "laser", None),  # solitéry v poli bez lesního okolí: 401 zůstává
        ("rough_all", T.WHITE, "laser", None),  # zrnitost přes LASER_FOREST_MAX lesa: pojistka, drsná koruna zůstává bílá
        ("under", T.LIGHT, "understory", None),  # Trentino: les s nízkým patrem → 406
        ("tall_rough", T.WHITE, "understory", None),  # Trentino: drsná vysoká smrčina bez nízkého patra zůstává bílá
        ("smooth", T.WHITE, "understory", None),
        ("field_rough", 1, "understory", None),
        ("field_bushes", 1, "laser", None),  # drobné keře v louce: 401, ne bílé tečky lesa
        ("field_bushes", 1, "understory", None),
        ("under", T.LIGHT, "understory", 0.45),  # PT: mlází 4 m pod korunami 22 m zůstává podrostem
        ("low_pines", T.LIGHT, "understory", None),  # bez stropu by koruny 7 m byly „nízké patro“
        ("low_pines", T.WHITE, "understory", 0.45),  # PT: strop 0,45 × okolní koruny → bílý les
    ],
)
def test_texture_green(kind, expect, mode, ratio):
    chm, cl = _scene(kind)
    out, shares = T.add_texture_green(cl, chm, 1.0, mode=mode, low_ratio=ratio)
    centre = out[85:115, 85:115]
    assert np.mean(centre == expect) > 0.9
    if kind == "field_bushes":
        assert not np.any(out == T.WHITE)  # zahozený keř dřív zůstal bílou tečkou lesa
    assert set(shares) == {"406", "408", "410"}
