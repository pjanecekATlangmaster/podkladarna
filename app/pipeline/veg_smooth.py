"""Tvar hranic porostů (401–410) z rastru tříd bez schodků a bez posunu hranice.

Rastrové polygony mají schodky po pixelech (1 m). Les a MTBO (od 2.47.0)
schodky vyhlazují tak, aby hranice zůstala na místě:

1. obrys se vede středy hran pixelů (schodek 45° je rovná čára),
2. body se vyhladí Gaussem podél obvodu (``sigma``), posun každého bodu
   je ale omezený na ``max_shift`` (hranice se neodchýlí o víc než ~1,5 m),
3. Douglas–Peucker s malou tolerancí, plocha se dorovná na původní
   (posun obrysu o rozdíl ploch / obvod, i se zaoblením křivek v OOM).

Stupně 2 a 3 před tím navíc zalijí zátoky a ustřihnou výběžky užší než
2 × poloměr (zjednodušení drobností jako dřív). Srovnání s ručními mapami
(Janov Okrouhlice, Vyšší Brod E2, 2026-10-29): proti ruční mapě jsou stupně
stejně přesné (rozhoduje klasifikace); dřívější výchozí stupeň 2 (DP 3 m +
křivky) ale mazal drobnosti do ~12 m a zvětšil 410 o 3–5 %, stupeň 1 nic
nemaže, plochu drží (±0,6 %) a má nejméně ostrých zlomů.

Kde by tvar přibyl, plocha se odřízne v překážkách (cesta s bufferem,
budova, vodní plocha) a odpadnou ostrůvky, které s původní plochou
nesousedí. Ubývat plocha smí volně (tam se louka jen zkrátí).
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


# Stupeň → (poloměr zjednodušení drobností m, sigma Gausse m, max. posun bodu m,
# tolerance DP m). 0 = kostičky 1:1 z rastru (bez úprav).
SMOOTH_LEVELS: dict[int, tuple[float, float, float, float]] = {
    1: (0.0, 2.0, 1.5, 0.6),
    2: (3.0, 2.5, 2.0, 0.6),
    3: (6.0, 3.5, 3.0, 0.8),
}
# Krok převzorkování obrysu před Gaussem (m).
_RESAMPLE_M = 0.5
# Dorovnání plochy nejvýš o tolik metrů kolmo na obrys.
_AREA_FIX_MAX_M = 0.5
# Díl menší než tohle (m²) po vyhlazení zahodit.
_MIN_PART_M2 = 1.0


# Délka úseku obrysu před převodem na středy hran (m) = pixel rastru tříd.
_MIDPOINT_SEGMENT_M = 1.0


def _midpoint_ring(coords) -> "np.ndarray":
    """Středy hran po ≤ 1 m (dlouhou rovnou hranu z polygonize nejdřív rozdělit,
    jinak by středy hran ořízly rohy)."""
    import numpy as np
    from shapely import get_coordinates, segmentize
    from shapely.geometry import LinearRing

    c = get_coordinates(segmentize(LinearRing(coords), _MIDPOINT_SEGMENT_M))
    if len(c) > 1 and np.allclose(c[0], c[-1]):
        c = c[:-1]
    return (c + np.roll(c, -1, axis=0)) / 2.0


def _gauss_ring(c, sigma: float, max_shift: float):
    """Uzavřený obrys: převzorkovat po ``_RESAMPLE_M``, Gauss podél obvodu, posun ≤ ``max_shift``."""
    import numpy as np
    from scipy.ndimage import gaussian_filter1d

    seg = np.linalg.norm(np.roll(c, -1, axis=0) - c, axis=1)
    length = float(seg.sum())
    if length < 4 * _RESAMPLE_M or sigma <= 0:
        return c
    s = np.concatenate([[0.0], np.cumsum(seg)])
    closed = np.vstack([c, c[:1]])
    n = max(8, int(length / _RESAMPLE_M))
    t = np.linspace(0.0, length, n, endpoint=False)
    x = np.interp(t, s, closed[:, 0])
    y = np.interp(t, s, closed[:, 1])
    sp = sigma / (length / n)
    dx = gaussian_filter1d(x, sp, mode="wrap") - x
    dy = gaussian_filter1d(y, sp, mode="wrap") - y
    d = np.hypot(dx, dy)
    k = np.where(d > max_shift, max_shift / np.maximum(d, 1e-9), 1.0)
    return np.column_stack([x + dx * k, y + dy * k])


def _curve_area_gain(poly) -> float:
    """O kolik m² se plocha zvětší převodem obrysů na křivky (Mapper „Převést na křivky“)."""
    from shapely.geometry import Polygon

    from app.pipeline.oom_import import convert_polyline_to_curves

    def curved(coords) -> list[tuple[float, float]]:
        k = 1000.0
        pts = convert_polyline_to_curves(
            [(round(x * k), round(y * k)) for x, y in coords], closed=True
        )
        out: list[tuple[float, float]] = []
        i = 0
        while i < len(pts) - 1:
            flags = pts[i][2] if len(pts[i]) > 2 else 0
            if flags & 1 and i + 3 < len(pts):
                (x0, y0), (x1, y1), (x2, y2), (x3, y3) = (
                    (p[0] / k, p[1] / k) for p in pts[i : i + 4]
                )
                for t in (0.0, 0.25, 0.5, 0.75):
                    u = 1.0 - t
                    out.append(
                        (
                            u**3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3,
                            u**3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3,
                        )
                    )
                i += 3
            else:
                out.append((pts[i][0] / k, pts[i][1] / k))
                i += 1
        return out

    try:
        rings = [list(poly.exterior.coords)] + [list(r.coords) for r in poly.interiors]
        shaped = Polygon(curved(rings[0]), [curved(r) for r in rings[1:] if len(r) >= 4])
        return float(shaped.area - poly.area)
    except Exception:
        return 0.0


def smooth_area(
    ring: Ring,
    holes: list[Ring],
    level: int,
    barriers: SmoothBarriers | None = None,
    *,
    as_curves: bool = True,
) -> AreaParts:
    """Vyhladí schodky obrysu bez posunu hranice (viz modul) a odřízne přírůstky v překážkách.

    ``as_curves``: obrys se v OOM převede na křivky – plocha se dorovná i o
    jejich zaoblení. Výsledkem může být víc dílů. Selhání → původní tvar.
    """
    from shapely import make_valid
    from shapely.geometry import Polygon

    from app.pipeline.area_split import polygons_of

    if not level:
        return [(ring, holes)]
    radius, sigma, max_shift, tol = SMOOTH_LEVELS.get(level, SMOOTH_LEVELS[1])
    original = [(ring, holes)]

    def shaped(coords) -> Ring:
        r = _gauss_ring(_midpoint_ring(coords), sigma, max_shift)
        pts = [(float(x), float(y)) for x, y in r]
        return pts + [pts[0]]

    try:
        poly = Polygon(ring, holes)
        if not poly.is_valid:
            poly = make_valid(poly)
        base = poly
        if radius > 0:
            # buffer(+r)→buffer(-r) = uzavření, buffer(-r)→buffer(+r) = otevření.
            closed = poly.buffer(radius, quad_segs=4).buffer(-radius, quad_segs=4)
            base = closed.buffer(-radius, quad_segs=4).buffer(radius, quad_segs=4)
            if not polygons_of(base):
                # Celá plocha je užší než 2 r – nemazat (pod ní je díra v sousední
                # ploše, zůstala by bílá), jen vyhladit jako stupeň 1.
                base = poly
                sigma, max_shift, tol = SMOOTH_LEVELS[1][1:]
        pieces = []
        for part in polygons_of(base):
            target = part.area
            g = Polygon(
                shaped(part.exterior.coords),
                [shaped(i.coords) for i in part.interiors if len(i.coords) >= 4],
            )
            if not g.is_valid:
                g = make_valid(g)
            g = g.simplify(tol, preserve_topology=True)
            for p in polygons_of(g):
                if p.area < _MIN_PART_M2:
                    continue
                share = target * p.area / max(g.area, 1e-9)
                gain = _curve_area_gain(p) if as_curves else 0.0
                d = (share - p.area - gain) / max(p.length, 1e-9)
                d = max(-_AREA_FIX_MAX_M, min(_AREA_FIX_MAX_M, d))
                if abs(d) > 1e-3:
                    p = p.buffer(d, quad_segs=2, join_style="mitre", mitre_limit=2.0)
                    p = p.simplify(tol / 4, preserve_topology=True)
                pieces.extend(q for q in polygons_of(p) if q.area >= _MIN_PART_M2)
        if not pieces:
            return original
        from shapely import union_all

        simp = union_all(pieces)
        if barriers is not None:
            near = barriers.near(simp)
            if near is not None:
                bad = simp.difference(poly).intersection(near)
                if not bad.is_empty:
                    simp = simp.difference(bad)
    except Exception:
        return original
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


# Sprint (beze změny od 2.46): stupeň → (tolerance Douglas–Peucker v m, poloměr morfologického vyhlazení v m).
# Poloměr > 0: uzavření (zalije zátoky a díry užší než 2 r) a otevření (ustřihne
# výběžky užší než 2 r) – zmizí obrysy korun jednotlivých stromů.
LEGACY_SMOOTH_LEVELS: dict[int, tuple[float, float]] = {
    1: (2.0, 0.0),
    2: (3.0, 3.0),
    3: (5.0, 6.0),
}


def smooth_area_legacy(
    ring: Ring,
    holes: list[Ring],
    level: int,
    barriers: SmoothBarriers | None = None,
) -> AreaParts:
    """Původní zaoblení (do 2.46): DP + křivky. Zůstává pro sprint.

    Výsledkem může být víc dílů (otevření rozdělí úzký přesmyk). Selhání /
    neplatný výsledek → původní tvar beze změny.
    """
    from shapely.geometry import Polygon

    tol_m, radius = LEGACY_SMOOTH_LEVELS.get(level, LEGACY_SMOOTH_LEVELS[1])
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
