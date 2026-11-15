"""FMZK (Flächen-Mehrzweckkarte, Vídeň) jako obdoba české DTM pro kresbu.

Rakousko má celostátně jen výšky (BEV ALS DTM / DSM 1 m); velkoměřítkovou mapu s vozovkami a chodníky má otevřeně
jen Vídeň (Petr 2026-11-14: Rakousko jen Vídeň). FMZK je z WFS Stadt Wien (``FMZK_WFS``, Open Government Data,
CC BY 4.0 „Datenquelle: Stadt Wien – data.wien.gv.at“, bez klíče): vozovky (Fahrbahn), chodníky (Gehsteig),
pěší zóny, parkování (Ruhender Verkehr), schody (Stiege), vnitroblokové dvory (Innenhof), zeleň, les, budovy.
Ploty FMZK vede jen jako plochy zdí (Mauer, Sockelzaun) – kreslí je OSM.

Výsledek se zapíše do ``work_dir/dtm`` stejně jako BGT a AV (``bgt_nl.node_coverage``, ``bgt_nl.write_dtm_files``).

Souřadnice: služba převádí do jiných systémů bez posunu data MGI (v EPSG:25833 o ~290 m vedle, ověřeno na Stephansdomu),
proto se stahuje v nativním MGI / Gauss-Krüger M34 (``SOURCE_CRS``) a převádí se tady přes PROJ (``reproject_features``).
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable

from app.pipeline import build_stats
from app.pipeline.bgt_nl import Polygon, clean_polygons, feature_polygons, node_coverage, tile_bounds, write_dtm_files

FMZK_WFS = "https://data.wien.gv.at/daten/geo"
SOURCE_CRS = "EPSG:31256"  # MGI / Austria GK East – nativní systém FMZK
SOURCE_LABEL = "FMZK (Stadt Wien)"
LAYERS = ("FMZKVERKEHR1OGD", "FMZKVERKEHR2OGD", "FMZKGEBOGD", "FMZKGRUEN1OGD", "FMZKGRUEN2OGD")
TILE_M = 1000.0
WORKERS = 4
HTTP_TIMEOUT_S = 120
HTTP_RETRIES = 2

# F_KLASSE → soubor DTM (CZ obdoba).
ROAD_CLASSES = frozenset({21})  # Fahrbahn
PARKING_CLASSES = frozenset({33})  # Ruhender Verkehr
SIDEWALK_CLASSES = frozenset({23, 24, 25})  # Gehsteig, Fußgängerzone, Gehweg/Radweg
STAIRS_CLASSES = frozenset({74})  # Stiege
SMALL_PAVED_CLASSES = frozenset({26, 59})  # Private Verkehrsfläche, Befestigte Fläche
YARD_CLASSES = frozenset({51})  # Innenhof → oliva (sprint dvůr bez cesty zaleje budovou)
BUILDING_CLASSES = frozenset({11})  # Gebäude (Überbauung, Flugdach ne)
GREEN_CLASSES = frozenset({58, 61, 69})  # Grünfläche, Sportfeld, Sonstige Grünfläche (zahrady → ``private_greens``)
OTHER_COVER_CLASSES = frozenset({52, 53, 54})  # Wald, Wiese, Feld/Acker
PUBLIC_CLASSES = ROAD_CLASSES | PARKING_CLASSES | SIDEWALK_CLASSES

# Grünfläche je soukromá zahrada (oliva), když se dotýká budovy, nedotýká se ulice ani chodníku (zadní zahrady
# uvnitř bloku) a na sousední budovu připadá nejvýš ``GARDEN_PER_BUILDING_M2``. Předzahrádky u chodníku zůstanou
# zelení (FMZK soukromé a veřejné nerozlišuje; raději méně olivy).
GARDEN_TOUCH_M = 0.5
GARDEN_PER_BUILDING_M2 = 2000.0

LogFn = Callable[[str], None]


def items_url(layer: str, bounds: tuple[float, float, float, float], crs: str) -> str:
    query = {
        "service": "WFS",
        "version": "1.1.0",
        "request": "GetFeature",
        "typeName": f"ogdwien:{layer}",
        "srsName": crs,
        "outputFormat": "json",
        "bbox": ",".join(f"{v:.1f}" for v in bounds) + f",{crs}",
    }
    return f"{FMZK_WFS}?{urllib.parse.urlencode(query)}"


def _get_json(url: str) -> tuple[dict, int]:
    from app.pipeline.fetch_openzu import USER_AGENT

    last: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read()
            return json.loads(raw), len(raw)
        except Exception as exc:  # síť / 5xx: krátce počkat a zkusit znovu
            last = exc
            if attempt < HTTP_RETRIES:
                time.sleep(2.0 * (attempt + 1))
    assert last is not None
    raise last


def fetch_layers(
    bounds: tuple[float, float, float, float],
    crs: str,
    layers: Iterable[str] = LAYERS,
    *,
    tile_m: float = TILE_M,
    workers: int = WORKERS,
    fetch: Callable[[str], tuple[dict, int]] = _get_json,
) -> tuple[dict[str, list[dict]], int]:
    """``{vrstva: [objekt, …]}`` pro výřez (dlaždice souběžně, objekty sloučené podle ``FMZK_ID``)."""
    layers = tuple(layers)
    jobs = [(layer, t) for layer in layers for t in tile_bounds(bounds, tile_m)]
    out: dict[str, dict[str, dict]] = {layer: {} for layer in layers}
    total = 0
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="fmzk") as ex:
        results = ex.map(lambda j: fetch(items_url(j[0], j[1], crs)), jobs)
        for (layer, tile), (data, nbytes) in zip(jobs, results):
            total += nbytes
            for i, f in enumerate(data.get("features") or []):
                props = f.get("properties") or {}
                key = str(props.get("FMZK_ID") or f.get("id") or f"{tile}:{i}")
                out[layer].setdefault(key, f)
    return {layer: list(v.values()) for layer, v in out.items()}, total


def _transformer(src: str, dst: str):
    from pyproj import Transformer

    return Transformer.from_crs(src, dst, always_xy=True)  # PROJ vybere nejpřesnější dostupný převod MGI → ETRS89


def source_bounds(bounds: tuple[float, float, float, float], crs: str, margin_m: float = 20.0) -> tuple[float, float, float, float]:
    """Výřez v souřadnicích jobu → obálka v ``SOURCE_CRS`` (s rezervou na stočení os)."""
    xmin, ymin, xmax, ymax = _transformer(crs, SOURCE_CRS).transform_bounds(*bounds, densify_pts=21)
    return (xmin - margin_m, ymin - margin_m, xmax + margin_m, ymax + margin_m)


def reproject_features(data: dict[str, list[dict]], src: str, dst: str) -> dict[str, list[dict]]:
    """Plochy a čáry z ``src`` do ``dst`` (geometrie objektů se přepíše na místě)."""
    if src == dst:
        return data
    tr = _transformer(src, dst)

    def ring(coords):
        if not coords:
            return coords
        xs, ys = tr.transform([float(p[0]) for p in coords], [float(p[1]) for p in coords])
        return [[x, y] for x, y in zip(xs, ys)]

    for feats in data.values():
        for f in feats:
            geom = f.get("geometry") or {}
            kind, coords = geom.get("type"), geom.get("coordinates") or []
            if kind == "Polygon" or kind == "MultiLineString":
                geom["coordinates"] = [ring(r) for r in coords]
            elif kind == "MultiPolygon":
                geom["coordinates"] = [[ring(r) for r in part] for part in coords]
            elif kind == "LineString":
                geom["coordinates"] = ring(coords)
    return data


def private_greens(
    greens: list[Polygon], buildings: list[Polygon], public: list[Polygon]
) -> tuple[list[Polygon], list[Polygon]]:
    """(zadní zahrady u domů → oliva, ostatní zeleň)."""
    import shapely
    from shapely.geometry import Polygon as ShPolygon

    def shapes(polys):
        out = []
        for p in polys:
            try:
                g = ShPolygon(p[0], p[1:])
            except (ValueError, IndexError):
                continue
            if g.is_valid and not g.is_empty:
                out.append(g)
        return out

    build, pub = shapes(buildings), shapes(public)
    btree = shapely.STRtree(build) if build else None
    ptree = shapely.STRtree(pub) if pub else None
    yards: list[Polygon] = []
    green: list[Polygon] = []
    for poly in greens:
        g = shapes([poly])
        if not g:
            continue
        zone = g[0].buffer(GARDEN_TOUCH_M)
        n = len(btree.query(zone, predicate="intersects")) if btree is not None else 0
        street = bool(len(ptree.query(zone, predicate="intersects"))) if ptree is not None else False
        (yards if n and not street and g[0].area / n <= GARDEN_PER_BUILDING_M2 else green).append(poly)
    return yards, green


def classify(data: dict[str, list[dict]]) -> dict[str, list]:
    """Objekty FMZK roztříděné do souborů DTM (``buildings``, ``roads``, …) + ``yards``, ``public``."""
    keys = (
        "buildings", "roads", "sidewalks", "stairs", "parking", "small_paved", "green", "other_cover",
        "garden_cover", "yards", "decks", "fences", "public",
    )
    out: dict[str, list] = {k: [] for k in keys}
    greens: list[Polygon] = []
    for layer in LAYERS:
        for f in data.get(layer) or []:
            try:
                cls = int((f.get("properties") or {}).get("F_KLASSE") or 0)
            except (TypeError, ValueError):
                continue
            polys = clean_polygons(feature_polygons(f))
            if cls in PUBLIC_CLASSES:
                out["public"].extend(polys)
            if cls in BUILDING_CLASSES:
                out["buildings"].extend(polys)
            elif cls in ROAD_CLASSES:
                out["roads"].extend(polys)
            elif cls in PARKING_CLASSES:
                out["parking"].extend(polys)
            elif cls in SIDEWALK_CLASSES:
                out["sidewalks"].extend(polys)
            elif cls in STAIRS_CLASSES:
                out["stairs"].extend(polys)
            elif cls in SMALL_PAVED_CLASSES:
                out["small_paved"].extend(polys)
            elif cls in YARD_CLASSES:
                out["yards"].extend(polys)
            elif cls in GREEN_CLASSES:
                greens.extend(polys)
            elif cls in OTHER_COVER_CLASSES:
                out["other_cover"].extend(polys)
    yards, green = private_greens(greens, out["buildings"], out["public"])
    out["yards"].extend(yards)
    out["green"].extend(green)
    return out


LOG_LABELS = {
    "buildings": "budovy (Gebäude)",
    "roads": "vozovky (Fahrbahn)",
    "sidewalks": "chodníky a pěší zóny (Gehsteig)",
    "stairs": "schody (Stiege)",
    "parking": "parkování (Ruhender Verkehr)",
    "small_paved": "ostatní zpevněné plochy",
    "green": "zeleň",
    "other_cover": "jiný kryt (les, louka, pole)",
}


def prepare(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log: LogFn | None = None,
    force: bool = False,  # noqa: ARG001 – stejný podpis jako příprava DTM (FMZK se necachuje mezi joby)
) -> Path | None:
    """FMZK pro výřez → soubory ``work_dir/dtm`` (jako DTM); None mimo Vídeň (služba nic nevrátí)."""
    from app.pipeline.crs_5514 import CRS_LABEL
    from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514

    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    t0 = time.monotonic()
    build_stats.note_background("dtm", source="at_fmzk")
    src_bounds = source_bounds(bounds, CRS_LABEL)
    data, nbytes = fetch_layers(src_bounds, SOURCE_CRS)
    reproject_features(data, SOURCE_CRS, CRS_LABEL)
    build_stats.note_bytes("at_fmzk", nbytes, cached=False, key="fmzk")
    count = sum(len(v) for v in data.values())
    tiles = len(tile_bounds(src_bounds, TILE_M))
    build_stats.note_background("dtm", packages=tiles, regions=["AT-9"], source="at_fmzk", objects=count)
    if log:
        log(f"FMZK: {count} objektů z {tiles} dlaždic ({nbytes / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s)")
    if not count:
        if log:
            log("FMZK: výřez mimo Vídeň, kresba z OSM.")
        return None
    sorted_data = classify(data)
    try:
        sorted_data = node_coverage(sorted_data)
    except Exception as exc:  # bez uzlování se kreslí z původních ploch
        if log:
            log(f"FMZK: uzlování ploch přeskočeno ({type(exc).__name__}: {exc})")
    return write_dtm_files(
        work_dir, sorted_data, bounds, log=log, source="at_fmzk", label="FMZK", labels=LOG_LABELS,
        yard_label="dvory a zadní zahrady",
    )
