"""Kontrola kvality proti Liveloxu (scripts/livelox_quality): třídy barev a prahy reportu."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "livelox_quality"))
import measure  # noqa: E402
import report  # noqa: E402


@pytest.mark.parametrize("rgb,cls", [
    ((255, 255, 255), "white"),
    ((255, 186, 54), "open"),
    ((255, 221, 154), "rough_open"),
    ((197, 255, 185), "green1"),
    ((139, 255, 116), "green2"),
    ((61, 255, 23), "green3"),
    ((0, 160, 230), "water"),
    ((190, 105, 40), "brown"),
    ((0, 0, 0), "black"),
    ((136, 136, 136), "grey"),
    ((206, 50, 175), "course"),
])
def test_classify_isom_colours(rgb, cls):
    px = np.array([[rgb]], dtype=np.uint8)
    assert measure.CLASSES[measure.classify(px)[0, 0]] == cls


def _m(**kw):
    base = dict(white=0.5, green=0.2, open=0.1, water=0.02, grey=0.2, paved=0.2, olive=0.05, contour_km_per_km2=40)
    return {**base, **kw}


@pytest.mark.parametrize("ours,ref,flag", [
    (_m(), _m(), None),
    (_m(green=0.02, white=0.68), _m(), "zeleň 406–410"),           # zeleň místo … chybí
    (_m(contour_km_per_km2=80), _m(), "vrstevnice"),               # „norské“ zahuštění
    (_m(open=0.12), _m(open=0.1), None),                           # drobný rozdíl
])
def test_report_thresholds_les(ours, ref, flag):
    issues = report.check("les", {"ours": ours, "ref": ref, "f1_green": 0.5})
    assert (flag is None and not issues) or any(i.startswith(flag) for i in issues)
