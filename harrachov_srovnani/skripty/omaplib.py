"""Načtení objektů .omap do shapely v EPSG:5514 (kód symbolu, typ symbolu, geometrie).
Převzato z local_test/otocka_sprint_narrow/omaplib.py; čtení přes oom_preview._read_omap_bytes."""
import math
import xml.etree.ElementTree as ET
from pathlib import Path

from shapely.geometry import LineString, MultiLineString, Point, Polygon


def load_objects(path):
    from app.pipeline.oom_preview import _read_omap_bytes
    root = ET.fromstring(_read_omap_bytes(Path(path)))
    N = root.tag.split("}")[0] + "}"
    geo = root.find(f".//{N}georeferencing")
    sc = float(geo.get("scale"))
    g = math.radians(float(geo.get("grivation")))
    ref = geo.find(f".//{N}projected_crs/{N}ref_point")
    rx, ry = float(ref.get("x")), float(ref.get("y"))
    fac = 1000 / (sc / 1000)

    def tp(mx, my):
        return rx + mx / fac * math.cos(g) - my / fac * math.sin(g), ry - mx / fac * math.sin(g) - my / fac * math.cos(g)

    syms = {s.get("id"): (s.get("code") or "", int(s.get("type") or 0), s.get("name") or "") for s in root.iter(N + "symbol")}
    out = []
    for i, o in enumerate(root.iter(N + "object")):
        code, stype, name = syms.get(o.get("symbol"), ("", 0, ""))
        c = o.find(N + "coords")
        if c is None or not (c.text or "").strip():
            continue
        parts, cur = [], []
        for q in c.text.strip().split(";"):
            v = q.split()
            if len(v) < 2:
                continue
            cur.append(tp(float(v[0]), float(v[1])))
            if len(v) >= 3 and int(v[2]) & 16:
                parts.append(cur)
                cur = []
        if cur:
            parts.append(cur)
        if stype == 4:
            rings = [p for p in parts if len(p) >= 4]
            if not rings:
                continue
            try:
                geom = Polygon(rings[0], rings[1:]).buffer(0)
            except ValueError:
                continue
            kind = "area"
        elif stype == 1 or (len(parts) == 1 and len(parts[0]) == 1):
            geom, kind = Point(parts[0][0]), "point"
        else:
            lines = [LineString(p) for p in parts if len(p) >= 2]
            if not lines:
                continue
            geom = lines[0] if len(lines) == 1 else MultiLineString(lines)
            kind = "line"
        out.append({"i": i, "code": code, "name": name, "kind": kind, "geom": geom})
    return out
