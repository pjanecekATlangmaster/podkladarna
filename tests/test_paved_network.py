"""Zpevněné plochy v lese a MTBO: šířky kresby ze sady, slučování ploch, silnice a cesty na ploše."""

from __future__ import annotations

import pytest

from app.pipeline import osm_paths as op
from app.pipeline.oom_symbol_map import symbol_line_width_mm


def test_symbol_line_width_from_set():
    # ISOM 1:15000: okraj 501.2 0,14 mm, silnice 503 0,35 mm, pěšina 506 0,18 mm.
    assert symbol_line_width_mm("les", 15000, "501.2") == pytest.approx(0.14)
    assert symbol_line_width_mm("les", 15000, "503") == pytest.approx(0.35)
    assert symbol_line_width_mm("les", 15000, "506") == pytest.approx(0.18)
    # 502 = výplň 0,3 mm + černé okraje → celkem 0,58 mm.
    assert symbol_line_width_mm("les", 15000, "502") == pytest.approx(0.58)
    # ISOM 1:10000 má symboly 1,5× větší.
    assert symbol_line_width_mm("les", 10000, "503") == pytest.approx(0.525, abs=0.01)
    # Plocha není čára.
    assert symbol_line_width_mm("les", 15000, "501.1") is None


def test_drawn_symbol_width_in_ground_metres():
    # 503 (0,35 mm) v 1:15000 = 5,25 m; v 1:10000 (sada 1,5×) totéž.
    assert op.drawn_symbol_width_m("road_2", "les", 15000) == pytest.approx(5.25)
    assert op.drawn_symbol_width_m("road_2", "les", 10000) == pytest.approx(5.25, abs=0.1)
    # MTBO pěšina 834 (0,4 mm) v 1:15000 = 6 m.
    assert op.drawn_symbol_width_m("footway", "mtbo", 15000) == pytest.approx(6.0)
    assert op.paved_edge_width_m("les", 15000) == pytest.approx(2.1)
    # MTBO sada 501.2 v katalogu nemá → ISOM 0,14 mm.
    assert op.paved_edge_width_m("mtbo", 15000) == pytest.approx(2.1)
