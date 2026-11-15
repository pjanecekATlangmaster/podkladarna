"""Berlín: Straßenbefahrung a budovy ALKIS jako obdoba české DTM pro kresbu.

Německo nemá celostátní velkoměřítkovou mapu s chodníky; ALKIS ostatních zemí má jen budovy a hrubé plochy.
Otevřeně ji má Berlín (Senatsverwaltung, GDI Berlin, Datenlizenz Deutschland – Zero 2.0, bez klíče):
- Straßenbefahrung (WFS ``strassenbefahrung``): vozovky (Fahrbahn), chodníky (Gehweg), pěší zóny a náměstí,
  cyklostezky, parkovací pruhy, schody, zeleň v uličním prostoru. Zaměřeno 2014, ulice se od té doby místy změnily.
- ALKIS budovy (WFS ``alkis_gebaeude``): obrysy budov z katastru (aktuální).
Obojí je v EPSG:25833 (souřadnice jobu Německa – Berlína), převod souřadnic proto není potřeba; kdyby se CRS jobu
lišil, převede se přes PROJ (``fmzk_at.reproject_features``, server sám převádí nespolehlivě – poučení z Vídně).

Výsledek se zapíše do ``work_dir/dtm`` stejně jako BGT, AV a FMZK (``bgt_nl.node_coverage``, ``bgt_nl.write_dtm_files``).
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
from app.pipeline.bgt_nl import clean_polygons, feature_polygons, node_coverage, tile_bounds, write_dtm_files
from app.pipeline.fmzk_at import reproject_features

SB_WFS = "https://gdi.berlin.de/services/wfs/strassenbefahrung"
ALKIS_WFS = "https://gdi.berlin.de/services/wfs/alkis_gebaeude"
SOURCE_CRS = "EPSG:25833"
SOURCE_LABEL = "Straßenbefahrung a ALKIS (GDI Berlin)"
TILE_M = 500.0
WORKERS = 4
PAGE_SIZE = 5000
MAX_PAGES = 20
HTTP_TIMEOUT_S = 120
HTTP_RETRIES = 2
MAX_RESPONSE_BYTES = 300_000_000

# vrstva → soubor DTM (CZ obdoba). Trennstreifen, kolejový svršek tramvaje, rampy a staveniště ne (kreslí OSM).
SB_LAYERS: dict[str, str] = {
    "cm_fahrbahn": "roads",
    "ca_haltebereich_bus": "roads",
    "ck_parkflaeche": "parking",
    "cl_gehweg": "sidewalks",
    "cj_fussgaengerzone": "sidewalks",
    "ci_oeffentlicher_platz": "sidewalks",
    "cb_haltestellenwartebereich": "sidewalks",
    "ch_radweg": "small_paved",  # cyklostezka: zpevněná plocha, ne chodník (jako BGT fietspad)
    "cc_treppe": "stairs",
    "ce_gruenflaeche": "green",
}
PUBLIC_KEYS = frozenset({"roads", "parking", "sidewalks", "small_paved"})
ALKIS_LAYER = "gebaeude"
BUILDING_KIND = "AX_Gebaeude"  # AX_Bauteil (části budov) ne
UNDERGROUND_OFL = frozenset({1200})  # Lage zur Erdoberfläche: unter der Erdoberfläche

LogFn = Callable[[str], None]


def items_url(service: str, layer: str, bounds: tuple[float, float, float, float], start: int = 0) -> str:
    prefix = "strassenbefahrung" if service == SB_WFS else "alkis_gebaeude"
    query = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": f"{prefix}:{layer}",
        "SRSNAME": f"urn:ogc:def:crs:EPSG::{SOURCE_CRS.split(':')[1]}",
        "BBOX": ",".join(f"{v:.1f}" for v in bounds) + f",urn:ogc:def:crs:EPSG::{SOURCE_CRS.split(':')[1]}",
        "OUTPUTFORMAT": "application/json",
        "COUNT": str(PAGE_SIZE),
        "STARTINDEX": str(start),
    }
    return f"{service}?{urllib.parse.urlencode(query)}"


def _get_json(url: str) -> tuple[dict, int]:
    from app.pipeline.fetch_openzu import USER_AGENT

    last: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"GDI Berlin: odpověď větší než {MAX_RESPONSE_BYTES // 1_000_000} MB")
            return json.loads(raw), len(raw)
        except Exception as exc:  # síť / 5xx: krátce počkat a zkusit znovu
            last = exc
            if attempt < HTTP_RETRIES:
                time.sleep(2.0 * (attempt + 1))
    assert last is not None
    raise last


def fetch_tile(
    service: str, layer: str, bounds: tuple[float, float, float, float], fetch: Callable[[str], tuple[dict, int]] = _get_json
) -> tuple[list[dict], int]:
    """Objekty vrstvy v dlaždici po stránkách ``PAGE_SIZE`` (nejvýš ``MAX_PAGES``)."""
    feats: list[dict] = []
    total = 0
    for page in range(MAX_PAGES):
        data, nbytes = fetch(items_url(service, layer, bounds, page * PAGE_SIZE))
        total += nbytes
        got = list(data.get("features") or [])
        feats.extend(got)
        if len(got) < PAGE_SIZE:
            return feats, total
    raise RuntimeError(f"GDI Berlin: vrstva {layer} má v dlaždici víc než {MAX_PAGES} stránek")


def _key(feat: dict, fallback: str) -> str:
    props = feat.get("properties") or {}
    return str(feat.get("id") or props.get("gis_id") or props.get("uuid") or fallback)


def fetch_layers(
    bounds: tuple[float, float, float, float],
    layers: Iterable[tuple[str, str]],
    *,
    tile_m: float = TILE_M,
    workers: int = WORKERS,
    fetch: Callable[[str], tuple[dict, int]] = _get_json,
) -> tuple[dict[str, list[dict]], int]:
    """``{vrstva: [objekt, …]}`` pro výřez; ``layers`` = (služba, vrstva), dlaždice souběžně, objekty podle id."""
    layers = tuple(layers)
    jobs = [(service, layer, t) for service, layer in layers for t in tile_bounds(bounds, tile_m)]
    out: dict[str, dict[str, dict]] = {layer: {} for _s, layer in layers}
    total = 0
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="sb-berlin") as ex:
        results = ex.map(lambda j: fetch_tile(j[0], j[1], j[2], fetch), jobs)
        for (_service, layer, tile), (feats, nbytes) in zip(jobs, results):
            total += nbytes
            for i, f in enumerate(feats):
                out[layer].setdefault(_key(f, f"{tile}:{i}"), f)
    return {layer: list(v.values()) for layer, v in out.items()}, total


def _is_building(props: dict) -> bool:
    if str(props.get("bezeich") or "") != BUILDING_KIND:
        return False
    try:
        return int(props.get("ofl") or 0) not in UNDERGROUND_OFL
    except (TypeError, ValueError):
        return True


def classify(data: dict[str, list[dict]]) -> dict[str, list]:
    """Objekty roztříděné do souborů DTM (``buildings``, ``roads``, …) + ``public``."""
    keys = (
        "buildings", "roads", "sidewalks", "stairs", "parking", "small_paved", "green", "other_cover",
        "garden_cover", "yards", "decks", "fences", "public",
    )
    out: dict[str, list] = {k: [] for k in keys}
    for layer, target in SB_LAYERS.items():
        for f in data.get(layer) or []:
            polys = clean_polygons(feature_polygons(f))
            out[target].extend(polys)
            if target in PUBLIC_KEYS:
                out["public"].extend(polys)
    for f in data.get(ALKIS_LAYER) or []:
        if _is_building(f.get("properties") or {}):
            out["buildings"].extend(clean_polygons(feature_polygons(f)))
    return out


LOG_LABELS = {
    "buildings": "budovy (ALKIS)",
    "roads": "vozovky (Fahrbahn)",
    "sidewalks": "chodníky, pěší zóny a náměstí (Gehweg)",
    "stairs": "schody (Treppe)",
    "parking": "parkování (Parkfläche)",
    "small_paved": "cyklostezky (Radweg)",
    "green": "zeleň v ulicích",
    "other_cover": "jiný kryt",
}


def _source_bounds(bounds: tuple[float, float, float, float], crs: str) -> tuple[float, float, float, float]:
    if crs == SOURCE_CRS:
        return bounds
    from pyproj import Transformer

    xmin, ymin, xmax, ymax = Transformer.from_crs(crs, SOURCE_CRS, always_xy=True).transform_bounds(*bounds, densify_pts=21)
    return (xmin - 20.0, ymin - 20.0, xmax + 20.0, ymax + 20.0)


def prepare(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log: LogFn | None = None,
    force: bool = False,  # noqa: ARG001 – stejný podpis jako příprava DTM (data se necachují mezi joby)
) -> Path | None:
    """Straßenbefahrung + ALKIS budovy pro výřez → soubory ``work_dir/dtm``; None mimo Berlín."""
    from app.pipeline.crs_5514 import CRS_LABEL
    from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514

    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    src_bounds = _source_bounds(bounds, CRS_LABEL)
    t0 = time.monotonic()
    build_stats.note_background("dtm", source="de_sb")
    layers = [(SB_WFS, layer) for layer in SB_LAYERS] + [(ALKIS_WFS, ALKIS_LAYER)]
    data, nbytes = fetch_layers(src_bounds, layers)
    reproject_features(data, SOURCE_CRS, CRS_LABEL)
    build_stats.note_bytes("de_sb", nbytes, cached=False, key="sb_berlin")
    street = sum(len(data.get(layer) or []) for layer in SB_LAYERS)
    count = street + len(data.get(ALKIS_LAYER) or [])
    tiles = len(tile_bounds(src_bounds, TILE_M))
    build_stats.note_background("dtm", packages=tiles, regions=["DE-BE"], source="de_sb", objects=count)
    if log:
        log(f"Straßenbefahrung + ALKIS: {count} objektů z {tiles} dlaždic ({nbytes / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s)")
    if not street:
        if log:
            log("Straßenbefahrung: výřez mimo Berlín, kresba z OSM.")
        return None
    sorted_data = classify(data)
    try:
        sorted_data = node_coverage(sorted_data)
    except Exception as exc:  # bez uzlování se kreslí z původních ploch
        if log:
            log(f"Straßenbefahrung: uzlování ploch přeskočeno ({type(exc).__name__}: {exc})")
    return write_dtm_files(
        work_dir, sorted_data, bounds, log=log, source="de_sb", label="SB", labels=LOG_LABELS,
        yard_label="soukromé pozemky",
    )
