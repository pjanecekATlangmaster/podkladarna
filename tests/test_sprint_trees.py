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
        ("yard", 0),  # strom ve dvoře mezi budovami – okolí jsou střechy
        ("roof", 0),  # vysoký nezelený objekt (střecha, věž)
        ("existing", 0),  # strom už je z OSM / AOPK
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
    existing = [(41.0, 40.0)] if scene == "existing" else None
    pts = detect_paved_trees(chm, CHM_GT, green, ORTHO_GT, buildings=buildings, existing=existing)
    assert len(pts) == want
    if want:
        x, y = min(pts, key=lambda p: p[1])
        assert abs(x - 40) < 1.5 and abs(y - 40) < 1.5
