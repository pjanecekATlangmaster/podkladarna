"""Tagy zdrojů u objektů mapy (volba jobu ``osm_tags``, výchozí vypnuto; Leo / Petr 2026-11-25).

Objekty v mapě nevznikají ze zdrojů 1:1 (spojení čar přes křižovatky, ořezy, slévání ploch, dedup OSM × DTM),
proto se tagy nenesou celou kresbou, ale párují se až nad hotovou mapou (po zjednodušení, před zápisem) podle tvaru:

* příprava OSM (``prepare_osm_paths``) uloží prvky OSM s tagy a geometrií v souřadnicích jobu
  (``osm_paths/osm_tagged.json``, jen se zapnutou volbou); prvky DTM dodá ``source_tags`` z balíků jobu
  (tagy ve slovníku OSM + ``dtm:*``, Petr 2026-11-25: stejné objekty mají mít stejné tagy),
* čára / obrys plochy se přiřadí objektu, podél kterého leží aspoň ``LINE_SHARE`` její délky do ``tol`` (nejvíc
  pokrytý slučitelný objekt: plot ne na silnici, ``code_family`` × ``tag_family``; části z výšek a vrstevnice ne),
  bod nejbližšímu bodovému objektu do ``POINT_TOL_MM``,
* všechny zdroje se párují najednou, takže objekt, který po dedupu zůstal za OSM i DTM, dostane tagy obou:
  v rámci zdroje jen tagy se shodnou hodnotou u všech prvků, mezi zdroji sjednocení s předností podle
  ``SOURCE_PRIORITY`` (OSM, pak DTM; rozdíl = konflikt v souhrnu), ``src=osm;dtm`` a ID (``osm_id``, ``dtm:id``),
* tag se zapíše do ``<tags><t k="…">…</t></tags>`` objektu .omap (OCD tagy nemá).

Kresbu nemění (jen přidá prvek ``tags`` do objektů). Nespárované prvky, vynechané klíče a konflikty se sčítají
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


# Zdroje tagů v pořadí přednosti: při sloučení objektu z více zdrojů vyhrává u sjednoceného klíče (``surface``,
# ``barrier`` …) hodnota dřívějšího zdroje (OSM je základ kresby), rozdíl se počítá jako konflikt.
SOURCE_PRIORITY = ("osm", "dtm")
ID_KEYS = {"osm": "osm_id", "dtm": "dtm:id"}
# Obecná hodnota (``building=yes``) ustoupí upřesnění z jiného zdroje bez konfliktu.
GENERIC_VALUE = "yes"
# Původ tagů objektu: ``src=osm;dtm``.
SRC_KEY = "src"
# Značka zdroje v názvu části (při shodném pokrytí má přednost objekt z části téhož zdroje).
PART_TOKENS = {"osm": "OSM", "dtm": "DTM"}
# Části bez dat zdrojů (z výšek, LiDARu, ortofota, vrstevnice) se nepárují.
_SKIP_PART_RE = re.compile(
    r"vrstevnic|spádnic|vegetation|porost|sráz|skal|balvan|kupk|prohlub|AOI|DEM|LiDAR|CHM|z výšek|ortofot", re.I
)
_SYMBOL_RE = re.compile(r'symbol="(-?\d+)"')
_ROAD_CODES = frozenset({"501", "502", "503", "504", "505", "506", "507", "508", "509", "512", "529", "532", "533"})
_BARRIER_CODES = frozenset({"416", "513", "514", "515", "516", "517", "518", "519"})


def code_family(code: str | None) -> str | None:
    """Skupina symbolu sady (``518`` → barrier); ``relief`` = vrstevnice a skály (nepárují se), None = neznámá."""
    if not code:
        return None
    c = str(code).split(".")[0]
    if c[:1] in ("1", "2"):
        return "relief"
    if c in _BARRIER_CODES:
        return "barrier"
    if c in ("521", "522"):
        return "building"
    if c in _ROAD_CODES or c.startswith("8"):
        return "road"
    if c[:1] in ("3", "4") or c in ("520", "527", "528"):
        return "cover"
    return None


def tag_family(tags: dict) -> str | None:
    """Skupina prvku podle tagů; None = neznámá (smí na jakýkoli objekt)."""
    if tags.get("building") not in (None, "no"):
        return "building"
    if "barrier" in tags:
        return "barrier"
    if any(k in tags for k in ("highway", "area:highway", "railway", "public_transport")) or tags.get("amenity") == "parking":
        return "road"
    if any(k in tags for k in ("landuse", "natural", "water", "waterway")) or tags.get("leisure") == "garden":
        return "cover"
    return None


def _compatible(rec_family: str | None, obj_family: str | None) -> bool:
    return rec_family is None or obj_family is None or rec_family == obj_family


def _src(rec: dict) -> str:
    return rec.get("src") or "osm"


def _rec_class(rec: dict) -> str:
    """Třída prvku pro souhrny a výběr převažující třídy: OSM ``highway=track``, jiné zdroje ``dtm:Plot``."""
    src = _src(rec)
    if src == "osm":
        return _class_of(rec["tags"])
    return f"{src}:{rec['tags'].get(f'{src}:typ', 'other')}"


def merge_sources(by_src: dict[str, list[dict]]) -> tuple[dict, set[str], Counter]:
    """Tagy objektu z prvků více zdrojů → (tagy, vynechané klíče, konflikty mezi zdroji).

    V rámci zdroje jen shodné hodnoty (``merge_tags``), mezi zdroji sjednocení podle ``SOURCE_PRIORITY``;
    ``src`` a ID prvků (``osm_id``, ``dtm:id``) jdou první.
    """
    order = [s for s in SOURCE_PRIORITY if s in by_src] + sorted(s for s in by_src if s not in SOURCE_PRIORITY)
    head: dict = {SRC_KEY: ";".join(order)}
    body: dict = {}
    dropped: set[str] = set()
    conflicts: Counter = Counter()
    for src in order:
        recs = by_src[src]
        id_key = ID_KEYS.get(src, f"{src}:id")
        ids = [r["id"] if src == "osm" else (r["tags"].get(id_key) or r["id"]) for r in recs]
        head[id_key] = ";".join(ids[:MAX_IDS]) + (";…" if len(ids) > MAX_IDS else "")
        kept, drop = merge_tags([{k: v for k, v in r["tags"].items() if k != id_key} for r in recs])
        dropped.update(drop)
        for k, v in kept.items():
            if k not in body:
                body[k] = v
            elif body[k] == GENERIC_VALUE and v != GENERIC_VALUE:
                body[k] = v  # ``building=yes`` z jednoho zdroje upřesní ``building=house`` z jiného, není to rozpor
            elif body[k] != v and v != GENERIC_VALUE:
                conflicts[k] += 1
    return {**head, **body}, dropped, conflicts


def attach_osm_tags(
    parts: list,
    work_dir: Path,
    *,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    stats: dict | None = None,
    symbol_codes: dict[int, str] | None = None,
    extra_records: list[dict] | None = None,
) -> list:
    """Části mapy s tagy zdrojů (OSM, DTM …) u spárovaných objektů; kresbu nemění.

    ``symbol_codes`` (index symbolu → kód sady) hlídá, aby plot nedostal tagy silnice a naopak
    (``code_family`` / ``tag_family``); ``extra_records`` jsou prvky dalších zdrojů (``source_tags``).
    """
    import numpy as np
    import shapely
    from shapely.geometry import LineString

    from app.pipeline.oom_coords import projected_to_map_coord
    from app.pipeline.oom_import import OomObjectPart

    st = stats if stats is not None else {}
    src = Path(work_dir) / "osm_paths" / TAGGED_NAME
    records: list[dict] = json.loads(src.read_text(encoding="utf-8")) if src.is_file() else []
    records += list(extra_records or [])
    if not records:
        st["missing"] = True
        return parts
    um_per_m = 1e6 / float(scale)
    line_tol = max(LINE_TOL_M * um_per_m, LINE_TOL_MM * 1000.0)
    point_tol = POINT_TOL_MM * 1000.0
    codes = symbol_codes or {}

    def to_map(pts):
        return [
            projected_to_map_coord(x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg)
            for x, y in pts
        ]

    # Objekty mapy: (index části, začátek objektu v XML části, typ, geometrie, skupina symbolu, název části).
    objs: list[tuple[int, int, int, object, str | None, str]] = []
    for pi, part in enumerate(parts):
        if _SKIP_PART_RE.search(part.name):
            continue
        for m in _OBJECT_RE.finditer(part.objects_xml):
            sm = _SYMBOL_RE.search(m.group(0))
            fam = code_family(codes.get(int(sm.group(1)))) if sm else None
            if fam == "relief":
                continue
            geom = _object_geometry(int(m.group(1)), m.group(0))
            if geom is not None and not geom.is_empty:
                objs.append((pi, m.start(), int(m.group(1)), geom, fam, part.name))
    line_ids = [i for i, o in enumerate(objs) if o[2] != 0]
    point_ids = [i for i, o in enumerate(objs) if o[2] == 0]
    line_tree = shapely.STRtree([objs[i][3] for i in line_ids]) if line_ids else None

    matched: dict[int, list[dict]] = {}
    unpaired = Counter()
    sources = Counter(_src(r) for r in records)
    unpaired_src = Counter()
    # Body (po zdrojích): každý bodový objekt si vezme nejbližší bod (lavička nepřevezme tagy přechodu vedle).
    for source in sources:
        node_recs = [r for r in records if "point" in r and _src(r) == source]
        taken: set[int] = set()
        if node_recs and point_ids:
            node_pts = [shapely.Point(*to_map([r["point"]])[0]) for r in node_recs]
            node_tree = shapely.STRtree(node_pts)
            for oi in point_ids:
                geom = objs[oi][3]
                j = node_tree.nearest(geom)
                if j is None or int(j) in taken or node_pts[int(j)].distance(geom) > point_tol:
                    continue
                if not _compatible(tag_family(node_recs[int(j)]["tags"]), objs[oi][4]):
                    continue
                taken.add(int(j))
                matched.setdefault(oi, []).append(node_recs[int(j)])
        for j, r in enumerate(node_recs):
            if j not in taken:
                unpaired[_rec_class(r)] += 1
                unpaired_src[source] += 1
    # Čáry a obrysy: prvek patří slučitelnému objektu, podél kterého leží nejvíc jeho délky (při shodě objekt
    # z části téhož zdroje – OSM cesta zůstane u „OSM cesty“, ne u souběžné ZABAGED ulice).
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
                fam = tag_family(rec["tags"])
                token = PART_TOKENS.get(_src(rec), "")
                cands = [
                    (hits, bool(token) and token in objs[oi][5], -oi)
                    for oi, hits in score.items()
                    if hits >= LINE_SHARE * total and _compatible(fam, objs[oi][4])
                ]
                if cands:
                    best = -max(cands)[2]
        if best is None:
            unpaired[_rec_class(rec)] += 1
            unpaired_src[_src(rec)] += 1
            continue
        matched.setdefault(best, []).append(rec)

    def _drop(r):
        unpaired[_rec_class(r)] += 1
        unpaired_src[_src(r)] += 1

    # Objekt z prvků různé třídy (silnice + souběžný chodník mimo kresbu): v každém zdroji jen převažující třída;
    # zdroje se slučují, jen když mají slučitelnou skupinu s hlavním zdrojem (podle ``SOURCE_PRIORITY``).
    incompatible = Counter()
    multi = 0
    dropped_keys = Counter()
    conflicts = Counter()
    inserts: dict[int, dict[int, str]] = {}
    for i, recs in matched.items():
        by_src: dict[str, list[dict]] = {}
        for r in recs:
            by_src.setdefault(_src(r), []).append(r)
        for s, rs in list(by_src.items()):
            classes = Counter(_rec_class(r) for r in rs)
            if len(classes) > 1:
                main = classes.most_common(1)[0][0]
                by_src[s] = [r for r in rs if _rec_class(r) == main]
                for r in rs:
                    if _rec_class(r) != main:
                        _drop(r)
        order = [s for s in SOURCE_PRIORITY if s in by_src] + sorted(s for s in by_src if s not in SOURCE_PRIORITY)
        main_fam = next((f for s in order if (f := tag_family(by_src[s][0]["tags"])) is not None), None)
        for s in order:
            fam = tag_family(by_src[s][0]["tags"])
            if fam is not None and main_fam is not None and fam != main_fam:
                for r in by_src.pop(s):
                    incompatible[_rec_class(r)] += 1
                    _drop(r)
        if not by_src:
            continue
        if len(by_src) > 1:
            multi += 1
        tags, dropped, conf = merge_sources(by_src)
        dropped_keys.update(dropped)
        conflicts.update(conf)
        pi, start = objs[i][0], objs[i][1]
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
        tagged=sum(len(v) for v in inserts.values()),
        elements=len(records),
        paired=len(records) - sum(unpaired.values()),
        unpaired=dict(unpaired.most_common()),
        dropped_keys=dict(dropped_keys.most_common()),
        sources={s: {"elements": n, "paired": n - unpaired_src[s]} for s, n in sources.items()},
        multi_source=multi,
        conflicts=dict(conflicts.most_common()),
        incompatible=dict(incompatible.most_common()),
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
    srcs = stats.get("sources") or {}
    if set(srcs) <= {"osm"}:
        return (
            f"OSM tagy: {stats.get('tagged', 0)} z {stats.get('objects', 0)} objektů OSM s tagy, "
            f"spárováno {stats.get('paired', 0)} z {stats.get('elements', 0)} prvků OSM; nespárované: {top}"
        )
    per = ", ".join(
        f"{s.upper()} {v.get('paired', 0)} z {v.get('elements', 0)}"
        for s, v in sorted(srcs.items(), key=lambda kv: (kv[0] not in SOURCE_PRIORITY, kv[0]))
    )
    conf = ", ".join(f"{k} {v}" for k, v in list((stats.get("conflicts") or {}).items())[:6]) or "–"
    return (
        f"Tagy zdrojů: {stats.get('tagged', 0)} z {stats.get('objects', 0)} objektů s tagy, "
        f"z více zdrojů {stats.get('multi_source', 0)}; spárované prvky {per}; "
        f"konflikty (přednost OSM): {conf}; nespárované: {top}"
    )
