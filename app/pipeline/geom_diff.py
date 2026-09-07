"""Odčítání polygonů (OGR Difference) – KP open land minus ZABAGED/OSM masky.

KP žlutá (401) pokrývá celý otevřený terén jedním nebo několika obřími
polygony. Stejné místo často kreslí i ZABAGED louka nebo OSM orná (412).
Bez odečtu vzniknou dva překryté objekty; s odečtem KP 401 vyplní jen mezery.
"""

from __future__ import annotations

_MIN_AREA_M2 = 12.0


def union_polygon_wkbs(wkbs: list[bytes]):
    """Spojí polygony do jedné OGR geometrie, nebo None když nic není."""
    if not wkbs:
        return None
    try:
        from osgeo import ogr
    except ImportError:
        return None
    ogr.UseExceptions()
    acc = None
    for raw in wkbs:
        if not raw:
            continue
        geom = ogr.CreateGeometryFromWkb(raw)
        if geom is None or geom.IsEmpty():
            continue
        # Jen plochy – linie by Difference pokazily.
        gtype = geom.GetGeometryType() % 1000
        if gtype not in (3, 6):  # Polygon, MultiPolygon
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


def difference_polygon_wkb(subject_wkb: bytes, mask) -> list[bytes]:
    """Vrátí WKB polygonů (subject − mask). Bez masky / při chybě původní subject."""
    if not subject_wkb:
        return []
    try:
        from osgeo import ogr
    except ImportError:
        return [subject_wkb]
    ogr.UseExceptions()
    subject = ogr.CreateGeometryFromWkb(subject_wkb)
    if subject is None or subject.IsEmpty():
        return []
    if mask is None or mask.IsEmpty():
        return [subject_wkb]
    try:
        result = subject.Difference(mask)
    except Exception:
        return [subject_wkb]
    if result is None or result.IsEmpty():
        return []
    return _explode_area_wkbs(result)


def _explode_area_wkbs(geom) -> list[bytes]:
    """MultiPolygon → seznam polygonů; zahodí úlomky pod minimální plochou."""
    from osgeo import ogr

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
    elif gtype == 7:  # GeometryCollection
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


def rings_to_polygon_wkb(rings: list[list[tuple[float, float]]]) -> bytes | None:
    """První prstenec = obrys, další = díry. Souřadnice v metrech (5514)."""
    if not rings or len(rings[0]) < 3:
        return None
    try:
        from osgeo import ogr
    except ImportError:
        return None
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
