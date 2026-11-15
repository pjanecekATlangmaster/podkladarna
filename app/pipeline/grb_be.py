"""Flandry: GRB (Grootschalig Referentiebestand, Digitaal Vlaanderen) jako obdoba české DTM pro kresbu.

GRB je velkoměřítková mapa Flander (podrobnost ~1 : 500 – 1 : 1 000) z WFS geo.api.vlaanderen.be bez klíče,
EPSG:31370 (Belgian Lambert 72), licence Modellicentie Gratis Hergebruik (uvést „Digitaal Vlaanderen“).

- budovy: ``GBG`` (hlavní budovy, přístavky, budovy s virtuální fasádou),
- uliční prostor: ``WBN`` (wegbaan, od fasády k fasádě) – sprint ho kreslí jako ulici v celé šířce stejně jako české
  parcely komunikací (``street_parcels``),
- vozovka a chodníky: ``WBN`` rozdělený čarami ``WGO`` (okraj vozovky, hranice zóny chodců / cyklistů, hranice
  nezpevněné zóny). Stěna, kterou prochází osa silnice (``Wegsegment``), je vozovka; ostatní jsou chodníky, stěny
  ohraničené převážně hranicí nezpevněné zóny zeleň.

Plochy chodníků a vozovek pak jdou do ``work_dir/dtm`` stejně jako BGT, AV, FMZK, Berlín, Plan de voirie a ETAK.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from app.pipeline import build_stats
from app.pipeline.bgt_nl import clean_polygons, feature_lines, feature_polygons, node_coverage, write_dtm_files
from app.pipeline.etak_ee import write_streets
from app.pipeline.fmzk_at import reproject_features

GRB_WFS = "https://geo.api.vlaanderen.be/GRB/wfs"
SOURCE_CRS = "EPSG:31370"
WORKERS = 4
PAGE_SIZE = 10000
MAX_PAGES = 30
HTTP_TIMEOUT_S = 180
HTTP_RETRIES = 3
MAX_RESPONSE_BYTES = 300_000_000
LAYERS = ("GBG", "WBN", "WGO", "Wegsegment")
WGO_UNPAVED = 3  # grens onverharde zone
AXIS_MIN_M = 2.0  # osa silnice ve stěně aspoň tak dlouhá → vozovka
UNPAVED_SHARE = 0.5

LogFn = Callable[[str], None]


def wfs_url(layer: str, bounds: tuple[float, float, float, float], start: int = 0) -> str:
    xmin, ymin, xmax, ymax = bounds
    query = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": f"GRB:{layer}",
        "outputFormat": "application/json",
        "srsName": SOURCE_CRS,
        "bbox": f"{xmin:.1f},{ymin:.1f},{xmax:.1f},{ymax:.1f},{SOURCE_CRS}",
        "count": str(PAGE_SIZE),
        "startIndex": str(start),
    }
    return f"{GRB_WFS}?{urllib.parse.urlencode(query)}"


def _get_json(url: str) -> tuple[dict, int]:
    from app.pipeline.fetch_openzu import USER_AGENT

    last: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"GRB: odpověď větší než {MAX_RESPONSE_BYTES // 1_000_000} MB")
            return json.loads(raw), len(raw)
        except Exception as exc:  # síť / 5xx: krátce počkat a zkusit znovu
            last = exc
            if attempt < HTTP_RETRIES:
                time.sleep(2.0 * (attempt + 1))
    assert last is not None
    raise last


def _fetch_layer(layer: str, bounds, fetch) -> tuple[list[dict], int]:
    feats: list[dict] = []
    nbytes = 0
    for page in range(MAX_PAGES):
        data, n = fetch(wfs_url(layer, bounds, page * PAGE_SIZE))
        nbytes += n
        feats.extend(f for f in (data.get("features") or []) if f.get("geometry"))
        if len(data.get("features") or []) < PAGE_SIZE:
            break
    return feats, nbytes


def fetch_layers(
    bounds: tuple[float, float, float, float],
    layers=LAYERS,
    *,
    workers: int = WORKERS,
    fetch: Callable[[str], tuple[dict, int]] = _get_json,
) -> tuple[dict[str, list[dict]], int]:
    """``{vrstva: [objekt, …]}`` pro obálku výřezu v EPSG:31370 (vrstvy souběžně, stránkování po ``PAGE_SIZE``)."""
    layers = tuple(layers)
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="grb-be") as ex:
        results = list(ex.map(lambda layer: _fetch_layer(layer, bounds, fetch), layers))
    return {layer: feats for layer, (feats, _n) in zip(layers, results)}, sum(n for _f, n in results)


def _to_rings(geom) -> list:
    """Shapely plocha → seznam ploch ve tvaru DTM (seznam kruhů souřadnic)."""
    parts = [geom] if geom.geom_type == "Polygon" else list(getattr(geom, "geoms", []))
    return [
        [list(map(tuple, p.exterior.coords)), *[list(map(tuple, h.coords)) for h in p.interiors]]
        for p in parts
        if p.geom_type == "Polygon" and not p.is_empty
    ]


def split_street_space(street_polys: list, kerbs: list[tuple[list, int]], axes: list[list]) -> dict[str, list]:
    """Uliční prostor rozdělený čarami WGO na vozovku (``roads``), chodníky (``sidewalks``) a zeleň (``green``)."""
    import shapely
    from shapely.geometry import LineString, Polygon
    from shapely.ops import polygonize, unary_union

    out: dict[str, list] = {"roads": [], "sidewalks": [], "green": []}
    shapes = []
    for poly in street_polys:
        g = Polygon(poly[0], poly[1:])
        shapes.append(g if g.is_valid else shapely.make_valid(g))
    if not shapes:
        return out
    space = unary_union(shapes)
    near = space.buffer(0.3)
    lines = [(LineString(c), t) for c, t in kerbs if len(c) >= 2]
    edges = unary_union([space.boundary, *[ln.intersection(near) for ln, _t in lines]])
    axis = unary_union([LineString(c) for c in axes if len(c) >= 2]) if axes else None
    unpaved = unary_union([ln for ln, t in lines if t == WGO_UNPAVED]).buffer(0.2) if lines else None
    tree = shapely.STRtree(shapes)
    for face in polygonize(edges):
        if face.area < 0.5 or not any(shapes[i].contains(face.representative_point()) for i in tree.query(face)):
            continue
        if axis is not None and axis.intersection(face).length >= AXIS_MIN_M:
            key = "roads"
        elif unpaved is not None and face.boundary.intersection(unpaved).length > UNPAVED_SHARE * face.boundary.length:
            key = "green"
        else:
            key = "sidewalks"
        out[key].append(face)
    return {k: [r for g in v for r in _to_rings(g)] for k, v in out.items()}


def classify(data: dict[str, list[dict]]) -> dict[str, list]:
    """Objekty GRB roztříděné do souborů DTM + ``streets`` (uliční prostor pro sprint) a ``public``."""
    keys = (
        "buildings", "roads", "sidewalks", "stairs", "parking", "small_paved", "green", "other_cover",
        "garden_cover", "yards", "decks", "fences", "public", "streets",
    )
    out: dict[str, list] = {k: [] for k in keys}
    for f in data.get("GBG") or []:
        out["buildings"].extend(clean_polygons(feature_polygons(f)))
    streets = clean_polygons([p for f in data.get("WBN") or [] for p in feature_polygons(f)])
    out["streets"] = streets
    kerbs = []
    for f in data.get("WGO") or []:
        t = int((f.get("properties") or {}).get("TYPE") or 0)
        kerbs.extend((line, t) for line in feature_lines(f))
    axes = [line for f in data.get("Wegsegment") or [] for line in feature_lines(f)]
    parts = split_street_space(streets, kerbs, axes)
    for key in ("roads", "sidewalks", "green"):
        polys = clean_polygons(parts[key])
        out[key].extend(polys)
        if key != "green":
            out["public"].extend(polys)
    return out


LOG_LABELS = {
    "buildings": "budovy (gebouw)",
    "roads": "vozovky (wegbaan s osou silnice)",
    "sidewalks": "chodníky a pěší plochy (wegbaan bez osy)",
    "green": "nezpevněné pásy v ulici",
}


def prepare(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log: LogFn | None = None,
    force: bool = False,  # noqa: ARG001 – stejný podpis jako příprava DTM (data se necachují mezi joby)
) -> Path | None:
    """GRB pro výřez → soubory ``work_dir/dtm`` a uliční prostor pro sprint; None, když služba nic nevrátí."""
    from pyproj import Transformer

    from app.pipeline.crs_5514 import CRS_LABEL
    from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514

    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    fetch_box = Transformer.from_crs(CRS_LABEL, SOURCE_CRS, always_xy=True).transform_bounds(*bounds, densify_pts=21)
    t0 = time.monotonic()
    build_stats.note_background("dtm", source="be_grb")
    data, nbytes = fetch_layers(fetch_box)
    reproject_features(data, SOURCE_CRS, CRS_LABEL)
    build_stats.note_bytes("be_grb", nbytes, cached=False, key="grb_be")
    count = sum(len(v) for v in data.values())
    build_stats.note_background("dtm", packages=len(LAYERS), regions=["BE"], source="be_grb", objects=count)
    if log:
        log(f"GRB: {count} objektů z {len(LAYERS)} vrstev ({nbytes / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s)")
    if not data.get("GBG") and not data.get("WBN"):
        if log:
            log("GRB: ve výřezu nic (mimo Flandry), kresba z OSM.")
        return None
    sorted_data = classify(data)
    streets = sorted_data.pop("streets")
    if write_streets(work_dir, streets) is not None and log:
        log(f"GRB uliční prostor (wegbaan): {len(streets)} ploch – sprint ho kreslí jako ulici v celé šířce")
    try:
        sorted_data = node_coverage(sorted_data)
    except Exception as exc:  # bez uzlování se kreslí z původních ploch
        if log:
            log(f"GRB: uzlování ploch přeskočeno ({type(exc).__name__}: {exc})")
    return write_dtm_files(
        work_dir, sorted_data, bounds, log=log, source="be_grb", label="GRB", labels=LOG_LABELS,
        yard_label="soukromé pozemky (GRB je nevede)",
    )
