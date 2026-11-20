"""Dánsko: GeoDanmark (Klimadatastyrelsen + obce) jako obdoba české DTM pro kresbu.

GeoDanmark je celostátní topografická databáze ve velkém měřítku (obce ji udržují, přesnost ~0,1 m). Do kresby se bere:

- ``bygning`` → budovy (stav „Anlagt“ / „Under nedlæggelse“, viditelné),
- ``hegn`` → ploty,
- ``parkering`` → parkoviště (obrys je uzavřená čára),
- vozovky: GeoDanmark nemá plochy silnic, jen okraje (``vejkant``) a osy (``vejmidte``). Vozovka vznikne z kolmých
  řezů po ``ROAD_STEP_M`` podél osy silnice k nejbližšímu okraji vozovky po každé straně (medián v okně, kusy bez
  okraje na obou stranách vynechá) – tam ji pak kreslí plochou / podle šířky jako české DTM. Osy se spojí přes zlomy,
  ústí vedlejší ulice (řez tam okraj nenajde) do ``ROAD_GAP_MAX_M`` se přemostí šířkou ze sousedních řezů a střed
  křižovatky je stěna okrajů vozovek kolem uzlu os (``junction_polygons``). Chodníky a stezky
  (okraje ``Sti`` / ``Cykelsti``) zatím ne.

WFS Datafordeleru s API klíčem (``reference_dk``), EPSG:25832, GeoJSON, stránkování po ``PAGE_SIZE``. Licence CC BY 4.0,
uvést GeoDanmark. Výsledek se zapíše do ``work_dir/dtm`` stejně jako BGT, ETAK, GRB.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median
from typing import Callable

from app.pipeline import build_stats, dtm_fences
from app.pipeline.bgt_nl import clean_polygons, feature_lines, feature_polygons, write_dtm_files
from app.pipeline.fmzk_at import reproject_features
from app.pipeline.reference_dk import DK_CRS, KEY_PLACEHOLDER, redact, with_key

GEODK_WFS = "https://wfs.datafordeler.dk/GEODKV/GEODKV_WFS/1.0.0/WFS"
SOURCE_CRS = DK_CRS
WORKERS = 3
PAGE_SIZE = 10000
MAX_PAGES = 20
HTTP_TIMEOUT_S = 180
HTTP_RETRIES = 4  # i 401: nový klíč Datafordeleru prvních ~30 min střídavě odmítá
MAX_RESPONSE_BYTES = 300_000_000
LAYERS = ("bygning", "hegn", "parkering", "vejmidte", "vejkant")
BUILDING_STATES = frozenset({"Anlagt", "Under nedlæggelse"})
ROAD_EDGE_TYPES = frozenset({"Kørevej", "Internvej", "Indkørselsvej"})
ROAD_STEP_M = 2.0  # rozestup kolmých řezů podél osy
ROAD_REACH_M = 15.0  # nejdál hledaný okraj vozovky od osy (na každou stranu)
ROAD_MIN_SIDE_M = 1.0
ROAD_WIDTH_M = (2.5, 30.0)
ROAD_MEDIAN_WIN = 5  # okno mediánu šířek (řezů)
ROAD_MIN_RUN = 3  # nejkratší souvislý kus vozovky (řezů)
# Křižovatky: v ústí vedlejší ulice kolmý řez okraj nenajde (obrubník zahýbá do ulice), střed křižovatky by zůstal
# bílý. V uzlu os (stupeň ≥ 3) se vezme plocha ohraničená okraji vozovek v kruhu o poloměru
# půl šířky nejširší ulice + ``JUNCTION_EXTRA_M`` (stěna, ve které leží uzel), omezená pásem os ±(půl šířky + ``JUNCTION_SIDE_M``).
JUNCTION_EXTRA_M = 10.0
ROAD_GAP_MAX_M = 30.0  # krátký úsek osy bez okrajů (ústí ulice) se přemostí šířkou ze sousedních řezů
JUNCTION_MAX_R_M = 25.0
JUNCTION_SIDE_M = 2.5
JUNCTION_ARM_MIN_VALID = 0.5  # podíl platných řezů osy, aby byla ramenem křižovatky
JUNCTION_NEAR_M = 30.0  # šířky ulic pro uzel z řezů do této vzdálenosti
# Sonda: 1 budova u Aarhusu (klíč dosadí ``source_status`` až při dotazu).
GEODK_PROBE = (
    f"{GEODK_WFS}?apikey={KEY_PLACEHOLDER}&service=WFS&version=2.0.0&request=GetFeature"
    "&typeNames=geodkv_v001:bygning_current&count=1&outputFormat=application/json"
)

LogFn = Callable[[str], None]


def wfs_url(layer: str, bounds: tuple[float, float, float, float], start: int = 0) -> str:
    """GetFeature (GeoJSON) se zástupným klíčem; klíč dosadí ``_get_json`` až při dotazu."""
    xmin, ymin, xmax, ymax = bounds
    query = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": f"geodkv_v001:{layer}_current",
        "outputFormat": "application/json",
        "srsName": SOURCE_CRS,
        "bbox": f"{xmin:.1f},{ymin:.1f},{xmax:.1f},{ymax:.1f},urn:ogc:def:crs:EPSG::25832",
        "sortBy": "objectid",  # stálé pořadí stránek (bez řazení může stránkování objekty vynechat / zdvojit)
        "count": str(PAGE_SIZE),
        "startIndex": str(start),
    }
    return f"{GEODK_WFS}?apikey={KEY_PLACEHOLDER}&{urllib.parse.urlencode(query)}"


def _get_json(url: str) -> tuple[dict, int]:
    from app.pipeline.fetch_openzu import USER_AGENT

    for attempt in range(HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(with_key(url), headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"GeoDanmark: odpověď větší než {MAX_RESPONSE_BYTES // 1_000_000} MB")
            return json.loads(raw), len(raw)
        except urllib.error.HTTPError as exc:  # nese URL s klíčem – dál jen kód
            code = exc.code
            if attempt >= HTTP_RETRIES or not (code in (401, 403, 429) or code >= 500):
                raise RuntimeError(f"GeoDanmark: HTTP {code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
            if attempt >= HTTP_RETRIES:
                raise RuntimeError(redact(f"GeoDanmark: {type(exc).__name__}")) from None
        time.sleep(2.0 * (attempt + 1))
    raise AssertionError("nedosažitelné")


def _fetch_layer(layer: str, bounds, fetch) -> tuple[list[dict], int]:
    feats: list[dict] = []
    nbytes = 0
    for page in range(MAX_PAGES):
        data, n = fetch(wfs_url(layer, bounds, page * PAGE_SIZE))
        nbytes += n
        batch = data.get("features") or []
        feats.extend(f for f in batch if f.get("geometry"))
        if len(batch) < PAGE_SIZE:
            break
    return feats, nbytes


def fetch_layers(bounds, layers=LAYERS, *, workers: int = WORKERS, fetch=_get_json) -> tuple[dict[str, list[dict]], int]:
    """``{vrstva: [objekt, …]}`` pro obálku výřezu v EPSG:25832 (vrstvy souběžně)."""
    layers = tuple(layers)
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="geodk") as ex:
        results = list(ex.map(lambda layer: _fetch_layer(layer, bounds, fetch), layers))
    return {layer: feats for layer, (feats, _n) in zip(layers, results)}, sum(n for _f, n in results)


def _props(feat: dict) -> dict:
    return feat.get("properties") or {}


def road_polygons(axes: list[list[tuple[float, float]]], edges: list[list[tuple[float, float]]]) -> list:
    """Plochy vozovek z os a okrajů: kolmé řezy po ``ROAD_STEP_M``, vzdálenost k nejbližšímu okraji na obou stranách
    (medián v okně), z dvojic sousedních platných řezů čtyřúhelníky sloučené po osách. Vrací shapely polygony."""
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import linemerge, unary_union
    from shapely.strtree import STRtree

    edge_lines = [LineString(e) for e in edges if len(e) >= 2]
    if not edge_lines:
        return []
    tree = STRtree(edge_lines)
    # osy spojené přes uzly stupně 2 (jinak na každém zlomu osy zůstala až 2m bílá štěrbina)
    merged_axes = linemerge([LineString(a) for a in axes if len(a) >= 2 and LineString(a).length > 0])
    lines = [g for g in getattr(merged_axes, "geoms", [merged_axes]) if not g.is_empty]
    ltree = STRtree(lines)

    def at_junction(li: int, pt) -> bool:
        """Konec osy leží na jiné ose (křižovatka), ne slepý konec."""
        q = Point(pt)
        return any(int(k) != li and lines[int(k)].distance(q) <= 1.0 for k in ltree.query(q.buffer(1.0)))

    out = []
    widths = []  # (osa, x, y, šířka) platných řezů pro křižovatky
    for li, line in enumerate(lines):
        length = line.length
        n = int(length // ROAD_STEP_M)
        if n < ROAD_MIN_RUN:
            continue
        samples = []  # (x, y, nx, ny, levá, pravá) nebo None
        geo = []  # (x, y, nx, ny) každého řezu (i neplatného) nebo None
        dists = [i * ROAD_STEP_M for i in range(n + 1)]
        if length - dists[-1] > 0.05:
            dists.append(length)  # řez i na konci osy
        for d in dists:
            p = line.interpolate(d)
            a, b = line.interpolate(max(0.0, d - 0.5)), line.interpolate(min(length, d + 0.5))
            dx, dy = b.x - a.x, b.y - a.y
            norm = math.hypot(dx, dy)
            if norm == 0:
                samples.append(None)
                geo.append(None)
                continue
            nx, ny = -dy / norm, dx / norm
            geo.append((p.x, p.y, nx, ny))
            sides = []
            for s in (1.0, -1.0):
                cut = LineString([(p.x, p.y), (p.x + s * nx * ROAD_REACH_M, p.y + s * ny * ROAD_REACH_M)])
                best = None
                for k in tree.query(cut):
                    hit = cut.intersection(edge_lines[int(k)])
                    if not hit.is_empty:
                        dist = p.distance(hit)
                        best = dist if best is None else min(best, dist)
                sides.append(best)
            left, right = sides
            ok = (
                left is not None and right is not None and left >= ROAD_MIN_SIDE_M and right >= ROAD_MIN_SIDE_M
                and ROAD_WIDTH_M[0] <= left + right <= ROAD_WIDTH_M[1]
            )
            samples.append((p.x, p.y, nx, ny, left, right) if ok else None)
        # medián v okně přes platné řezy (jednotlivé odskoky k vjezdům a zálivům se neprojeví)
        half = ROAD_MEDIAN_WIN // 2
        smooth = []
        line_widths = []
        for i, sm in enumerate(samples):
            if sm is None:
                smooth.append(None)
                continue
            win = [samples[j] for j in range(max(0, i - half), min(len(samples), i + half + 1)) if samples[j]]
            smooth.append((sm[0], sm[1], sm[2], sm[3], median(w[4] for w in win), median(w[5] for w in win)))
            line_widths.append((li, sm[0], sm[1], smooth[-1][4] + smooth[-1][5]))
        if len(line_widths) >= JUNCTION_ARM_MIN_VALID * len(samples):
            # rameno křižovatky a přemostění ústí jen u osy, která je převážně změřená vozovka
            widths.extend(line_widths)
            _bridge_gaps(smooth, geo, dists, at_junction(li, line.coords[0]), at_junction(li, line.coords[-1]))
        quads, run = [], []

        def flush():
            if len(run) >= ROAD_MIN_RUN:
                for u, v in zip(run, run[1:]):
                    quads.append(Polygon([
                        (u[0] + u[2] * u[4], u[1] + u[3] * u[4]), (v[0] + v[2] * v[4], v[1] + v[3] * v[4]),
                        (v[0] - v[2] * v[5], v[1] - v[3] * v[5]), (u[0] - u[2] * u[5], u[1] - u[3] * u[5]),
                    ]).buffer(0))
            run.clear()

        for sm in smooth:
            if sm is None:
                flush()
            else:
                run.append(sm)
        flush()
        if quads:
            out.append(unary_union(quads))
    out.extend(junction_polygons(lines, edge_lines, tree, widths))
    if not out:
        return []
    merged = unary_union(out).buffer(0.1, join_style=2).buffer(-0.1, join_style=2)
    return [g for g in getattr(merged, "geoms", [merged]) if g.area >= 10.0]


def _bridge_gaps(smooth: list, geo: list, dists: list[float], start_junction: bool, end_junction: bool) -> None:
    """Úseky bez okraje do ``ROAD_GAP_MAX_M`` doplní šířkou ze sousedních platných řezů (uvnitř osy lineárně,
    na konci osy v křižovatce konstantně až do uzlu). Slepé konce a delší úseky zůstanou bez vozovky."""
    valid, run = [], []
    for i, sm in enumerate(smooth + [None]):
        if sm is not None:
            run.append(i)
            continue
        if len(run) >= ROAD_MIN_RUN:  # kotvou jsou jen souvislé kusy vozovky, ne osamělý řez
            valid.extend(run)
        run = []
    if not valid:
        return

    def put(i: int, left: float, right: float) -> None:
        if geo[i] is not None:
            smooth[i] = (*geo[i], left, right)

    for a, b in zip(valid, valid[1:]):
        gap = dists[b] - dists[a]
        if b - a > 1 and gap <= ROAD_GAP_MAX_M:
            for i in range(a + 1, b):
                t = (dists[i] - dists[a]) / gap
                put(i, smooth[a][4] + t * (smooth[b][4] - smooth[a][4]), smooth[a][5] + t * (smooth[b][5] - smooth[a][5]))
    first, last = valid[0], valid[-1]
    if start_junction and first > 0 and dists[first] <= ROAD_GAP_MAX_M:
        for i in range(first):
            put(i, smooth[first][4], smooth[first][5])
    if end_junction and last < len(smooth) - 1 and dists[-1] - dists[last] <= ROAD_GAP_MAX_M:
        for i in range(last + 1, len(smooth)):
            put(i, smooth[last][4], smooth[last][5])


def junction_nodes(lines: list) -> list[tuple[float, float]]:
    """Uzly os stupně ≥ 3 (i „T“ na průběžné ose a křížení) z naodkovaných os, sloučené do 2 m."""
    from collections import Counter

    from shapely.ops import unary_union

    noded = unary_union(lines)
    deg: Counter = Counter()
    for seg in getattr(noded, "geoms", [noded]):
        if seg.geom_type != "LineString" or seg.is_empty:
            continue
        c = seg.coords
        deg[(round(c[0][0], 2), round(c[0][1], 2))] += 1
        deg[(round(c[-1][0], 2), round(c[-1][1], 2))] += 1
    nodes: list[tuple[float, float]] = []
    for pt, k in deg.items():
        if k >= 3 and all(math.dist(pt, q) > 2.0 for q in nodes):
            nodes.append(pt)
    return nodes


def _closed_by_curbs(face, disk, arms: list) -> bool:
    """Stěna je uzavřená okraji vozovek: každý její oblouk na hranici kruhu je ústí ulice – protne ho osa ramene
    a není delší než šířka ramene (+ rezerva na šikmý průchod a zaoblení obrubníku)."""
    from shapely.ops import linemerge

    arcs = face.boundary.intersection(disk.exterior.buffer(0.01))
    pieces = [g for g in getattr(arcs, "geoms", [arcs]) if g.geom_type in ("LineString", "MultiLineString") and g.length > 0.05]
    if not pieces:
        return True
    merged = linemerge(pieces) if len(pieces) > 1 else pieces[0]
    for arc in getattr(merged, "geoms", [merged]):
        widths = [w for ln, w in arms if arc.buffer(0.05).intersects(ln)]
        if not widths or arc.length > 1.5 * max(widths) + 2 * JUNCTION_SIDE_M:
            return False
    return True


def junction_polygons(lines: list, edge_lines: list, tree, widths: list[tuple[int, float, float, float]]) -> list:
    """Plochy středů křižovatek: stěna okrajů vozovek obsahující uzel os v kruhu kolem uzlu, omezená pásem os
    vstupujících do uzlu (každá podle své změřené šířky; osa bez změřené vozovky pás nedává)."""
    from shapely.geometry import Point
    from shapely.ops import polygonize, unary_union
    from shapely.strtree import STRtree

    if not lines or not widths:
        return []
    width_pts = [Point(x, y) for _, x, y, _ in widths]
    wtree = STRtree(width_pts)
    ltree = STRtree(lines)
    out = []
    for x, y in junction_nodes(lines):
        node = Point(x, y)
        near = node.buffer(JUNCTION_NEAR_M)
        per_line: dict[int, list[float]] = {}
        for k in wtree.query(near):
            li, wx, wy, w = widths[int(k)]
            if math.dist((wx, wy), (x, y)) <= JUNCTION_NEAR_M:
                per_line.setdefault(li, []).append(w)
        arms = [
            (lines[int(li)], median(per_line[int(li)])) for li in ltree.query(node.buffer(0.5))
            if int(li) in per_line and lines[int(li)].distance(node) <= 0.5
        ]
        if not arms:
            continue  # uzel bez změřené vozovky (pěší / bez okrajů)
        r = min(JUNCTION_MAX_R_M, max(w for _, w in arms) / 2.0 + JUNCTION_EXTRA_M)
        disk = node.buffer(r, 32)
        cut = [edge_lines[int(k)].intersection(disk) for k in tree.query(disk)]
        cut = [g for g in cut if not g.is_empty]
        faces = [f for f in polygonize(unary_union(cut + [disk.exterior])) if f.contains(node)]
        if faces and _closed_by_curbs(faces[0], disk, arms):
            band = unary_union([ln.buffer(w / 2.0 + JUNCTION_SIDE_M, cap_style="flat") for ln, w in arms])
            face = faces[0].intersection(band).intersection(disk)
        else:
            # okraje vozovky v kruhu nejsou souvislé (stěna by vytekla mimo ulici): jen pásy os podle šířky
            face = unary_union([ln.buffer(w / 2.0) for ln, w in arms]).intersection(disk)
            if faces:
                face = face.intersection(faces[0])
        if not face.is_empty and face.area >= 1.0:
            out.append(face)
    return out


def _shapely_to_rings(geom) -> list:
    return [
        [[(round(x, 2), round(y, 2)) for x, y in g.exterior.coords]]
        + [[(round(x, 2), round(y, 2)) for x, y in r.coords] for r in g.interiors]
        for g in getattr(geom, "geoms", [geom])
        if not g.is_empty
    ]


def classify(data: dict[str, list[dict]]) -> dict[str, list]:
    """Objekty roztříděné do souborů DTM (``buildings``, ``roads``, ``parking``, ``fences``)."""
    keys = (
        "buildings", "roads", "sidewalks", "stairs", "parking", "small_paved", "green", "other_cover",
        "garden_cover", "yards", "decks", "fences", "public",
    )
    out: dict[str, list] = {k: [] for k in keys}
    for f in data.get("bygning") or []:
        pr = _props(f)
        if pr.get("status") in BUILDING_STATES and pr.get("synligBygning") is not False:
            out["buildings"].extend(feature_polygons(f))
    out["buildings"] = clean_polygons(out["buildings"])
    for f in data.get("hegn") or []:
        for line in feature_lines(f):
            out["fences"].append(
                dtm_fences.Fence(coords=tuple(line), druh="", level=0, boundary=False, zpusob="1",
                                 oid=str(_props(f).get("id_lokalId") or ""))
            )
    for f in data.get("parkering") or []:
        for line in feature_lines(f):
            if len(line) >= 4 and math.dist(line[0], line[-1]) < 0.05:
                out["parking"].append([list(line)])
    out["parking"] = clean_polygons(out["parking"])
    axes = [
        line for f in data.get("vejmidte") or []
        if _props(f).get("vejmidtetype") == "Vej" and _props(f).get("trafikart") not in ("Sti", "Cykelsti")
        for line in feature_lines(f)
    ]
    edges = [line for f in data.get("vejkant") or [] if _props(f).get("type") in ROAD_EDGE_TYPES for line in feature_lines(f)]
    roads = []
    for geom in road_polygons(axes, edges):
        roads.extend(_shapely_to_rings(geom))
    out["roads"] = clean_polygons(roads)
    out["public"] = list(out["roads"]) + list(out["parking"])
    return out


LOG_LABELS = {
    "buildings": "budovy (bygning)",
    "roads": "vozovky (z okrajů vejkant)",
    "parking": "parkoviště (parkering)",
}


def prepare(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log: LogFn | None = None,
    force: bool = False,  # noqa: ARG001 – stejný podpis jako příprava DTM (data se necachují mezi joby)
) -> Path | None:
    """GeoDanmark pro výřez → soubory ``work_dir/dtm``; None, když služba nic nevrátí."""
    from pyproj import Transformer

    from app.pipeline.crs_5514 import CRS_LABEL
    from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514

    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    fetch_box = Transformer.from_crs(CRS_LABEL, SOURCE_CRS, always_xy=True).transform_bounds(*bounds, densify_pts=21)
    t0 = time.monotonic()
    build_stats.note_background("dtm", source="dk_geodanmark")
    data, nbytes = fetch_layers(fetch_box)
    reproject_features(data, SOURCE_CRS, CRS_LABEL)
    build_stats.note_bytes("dk_geodanmark", nbytes, cached=False, key="geodanmark_dk")
    count = sum(len(v) for v in data.values())
    build_stats.note_background("dtm", packages=len(LAYERS), regions=["DK"], source="dk_geodanmark", objects=count)
    if log:
        log(f"GeoDanmark: {count} objektů z {len(LAYERS)} vrstev ({nbytes / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s)")
    if not count:
        if log:
            log("GeoDanmark: ve výřezu nic, kresba z OSM.")
        return None
    sorted_data = classify(data)
    return write_dtm_files(
        work_dir, sorted_data, bounds, log=log, source="dk_geodanmark", label="GeoDanmark", labels=LOG_LABELS,
    )
