"""Tagy OSM u objektů mapy (volba jobu ``osm_tags``, výchozí vypnuto; Leo / Petr 2026-11-25).

Objekty v mapě nevznikají z OSM 1:1 (spojení čar přes křižovatky, ořezy, slévání ploch), proto se tagy
nenesou celou kresbou, ale párují se až nad hotovou mapou (po zjednodušení, před zápisem) podle tvaru:

* příprava OSM (``prepare_osm_paths``) uloží prvky OSM s tagy a geometrií v souřadnicích jobu
  (``osm_paths/osm_tagged.json``, jen se zapnutou volbou),
* čára / obrys plochy OSM se přiřadí objektu části „OSM …“, podél kterého leží aspoň ``LINE_SHARE``
  její délky do ``tol`` (nejvíc pokrytý objekt), bod OSM nejbližšímu bodovému objektu do ``POINT_TOL_MM``,
* objekt z více prvků OSM dostane jen tagy se shodnou hodnotou u všech, ostatní klíče se vynechají
  (konflikt); ``osm_id`` je seznam ``way/1;way/2`` (konvence OSM pro více hodnot),
* tag se zapíše do ``<tags><t k="…">…</t></tags>`` objektu .omap (OCD tagy nemá).

Kresbu nemění (jen přidá prvek ``tags`` do objektů). Nespárované prvky a vynechané klíče se sčítají
(``osm_tagged_report.json`` v pracovní složce a řádek logu), aby šlo párování postupně zlepšovat.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape

TAGGED_NAME = "osm_tagged.json"
REPORT_NAME = "osm_tagged_report.json"
# Podíl délky čáry OSM, který musí ležet podél objektu.
LINE_SHARE = 0.7
# Tolerance párování čar: aspoň metry v terénu, aspoň mm mapy (zjednodušení 0,1 mm, spojení čar).
LINE_TOL_M = 1.5
LINE_TOL_MM = 0.25
# Body se v lese a MTBO odsouvají od cest až o 0,9 mm mapy.
POINT_TOL_MM = 1.0
# Krok vzorkování čáry OSM.
SAMPLE_M = 2.0
# Nejvýš tolik ID v ``osm_id`` (dlouhá silnice spojená z mnoha úseků).
MAX_IDS = 50
# Hlavní klíče pro souhrn nespárovaných prvků („highway=track“).
CLASS_KEYS = (
    "highway", "building", "landuse", "natural", "waterway", "water", "barrier", "railway", "power",
    "amenity", "leisure", "man_made", "historic", "tourism", "area:highway", "public_transport",
)

_OBJECT_RE = re.compile(r'<object type="(\d)" symbol="-?\d+"[^>]*>.*?</object>', re.S)
_COORDS_RE = re.compile(r"<coords[^>]*>([^<]*)</coords>")
# Příznaky souřadnice v .omap: 2 = konec uzavřeného prstence, 16 = konec díry.
_RING_END = 2 | 16


def write_tagged_elements(work_dir: Path, elements: list[dict]) -> int:
    """Prvky OSM s tagy (body, cesty, multipolygony) v souřadnicích jobu → ``osm_paths/osm_tagged.json``."""
    from app.pipeline.crs_5514 import wgs84_to_projected
    from app.pipeline.osm_paths import _nodes_geometry_to_5514, osm_area_polygons_5514

    out: list[dict] = []
    for el in elements:
        tags = el.get("tags") or {}
        typ = el.get("type")
        if not tags or el.get("id") is None:
            continue
        rec = {"id": f"{typ}/{el.get('id')}", "tags": tags}
        if typ == "node":
            if el.get("lat") is None or el.get("lon") is None:
                continue
            rec["point"] = list(wgs84_to_projected(float(el["lat"]), float(el["lon"])))
        elif typ == "way":
            pts = _nodes_geometry_to_5514(el.get("geometry"))
            if len(pts) < 2:
                continue
            rec["lines"] = [pts]
        elif typ == "relation":
            rings = [r for poly in osm_area_polygons_5514(el) for r in poly if len(r) >= 2]
            if not rings:
                continue
            rec["lines"] = rings
        else:
            continue
        out.append(rec)
    dest = Path(work_dir) / "osm_paths" / TAGGED_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return len(out)


def _class_of(tags: dict) -> str:
    for k in CLASS_KEYS:
        if k in tags:
            return f"{k}={tags[k]}" if k != "building" else "building"
    return "other"


def _object_geometry(type_: int, obj_xml: str):
    """Bod nebo čáry (obrysy ploch jako čáry) objektu v µm mapy."""
    from shapely.geometry import LineString, MultiLineString, Point

    m = _COORDS_RE.search(obj_xml)
    if not m:
        return None
    rings: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    for tok in m.group(1).strip().strip(";").split(";"):
        a = tok.split()
        if len(a) < 2:
            continue
        cur.append((float(a[0]), float(a[1])))
        if len(a) > 2 and int(a[2]) & _RING_END:
            rings.append(cur)
            cur = []
    if cur:
        rings.append(cur)
    if type_ == 0:
        return Point(rings[0][0]) if rings and rings[0] else None
    lines = [r for r in rings if len(r) >= 2]
    if not lines:
        return None
    return LineString(lines[0]) if len(lines) == 1 else MultiLineString(lines)


def merge_tags(tag_sets: list[dict]) -> tuple[dict, set[str]]:
    """Jen klíče se shodnou hodnotou u všech prvků; vrací i vynechané (konfliktní nebo chybějící) klíče."""
    if not tag_sets:
        return {}, set()
    keys = set().union(*tag_sets)
    kept: dict = {}
    dropped: set[str] = set()
    for k in sorted(keys):
        vals = {ts.get(k) for ts in tag_sets}
        if len(vals) == 1 and None not in vals:
            kept[k] = tag_sets[0][k]
        else:
            dropped.add(k)
    return kept, dropped


def tags_xml(tags: dict) -> str:
    items = "".join(
        f'<t k="{escape(str(k), {chr(34): "&quot;"})}">{escape(str(v))}</t>' for k, v in tags.items()
    )
    return f"<tags>{items}</tags>"


def attach_osm_tags(
    parts: list,
    work_dir: Path,
    *,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    stats: dict | None = None,
) -> list:
    """Části mapy s tagy OSM u spárovaných objektů částí „OSM …“ (ostatní části beze změny)."""
    import numpy as np
    import shapely
    from shapely.geometry import LineString

    from app.pipeline.oom_coords import projected_to_map_coord
    from app.pipeline.oom_import import OomObjectPart

    st = stats if stats is not None else {}
    src = Path(work_dir) / "osm_paths" / TAGGED_NAME
    if not src.is_file():
        st["missing"] = True
        return parts
    records = json.loads(src.read_text(encoding="utf-8"))
    um_per_m = 1e6 / float(scale)
    line_tol = max(LINE_TOL_M * um_per_m, LINE_TOL_MM * 1000.0)
    point_tol = POINT_TOL_MM * 1000.0

    def to_map(pts):
        return [
            projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg)
            for x, y in pts
        ]

    # Objekty částí OSM: (index části, začátek objektu v XML části, typ, geometrie).
    objs: list[tuple[int, int, int, object]] = []
    for pi, part in enumerate(parts):
        if "OSM" not in part.name:
            continue
        for m in _OBJECT_RE.finditer(part.objects_xml):
            geom = _object_geometry(int(m.group(1)), m.group(0))
            if geom is not None and not geom.is_empty:
                objs.append((pi, m.start(), int(m.group(1)), geom))
    line_ids = [i for i, o in enumerate(objs) if o[2] != 0]
    point_ids = [i for i, o in enumerate(objs) if o[2] == 0]
    line_tree = shapely.STRtree([objs[i][3] for i in line_ids]) if line_ids else None

    matched: dict[int, list[dict]] = {}
    unpaired = Counter()
    n_elements = len(records)
    # Body: každý bodový objekt si vezme nejbližší bod OSM (lavička nepřevezme tagy přechodu vedle).
    node_recs = [r for r in records if "point" in r]
    taken: set[int] = set()
    if node_recs and point_ids:
        node_pts = [shapely.Point(*to_map([r["point"]])[0]) for r in node_recs]
        node_tree = shapely.STRtree(node_pts)
        for oi in point_ids:
            geom = objs[oi][3]
            j = node_tree.nearest(geom)
            if j is None or int(j) in taken or node_pts[int(j)].distance(geom) > point_tol:
                continue
            taken.add(int(j))
            matched[oi] = [node_recs[int(j)]]
    for j, r in enumerate(node_recs):
        if j not in taken:
            unpaired[_class_of(r["tags"])] += 1
    # Čáry a obrysy: prvek OSM patří objektu, podél kterého leží nejvíc jeho délky.
    for rec in records:
        if "point" in rec:
            continue
        best: int | None = None
        if line_tree is not None:
            score: Counter = Counter()
            total = 0
            for ring in rec.get("lines") or []:
                ln = LineString(to_map(ring))
                if ln.length <= 0:
                    continue
                step = max(SAMPLE_M * um_per_m, 1.0)
                n = max(2, int(ln.length / step) + 1)
                samples = shapely.line_interpolate_point(ln, np.linspace(0.0, ln.length, n))
                total += n
                for j in line_tree.query(ln, predicate="dwithin", distance=line_tol):
                    near = shapely.dwithin(samples, objs[line_ids[int(j)]][3], line_tol)
                    score[line_ids[int(j)]] += int(near.sum())
            if total and score:
                cand, hits = score.most_common(1)[0]
                if hits >= LINE_SHARE * total:
                    best = cand
        if best is None:
            unpaired[_class_of(rec["tags"])] += 1
            continue
        matched.setdefault(best, []).append(rec)
    # Objekt z prvků různé třídy (silnice + souběžný chodník mimo kresbu): tagy jen z převažující třídy,
    # ostatní prvky se počítají jako nespárované.
    for i, recs in list(matched.items()):
        classes = Counter(_class_of(r["tags"]) for r in recs)
        if len(classes) > 1:
            main = classes.most_common(1)[0][0]
            matched[i] = [r for r in recs if _class_of(r["tags"]) == main]
            for r in recs:
                if _class_of(r["tags"]) != main:
                    unpaired[_class_of(r["tags"])] += 1

    dropped_keys = Counter()
    # (část, začátek objektu) → XML tagů; pořadí objektů v části se nemění.
    inserts: dict[int, dict[int, str]] = {}
    for i, recs in matched.items():
        pi, start, _t, _g = objs[i]
        tags, dropped = merge_tags([r["tags"] for r in recs])
        ids = [r["id"] for r in recs]
        tags = {"osm_id": ";".join(ids[:MAX_IDS]) + (";…" if len(ids) > MAX_IDS else ""), **tags}
        dropped_keys.update(dropped)
        inserts.setdefault(pi, {})[start] = tags_xml(tags)
    out = []
    for pi, part in enumerate(parts):
        ins = inserts.get(pi)
        if not ins:
            out.append(part)
            continue

        def _add(m, ins=ins):
            t = ins.get(m.start())
            return m.group(0) if t is None else m.group(0)[: -len("</object>")] + t + "</object>"

        out.append(
            OomObjectPart(name=part.name, objects_xml=_OBJECT_RE.sub(_add, part.objects_xml), count=part.count)
        )
    st.update(
        objects=len(objs),
        tagged=len(matched),
        elements=n_elements,
        paired=n_elements - sum(unpaired.values()),
        unpaired=dict(unpaired.most_common()),
        dropped_keys=dict(dropped_keys.most_common()),
    )
    try:
        (Path(work_dir) / REPORT_NAME).write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return out


def log_line(stats: dict) -> str:
    if stats.get("missing"):
        return "OSM tagy: chybí prvky OSM z přípravy, tagy se nezapsaly"
    top = ", ".join(f"{k} {v}" for k, v in list((stats.get("unpaired") or {}).items())[:8]) or "–"
    return (
        f"OSM tagy: {stats.get('tagged', 0)} z {stats.get('objects', 0)} objektů OSM s tagy, "
        f"spárováno {stats.get('paired', 0)} z {stats.get('elements', 0)} prvků OSM; nespárované: {top}"
    )
