"""Zaoblení plošné vegetace (louky, hustý porost) bez přelití přes překážky.

Rastrové polygony mají schodky po pixelech. Plochu zjednodušíme (Douglas–Peucker)
a pak ji v OOM převedeme na křivky. Zjednodušením se ale hranice posune až o
toleranci – mohla by tak louka přelézt na druhou stranu cesty, přes budovu nebo
do vody, kde v realitě není. Proto se **nově přibylá** plocha (zjednodušený tvar
mínus původní) odřízne všude, kde leží v překážce (cesta s bufferem, budova,
vodní plocha), a odpadnou i ostrůvky, které s původní plochou nesousedí.
Ubývat plocha smí volně (tam se louka jen zkrátí).
"""

from __future__ import annotations

import json
from pathlib import Path

Ring = list[tuple[float, float]]
AreaParts = list[tuple[Ring, list[Ring]]]

# Polovina šířky cesty v OSM (m) – tolik se kolem středové čáry bere jako cesta.
PATH_HALF_WIDTH_M = 1.0
_BARRIER_FEATURE_KINDS = frozenset({"building", "water_well_building", "water_body"})


class SmoothBarriers:
    """Překážky (cesty, budovy, voda) s prostorovým indexem."""

    def __init__(self, geoms: list) -> None:
        from shapely import STRtree

        self._geoms = [g for g in geoms if g is not None and not g.is_empty]
        self._tree = STRtree(self._geoms) if self._geoms else None

    def __len__(self) -> int:
        return len(self._geoms)

    def near(self, geom):
        """Sjednocení překážek protínajících ``geom`` (nebo ``None``)."""
        if self._tree is None:
            return None
        idx = self._tree.query(geom)
        if not len(idx):
            return None
        from shapely import union_all

        return union_all([self._geoms[i] for i in idx])


def load_smooth_barriers(work_dir: Path) -> SmoothBarriers:
    """Překážky z OSM podkladu jobu (``osm_paths/``); chybějící soubory se přeskočí."""
    from shapely.geometry import shape

    geoms: list = []
    osm_dir = Path(work_dir) / "osm_paths"

    def _features(name: str) -> list[dict]:
        path = osm_dir / name
        if not path.is_file():
            return []
        try:
            return list(json.loads(path.read_text(encoding="utf-8")).get("features") or [])
        except (OSError, ValueError):
            return []

    for feat in _features("paths_mixed.geojson"):
        try:
            geoms.append(shape(feat["geometry"]).buffer(PATH_HALF_WIDTH_M))
        except Exception:
            continue
    for feat in _features("features.geojson"):
        kind = str((feat.get("properties") or {}).get("kind") or "")
        if kind not in _BARRIER_FEATURE_KINDS:
            continue
        try:
            g = shape(feat["geometry"])
            geoms.append(g if g.is_valid else g.buffer(0))
        except Exception:
            continue
    return SmoothBarriers(geoms)


# Stupeň → (tolerance Douglas–Peucker v m, poloměr morfologického vyhlazení v m).
# Poloměr > 0: uzavření (zalije zátoky a díry užší než 2 r) a otevření (ustřihne
# výběžky užší než 2 r) – zmizí obrysy korun jednotlivých stromů.
SMOOTH_LEVELS: dict[int, tuple[float, float]] = {
    1: (2.0, 0.0),
    2: (3.0, 3.0),
    3: (5.0, 6.0),
}


def smooth_area(
    ring: Ring,
    holes: list[Ring],
    level: int,
    barriers: SmoothBarriers | None = None,
) -> AreaParts:
    """Zaoblí plochu o daný stupeň a odřízne přírůstky v překážkách.

    Výsledkem může být víc dílů (otevření rozdělí úzký přesmyk). Selhání /
    neplatný výsledek → původní tvar beze změny.
    """
    from shapely.geometry import Polygon

    tol_m, radius = SMOOTH_LEVELS.get(level, SMOOTH_LEVELS[1])
    original = [(ring, holes)]
    try:
        poly = Polygon(ring, holes)
        if not poly.is_valid:
            return original
        base = poly
        if radius > 0:
            # buffer(+r)→buffer(-r) = uzavření, buffer(-r)→buffer(+r) = otevření.
            closed = poly.buffer(radius, quad_segs=4).buffer(-radius, quad_segs=4)
            base = closed.buffer(-radius, quad_segs=4).buffer(radius, quad_segs=4)
        simp = base.simplify(tol_m, preserve_topology=True)
        if simp.is_empty or not simp.is_valid:
            return original
        if barriers is not None:
            near = barriers.near(simp)
            if near is not None:
                bad = simp.difference(poly).intersection(near)
                if not bad.is_empty:
                    simp = simp.difference(bad)
    except Exception:
        return original
    from app.pipeline.area_split import polygons_of

    out: AreaParts = []
    for p in polygons_of(simp):
        # Ostrůvek za překážkou, který s původní plochou nesousedí, zahodit.
        if not p.intersects(poly):
            continue
        if len(p.exterior.coords) < 4:
            continue
        out.append(
            (
                list(p.exterior.coords),
                [list(i.coords) for i in p.interiors if len(i.coords) >= 4],
            )
        )
    return out or original
