"""Dánsko: GeoDanmark (Klimadatastyrelsen + obce) jako obdoba české DTM pro kresbu.

GeoDanmark je celostátní topografická databáze ve velkém měřítku (obce ji udržují, přesnost ~0,1 m). Do kresby se bere:

- ``bygning`` → budovy (stav „Anlagt“ / „Under nedlæggelse“, viditelné),
- ``hegn`` → ploty,
- ``parkering`` → parkoviště (obrys je uzavřená čára),
- vozovky: GeoDanmark nemá plochy silnic, jen okraje (``vejkant``) a osy (``vejmidte``). Vozovka vznikne z kolmých
  řezů po ``ROAD_STEP_M`` podél osy silnice k nejbližšímu okraji vozovky po každé straně (medián v okně, kusy bez
  okraje na obou stranách vynechá) – tam ji pak kreslí plochou / podle šířky jako české DTM. Chodníky a stezky
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
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union
    from shapely.strtree import STRtree

    edge_lines = [LineString(e) for e in edges if len(e) >= 2]
    if not edge_lines:
        return []
    tree = STRtree(edge_lines)
    out = []
    for axis in axes:
        if len(axis) < 2:
            continue
        line = LineString(axis)
        length = line.length
        n = int(length // ROAD_STEP_M)
        if n < ROAD_MIN_RUN:
            continue
        samples = []  # (x, y, nx, ny, levá, pravá) nebo None
        for i in range(n + 1):
            d = min(length, i * ROAD_STEP_M)
            p = line.interpolate(d)
            a, b = line.interpolate(max(0.0, d - 0.5)), line.interpolate(min(length, d + 0.5))
            dx, dy = b.x - a.x, b.y - a.y
            norm = math.hypot(dx, dy)
            if norm == 0:
                samples.append(None)
                continue
            nx, ny = -dy / norm, dx / norm
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
        for i, sm in enumerate(samples):
            if sm is None:
                smooth.append(None)
                continue
            win = [samples[j] for j in range(max(0, i - half), min(len(samples), i + half + 1)) if samples[j]]
            smooth.append((sm[0], sm[1], sm[2], sm[3], median(w[4] for w in win), median(w[5] for w in win)))
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
    if not out:
        return []
    merged = unary_union(out).buffer(0.1, join_style=2).buffer(-0.1, join_style=2)
    return [g for g in getattr(merged, "geoms", [merged]) if g.area >= 10.0]


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
