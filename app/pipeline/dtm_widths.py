"""Les (a MTBO s ``MTBO_USE_DTM``): skutečná šířka cest z ploch DTM pro volbu symbolu.

DTM kraje má plochy silnic (``roads.json``) a chodníků (``sidewalks.json``) v přesném tvaru (sprint je kreslí
plochou). V lese se z nich změří šířka podél OSM čáry (kolmé řezy) a podle ní se zvolí symbol:

- silnice široká ≥ ``ROAD_502_1_MIN_M`` m → 502.1, ≥ ``ROAD_502_MIN_M`` m → 502 (užší zůstává podle tagů OSM);
- zpevněná pěší cesta široká ≥ ``WALK_505_MIN_M`` m → 505 místo 506;
- chodník / pěší plocha z DTM širší, než se v měřítku vejde čárou (``WALK_AREA_MIN_MM`` mm mapy), se kreslí jako
  zpevněná plocha 501 (plochou jen pevný povrch – ``ChodnikPlocha`` z DTM je dlažba / asfalt).
"""

from __future__ import annotations

import math
import statistics
from pathlib import Path

ROAD_502_1_MIN_M = 7.0
ROAD_502_MIN_M = 5.0
WALK_505_MIN_M = 2.5
# Pěší plocha z DTM užší než tolik mm mapy se nekreslí plochou (zůstane čára 505 / 506).
WALK_AREA_MIN_MM = 0.5
# Šířka se měří kolmými řezy po tolika metrech; do výsledku jen řezy, jejichž bod leží v ploše.
SAMPLE_STEP_M = 5.0
MAX_HALF_M = 25.0
# Šířka platí, když v ploše DTM leží aspoň tolik řezů (zbytek čáry DTM nezná nebo vede mimo).
MIN_INSIDE_SHARE = 0.5
# Stejný druh cesty – symbol z DTM jen pro (ne mosty / tunely / schody):
WALK_BASES = frozenset({"sidewalk", "footway", "path", "pedestrian", "cycleway", "cycleway_paved"})


class WidthIndex:
    """Plochy DTM (shapely) s prostorovým indexem pro měření šířky čar."""

    def __init__(self, polys: list) -> None:
        from shapely.prepared import prep
        from shapely.strtree import STRtree

        self.polys = [p for p in polys if p is not None and not p.is_empty]
        self.prepared = [prep(p) for p in self.polys]
        self.tree = STRtree(self.polys) if self.polys else None

    def width_at(self, x: float, y: float, nx: float, ny: float, max_half: float = MAX_HALF_M) -> float | None:
        """Šířka plochy v bodě (x, y) ve směru normály (nx, ny); None, když bod neleží v ploše."""
        if self.tree is None:
            return None
        from shapely.geometry import LineString, Point

        p = Point(x, y)
        seg = LineString([(x - nx * max_half, y - ny * max_half), (x + nx * max_half, y + ny * max_half)])
        hits = [self.polys[i] for i in self.tree.query(p) if self.prepared[i].covers(p)]
        if not hits:
            return None
        from shapely.ops import unary_union

        inter = seg.intersection(unary_union(hits) if len(hits) > 1 else hits[0])
        parts = [inter] if inter.geom_type == "LineString" else [g for g in getattr(inter, "geoms", []) if g.geom_type == "LineString"]
        for g in parts:
            if g.distance(p) < 1e-6:
                return float(g.length)
        return None

    def line_width(self, coords: list, step_m: float = SAMPLE_STEP_M) -> float | None:
        """Medián šířky plochy podél čáry, nebo None (čára leží v ploše méně než z ``MIN_INSIDE_SHARE``)."""
        if self.tree is None or len(coords) < 2:
            return None
        from shapely.geometry import LineString

        line = LineString(coords)
        if line.length <= 0:
            return None
        n = max(1, int(line.length // step_m))
        widths: list[float] = []
        for k in range(n):
            d = (k + 0.5) * line.length / n
            a = line.interpolate(max(0.0, d - 0.5))
            b = line.interpolate(min(line.length, d + 0.5))
            tx, ty = b.x - a.x, b.y - a.y
            norm = math.hypot(tx, ty) or 1.0
            p = line.interpolate(d)
            w = self.width_at(p.x, p.y, -ty / norm, tx / norm)
            if w is not None:
                widths.append(w)
        if len(widths) < MIN_INSIDE_SHARE * n:
            return None
        return statistics.median(widths)


def width_line_code(highway: str, width_m: float | None, tag_code: str) -> str:
    """Les: kód symbolu podle skutečné šířky (DTM); bez šířky / mimo pravidla ``tag_code`` z OSM."""
    if width_m is None:
        return tag_code
    from app.pipeline.osm_paths import path_draw_highway

    base = path_draw_highway(highway)
    if base.startswith("road_"):
        if width_m >= ROAD_502_1_MIN_M:
            return "502.1"
        if width_m >= ROAD_502_MIN_M and tag_code in {"503", "504"}:
            return "502"
        return tag_code
    if base in WALK_BASES and tag_code == "506" and width_m >= WALK_505_MIN_M:
        return "505"
    return tag_code


def _load(work_dir: Path, name: str) -> list:
    from shapely.geometry import Polygon

    from app.pipeline.dtm_import import _load_polygons_file

    out = []
    for poly in _load_polygons_file(work_dir / "dtm" / name):
        try:
            g = Polygon(poly[0], poly[1:])
            out.append(g if g.is_valid else g.buffer(0))
        except (ValueError, TypeError):
            continue
    return out


def dtm_width_indexes(work_dir: Path) -> tuple[WidthIndex | None, WidthIndex | None]:
    """(silnice, chodníky) z DTM jobu, nebo None tam, kde data nejsou."""
    from app.pipeline.dtm_import import ROADS_NAME, SIDEWALKS_NAME

    try:
        roads = _load(work_dir, ROADS_NAME)
        walks = _load(work_dir, SIDEWALKS_NAME)
    except ImportError:
        return None, None
    return (WidthIndex(roads) if roads else None), (WidthIndex(walks) if walks else None)


def wide_walk_area_features(work_dir: Path, scale: int) -> list[dict]:
    """Chodníky / pěší plochy z DTM široké aspoň ``WALK_AREA_MIN_MM`` mm mapy jako prvky ``pedestrian_area``.

    Úzké části (běžný chodník podél ulice) se odříznou otevřením (buffer −t/2, +t/2); zůstanou náměstí,
    promenády a široké pěší cesty, které se pak v lese kreslí jako zpevněná plocha a napojí do sítě.
    """
    try:
        from shapely.geometry import mapping
        from shapely.ops import unary_union
    except ImportError:
        return []
    polys = _load(work_dir, "sidewalks.json")
    if not polys:
        return []
    t = WALK_AREA_MIN_MM * scale / 1000.0
    wide = unary_union(polys).buffer(-t / 2, join_style=2).buffer(t / 2, join_style=2)
    if wide.is_empty:
        return []
    geoms = [wide] if wide.geom_type == "Polygon" else [g for g in getattr(wide, "geoms", []) if g.geom_type == "Polygon"]
    return [
        {
            "type": "Feature",
            "properties": {"source": "dtm", "kind": "pedestrian_area", "oom_code": "501"},
            "geometry": mapping(g),
        }
        for g in geoms
        if g.area >= t * t
    ]
