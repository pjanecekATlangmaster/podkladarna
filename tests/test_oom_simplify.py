"""Zjednodušení tvaru jako Ctrl+M v Mapperu (oom_simplify)."""

import math
import re

import pytest

from app.pipeline.oom_import import OomObjectPart
from app.pipeline.oom_simplify import simplify_indices, simplify_object_parts, simplify_ring, _seg_dist2

LINE = [(i * 10, 0) for i in range(11)]


@pytest.mark.parametrize(
    "pts, closed, expected",
    [
        (LINE, False, [0, 10]),
        (LINE[:5] + [(50, 5)] + LINE[6:], False, None),  # zub 5 > tol zůstane
        ([(0, 0), (10, 0), (10, 10), (0, 10)], True, [0, 1, 2, 3]),
        ([(0, 0), (5, 0.1), (10, 0), (5, 0.2)], True, None),  # nikdy pod 3 vrcholy
    ],
)
def test_simplify_indices(pts, closed, expected):
    keep = simplify_indices(pts, closed=closed, tol=1.0)
    if expected is not None:
        assert keep == expected
    elif closed:
        assert len(keep) >= 3
    else:
        assert 5 in keep and keep[0] == 0 and keep[-1] == len(pts) - 1


def test_deviation_within_tolerance():
    pts = [(i * 3.0, math.sin(i / 4.0) * 0.8) for i in range(60)]
    tol = 0.5
    keep = simplify_indices(pts, closed=False, tol=tol)
    assert len(keep) < len(pts)
    for i, (x, y) in enumerate(pts):
        d = min(
            _seg_dist2(x, y, *pts[a], *pts[b]) for a, b in zip(keep, keep[1:])
        )
        assert d <= tol * tol + 1e-9


def test_simplify_ring_keeps_closing_duplicate():
    ring = [(0, 0), (5, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    out = simplify_ring(ring, 0.1, closed=True)
    assert out[0] == out[-1] and len(out) == 5


def _xml(*objs):
    return "".join(
        f'<object type="1" symbol="3"><coords count="{len(c.split(";")) - 1}">{c}</coords></object>' for c in objs
    )


def _run(*coords):
    part = OomObjectPart(name="t", objects_xml=_xml(*coords), count=len(coords))
    stats: dict = {}
    out = simplify_object_parts([part], stats=stats)[0].objects_xml
    return re.findall(r'<coords count="(\d+)">([^<]*)</coords>', out), stats


def test_xml_line_and_flags_and_curves():
    line = ";".join(f"{i * 1000} 0" for i in range(6)) + ";"
    dash = ";".join(f"{i * 1000} 5000" + (" 32" if i == 2 else "") for i in range(6)) + ";"
    curve = ";".join(f"{i * 1000} 9000" + (" 1" if i == 0 else "") for i in range(6)) + ";"
    res, stats = _run(line, dash, curve)
    assert res[0][1] == "0 0;5000 0;"
    assert "2000 5000 32" in res[1][1]
    assert res[2][1] == curve
    assert stats["before"] > stats["after"]


def test_xml_shared_vertex_and_t_junction():
    a = "0 0;1000 0;2000 0;3000 0;4000 0;"
    b = "2000 0;2000 5000;"  # sdílí vrchol (2000 0) i T-napojení
    c = "0 20000;1000 20000;2000 20000;3000 20000;4000 20000;"
    d = "3000 20005;3000 30000;"  # konec leží na úsečce c (do 10 µm)
    res, _ = _run(a, b, c, d)
    assert res[0][1] == "0 0;2000 0;4000 0;"
    assert "3000 20000" in res[2][1]


def test_xml_area_with_hole():
    outer = "0 0;5000 0;10000 0;10000 10000;0 10000;0 0 18;"
    hole = "3000 3000;6000 3000;6000 6000;3000 6000;3000 3000 18;"
    res, _ = _run(outer + hole)
    cnt, body = res[0]
    pts = [p for p in body.split(";") if p]
    assert int(cnt) == len(pts) == 10
    assert pts[4] == "0 0 18" and pts[-1] == "3000 3000 18"
