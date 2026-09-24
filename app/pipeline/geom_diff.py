"""Odčítání polygonů (OGR Difference) – KP open land minus ZABAGED/OSM masky.

KP žlutá (401) pokrývá celý otevřený terén jedním nebo několika obřími
polygony. Stejné místo často kreslí i ZABAGED louka nebo OSM orná (412).
Bez odečtu vzniknou dva překryté objekty; s odečtem KP 401 vyplní jen mezery.

Když chybí ``osgeo``, použije se Shapely (stejné WKB).
"""

from __future__ import annotations

_MIN_AREA_M2 = 12.0


def _have_ogr() -> bool:
    try:
        from osgeo import ogr  # noqa: F401

        return True
    except ImportError:
        return False


def union_polygon_wkbs(wkbs: list[bytes]):
    """Spojí polygony do jedné geometrie (OGR nebo Shapely), nebo None."""
    if not wkbs:
        return None
    if _have_ogr():
        return _union_ogr(wkbs)
    return _union_shapely(wkbs)


def difference_polygon_wkb(subject_wkb: bytes, mask) -> list[bytes]:
    """Vrátí WKB polygonů (subject − mask). Bez masky / při chybě původní subject."""
    if not subject_wkb:
        return []
    if _have_ogr():
        return _difference_ogr(subject_wkb, mask)
    return _difference_shapely(subject_wkb, mask)


def rings_to_polygon_wkb(rings: list[list[tuple[float, float]]]) -> bytes | None:
    """První prstenec = obrys, další = díry. Souřadnice v metrech (5514)."""
    if not rings or len(rings[0]) < 3:
        return None
    if _have_ogr():
        return _rings_to_wkb_ogr(rings)
    return _rings_to_wkb_shapely(rings)


def _union_ogr(wkbs: list[bytes]):
    from osgeo import ogr

    ogr.UseExceptions()
    acc = None
    for raw in wkbs:
        if not raw:
            continue
        geom = ogr.CreateGeometryFromWkb(raw)
        if geom is None or geom.IsEmpty():
            continue
        try:
            if hasattr(geom, "MakeValid"):
                fixed = geom.MakeValid()
                if fixed is not None and not fixed.IsEmpty():
                    geom = fixed
        except Exception:
            pass
        gtype = geom.GetGeometryType() % 1000
        if gtype not in (3, 6):
            # MakeValid může vrátit GeometryCollection – vyber plochy.
            if gtype == 7:
                for i in range(geom.GetGeometryCount()):
                    sub = geom.GetGeometryRef(i)
                    if sub is None:
                        continue
                    st = sub.GetGeometryType() % 1000
                    if st not in (3, 6):
                        continue
                    if acc is None:
                        acc = sub.Clone()
                    else:
                        try:
                            merged = acc.Union(sub)
                        except Exception:
                            continue
                        if merged is not None and not merged.IsEmpty():
                            acc = merged
            continue
        if acc is None:
            acc = geom
        else:
            try:
                merged = acc.Union(geom)
            except Exception:
                continue
            if merged is not None and not merged.IsEmpty():
                acc = merged
    return acc


def _difference_ogr(subject_wkb: bytes, mask) -> list[bytes]:
    from osgeo import ogr

    ogr.UseExceptions()
    subject = ogr.CreateGeometryFromWkb(subject_wkb)
    if subject is None or subject.IsEmpty():
        return []
    try:
        if hasattr(subject, "MakeValid"):
            fixed = subject.MakeValid()
            if fixed is not None and not fixed.IsEmpty():
                subject = fixed
    except Exception:
        pass
    if mask is None or mask.IsEmpty():
        return [subject_wkb]
    try:
        result = subject.Difference(mask)
    except Exception:
        return [bytes(subject.ExportToWkb())]
    if result is None or result.IsEmpty():
        return []
    return _explode_area_wkbs_ogr(result)


def _explode_area_wkbs_ogr(geom) -> list[bytes]:
    out: list[bytes] = []

    def keep(g) -> None:
        if g is None or g.IsEmpty():
            return
        try:
            area = float(g.GetArea())
        except Exception:
            return
        if area < _MIN_AREA_M2:
            return
        out.append(bytes(g.ExportToWkb()))

    gtype = geom.GetGeometryType() % 1000
    if gtype == 3:
        keep(geom)
    elif gtype == 6:
        for i in range(geom.GetGeometryCount()):
            keep(geom.GetGeometryRef(i))
    elif gtype == 7:
        for i in range(geom.GetGeometryCount()):
            sub = geom.GetGeometryRef(i)
            if sub is None:
                continue
            st = sub.GetGeometryType() % 1000
            if st == 3:
                keep(sub)
            elif st == 6:
                for j in range(sub.GetGeometryCount()):
                    keep(sub.GetGeometryRef(j))
    return out


def _rings_to_wkb_ogr(rings: list[list[tuple[float, float]]]) -> bytes | None:
    from osgeo import ogr

    ogr.UseExceptions()
    poly = ogr.Geometry(ogr.wkbPolygon)

    def add_ring(pts: list[tuple[float, float]]) -> None:
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in pts:
            ring.AddPoint_2D(float(x), float(y))
        if pts[0] != pts[-1]:
            ring.AddPoint_2D(float(pts[0][0]), float(pts[0][1]))
        poly.AddGeometry(ring)

    add_ring(rings[0])
    for hole in rings[1:]:
        if len(hole) >= 3:
            add_ring(hole)
    if poly.IsEmpty() or float(poly.GetArea()) < _MIN_AREA_M2:
        return None
    return bytes(poly.ExportToWkb())


class _ShapelyMask:
    """Tenké balení Shapely geometrie se stejným API jako OGR (IsEmpty)."""

    __slots__ = ("geom",)

    def __init__(self, geom):
        self.geom = geom

    def IsEmpty(self) -> bool:
        return self.geom is None or self.geom.is_empty


def _shapely_as_polygons(geom) -> list:
    """Polygon / MultiPolygon / GeometryCollection → seznam Polygon."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return [g for g in geom.geoms if not g.is_empty]
    if geom.geom_type == "GeometryCollection":
        out = []
        for g in geom.geoms:
            out.extend(_shapely_as_polygons(g))
        return out
    return []


def _shapely_make_valid_polys(geom) -> list:
    try:
        from shapely import make_valid
    except ImportError:
        return _shapely_as_polygons(geom)
    try:
        fixed = make_valid(geom)
    except Exception:
        return _shapely_as_polygons(geom)
    return _shapely_as_polygons(fixed)


def _union_shapely(wkbs: list[bytes]):
    try:
        from shapely import from_wkb, unary_union
    except ImportError:
        return None
    geoms = []
    for raw in wkbs:
        if not raw:
            continue
        try:
            g = from_wkb(raw)
        except Exception:
            continue
        if g is None or g.is_empty:
            continue
        geoms.extend(_shapely_make_valid_polys(g))
    if not geoms:
        return None
    merged = None
    try:
        merged = unary_union(geoms)
    except Exception:
        merged = None
    if merged is None or merged.is_empty:
        # Neplatné vstupy / TopologyException → skládej po jednom.
        acc = None
        for g in geoms:
            if acc is None:
                acc = g
                continue
            try:
                nxt = unary_union([acc, g])
            except Exception:
                try:
                    nxt = acc.union(g)
                except Exception:
                    continue
            if nxt is not None and not nxt.is_empty:
                polys = _shapely_make_valid_polys(nxt)
                if not polys:
                    continue
                try:
                    acc = unary_union(polys) if len(polys) > 1 else polys[0]
                except Exception:
                    acc = polys[0]
        merged = acc
    if merged is None or merged.is_empty:
        return None
    polys = _shapely_make_valid_polys(merged)
    if not polys:
        return None
    if len(polys) == 1:
        return _ShapelyMask(polys[0])
    try:
        from shapely import MultiPolygon

        return _ShapelyMask(MultiPolygon(polys))
    except Exception:
        return _ShapelyMask(unary_union(polys))


def _difference_shapely(subject_wkb: bytes, mask) -> list[bytes]:
    try:
        from shapely import from_wkb, to_wkb, unary_union
    except ImportError:
        return [subject_wkb]
    try:
        subject = from_wkb(subject_wkb)
    except Exception:
        return []
    if subject is None or subject.is_empty:
        return []
    subj_polys = _shapely_make_valid_polys(subject)
    if not subj_polys:
        return []
    try:
        subject = unary_union(subj_polys) if len(subj_polys) > 1 else subj_polys[0]
    except Exception:
        subject = subj_polys[0]
    mask_geom = None
    if mask is not None:
        mask_geom = getattr(mask, "geom", mask)
        if hasattr(mask_geom, "IsEmpty") and not hasattr(mask_geom, "is_empty"):
            return [subject_wkb]
        if mask_geom is None or getattr(mask_geom, "is_empty", True):
            return [subject_wkb]
        mask_polys = _shapely_make_valid_polys(mask_geom)
        if not mask_polys:
            return [bytes(to_wkb(subject, hex=False))]
        try:
            mask_geom = (
                unary_union(mask_polys) if len(mask_polys) > 1 else mask_polys[0]
            )
        except Exception:
            mask_geom = mask_polys[0]
    else:
        return [bytes(to_wkb(subject, hex=False))]
    try:
        result = subject.difference(mask_geom)
    except Exception:
        # Fallback: odečítej masku po částech.
        result = subject
        for piece in _shapely_as_polygons(mask_geom):
            try:
                result = result.difference(piece)
            except Exception:
                continue
            if result is None or result.is_empty:
                return []
    if result is None or result.is_empty:
        return []
    return _explode_area_wkbs_shapely(result, to_wkb)


def _explode_area_wkbs_shapely(geom, to_wkb) -> list[bytes]:
    out: list[bytes] = []

    def keep(g) -> None:
        if g is None or g.is_empty:
            return
        if float(g.area) < _MIN_AREA_M2:
            return
        out.append(bytes(to_wkb(g, hex=False)))

    if geom.geom_type == "Polygon":
        keep(geom)
    elif geom.geom_type == "MultiPolygon":
        for g in geom.geoms:
            keep(g)
    elif geom.geom_type == "GeometryCollection":
        for g in geom.geoms:
            if g.geom_type == "Polygon":
                keep(g)
            elif g.geom_type == "MultiPolygon":
                for p in g.geoms:
                    keep(p)
    return out


def _rings_to_wkb_shapely(rings: list[list[tuple[float, float]]]) -> bytes | None:
    try:
        from shapely import Polygon, to_wkb
    except ImportError:
        return None

    def close(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        out = [(float(x), float(y)) for x, y in pts]
        if out[0] != out[-1]:
            out.append(out[0])
        return out

    shell = close(rings[0])
    holes = [close(h) for h in rings[1:] if len(h) >= 3]
    try:
        poly = Polygon(shell, holes or None)
    except Exception:
        return None
    if poly.is_empty or float(poly.area) < _MIN_AREA_M2:
        return None
    return bytes(to_wkb(poly, hex=False))
