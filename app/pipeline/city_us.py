"""USA: městská planimetrická data (New York, Washington DC) jako obdoba české DTM pro kresbu.

Celostátní velkoměřítková mapa s plochami chodníků v USA není (budovy a silnice jsou z OSM). Některá města ale vedou
„planimetrics“ – plochy vozovek, chodníků, parkovišť a budov zachycené z leteckých snímků:

- New York City: NYC Planimetric Database (OTI, snímky 2022, vydáno 2024) a Building Footprints (průběžně), NYC Open
  Data (Socrata) bez klíče, dotaz ``intersects(the_geom, POLYGON)``; podmínky NYC Open Data bez omezení použití.
- Washington DC: Planimetrics 2023 (DC GIS / OCTO), ArcGIS MapServer ``maps2.dcgis.dc.gov`` bez klíče, CC BY 4.0.

Obě služby vrací WGS84 (GeoJSON); převod do souřadnic jobu (NAD83 / UTM) dělá PROJ (``fmzk_at.reproject_features``).
Výsledek se zapíše do ``work_dir/dtm`` stejně jako BGT, Plan de voirie nebo YLRE, takže OSM zůstává základem a městská
data jen doplňují (dedup proti OSM dělá kresba DTM). Mimo města (``technical_map_area``) se nic nestahuje.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.pipeline import build_stats
from app.pipeline.bgt_nl import clean_polygons, feature_polygons, node_coverage, write_dtm_files
from app.pipeline.fmzk_at import reproject_features

SOURCE_CRS = "EPSG:4326"
WORKERS = 4
HTTP_TIMEOUT_S = 180
HTTP_RETRIES = 3
MAX_RESPONSE_BYTES = 300_000_000
MAX_PAGES = 40

NYC_SODA = "https://data.cityofnewyork.us/resource"
NYC_PAGE = 50000
DC_PLANIMETRICS = "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/Planimetrics_2023/MapServer"
DC_PAGE = 2000

# Sondy zdrojů (``source_status``): jeden objekt z každé služby.
NYC_PROBE = f"{NYC_SODA}/7cgt-uhhz.json?$limit=1&$select=feat_code"
DC_PROBE = f"{DC_PLANIMETRICS}/10/query?where=1%3D1&outFields=OBJECTID&returnGeometry=false&resultRecordCount=1&f=json"


@dataclass(frozen=True)
class Layer:
    key: str  # id datové sady (Socrata) nebo číslo vrstvy (ArcGIS)
    code_field: str  # pole s kódem druhu
    codes: dict[int, str]  # kód → soubor DTM (chybějící kód = nebrat)


@dataclass(frozen=True)
class City:
    key: str
    name: str
    envelope: tuple[float, float, float, float]  # hrubá obálka WGS84 (západ, jih, východ, sever)
    service: str  # "socrata" | "arcgis"
    layers: tuple[Layer, ...]

    def intersects(self, bbox: tuple[float, float, float, float]) -> bool:
        w, s, e, n = bbox
        cw, cs, ce, cn = self.envelope
        return w < ce and e > cw and s < cn and n > cs


# NYC Planimetric Database (kódy podle github.com/CityOfNewYork/nyc-planimetrics Capture_Rules.md).
# Roadbed: vozovka, křižovatka, krajnice, vjezd. Median: ostrůvek a obrubníkový střed ano, malovaný (leží na vozovce) ne,
# travnatý jako zeleň. Budovy: 2100 budova, 5110 garáž (lávky 2110 a rozestavěné ne).
NYC = City(
    key="nyc",
    name="New York City",
    envelope=(-74.27, 40.48, -73.68, 40.93),
    service="socrata",
    layers=(
        Layer("5zhs-2jue", "feature_code", {2100: "buildings", 5110: "buildings"}),
        Layer("i36f-5ih7", "sub_code", {350000: "roads", 350010: "roads", 350020: "roads", 350030: "roads"}),
        Layer("52n9-sdep", "sub_code", {380000: "sidewalks", 380010: "sidewalks"}),
        Layer("ue2e-9jm2", "sub_code", {600000: "sidewalks", 600010: "sidewalks"}),
        Layer("7cgt-uhhz", "sub_code", {500000: "parking"}),
        Layer("ees7-4ufv", "sub_code", {360080: "small_paved", 360020: "small_paved", 360050: "green"}),
    ),
)

# DC Planimetrics 2023: Roads (10), Sidewalks (11), Building Footprints (1). „Hidden“ = pod mostem / nadjezdem – ne,
# přechody leží na vozovce – ne; nezpevněné ostrůvky jako zeleň.
DC = City(
    key="dc",
    name="Washington DC",
    envelope=(-77.13, 38.79, -76.90, 39.00),
    service="arcgis",
    layers=(
        Layer("1", "FEATURECODE", {2000: "buildings", 2030: "buildings"}),
        Layer(
            "10",
            "FEATURECODE",
            {
                1050: "roads", 1060: "roads", 1065: "roads", 1090: "roads", 1070: "parking",
                1055: "small_paved", 1056: "small_paved", 1057: "green", 1058: "green",
            },
        ),
        Layer("11", "FEATURECODE", {1480: "sidewalks", 1482: "stairs"}),
    ),
)

CITIES: tuple[City, ...] = (NYC, DC)
PUBLIC_KEYS = frozenset({"roads", "parking", "small_paved", "sidewalks", "stairs"})

LogFn = Callable[[str], None]


def cities_for(bbox_wgs84: tuple[float, float, float, float]) -> list[City]:
    return [c for c in CITIES if c.intersects(bbox_wgs84)]


def socrata_url(dataset: str, bbox: tuple[float, float, float, float], offset: int = 0, limit: int = NYC_PAGE) -> str:
    w, s, e, n = bbox
    ring = f"{w:.6f} {s:.6f},{e:.6f} {s:.6f},{e:.6f} {n:.6f},{w:.6f} {n:.6f},{w:.6f} {s:.6f}"
    query = {
        "$where": f"intersects(the_geom,'POLYGON(({ring}))')",
        "$order": ":id",
        "$limit": str(limit),
        "$offset": str(offset),
    }
    return f"{NYC_SODA}/{dataset}.geojson?{urllib.parse.urlencode(query)}"


def arcgis_url(layer: str, bbox: tuple[float, float, float, float], offset: int = 0, limit: int = DC_PAGE) -> str:
    w, s, e, n = bbox
    query = {
        "where": "1=1",
        "geometry": f"{w:.6f},{s:.6f},{e:.6f},{n:.6f}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "OBJECTID,FEATURECODE",
        "outSR": "4326",
        "orderByFields": "OBJECTID",
        "resultOffset": str(offset),
        "resultRecordCount": str(limit),
        "f": "geojson",
    }
    return f"{DC_PLANIMETRICS}/{layer}/query?{urllib.parse.urlencode(query)}"


def _get_json(url: str) -> tuple[dict, int]:
    from app.pipeline.fetch_openzu import USER_AGENT

    last: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"Planimetrics: odpověď větší než {MAX_RESPONSE_BYTES // 1_000_000} MB")
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("error"):  # ArcGIS hlásí chybu s HTTP 200
                raise RuntimeError(f"Planimetrics: chyba služby {data['error']}")
            return data, len(raw)
        except Exception as exc:  # síť / 5xx: krátce počkat a zkusit znovu
            last = exc
            if attempt < HTTP_RETRIES:
                time.sleep(2.0 * (attempt + 1))
    assert last is not None
    raise last


def _fetch_layer(city: City, layer: Layer, bbox, fetch) -> tuple[list[dict], int]:
    page = NYC_PAGE if city.service == "socrata" else DC_PAGE
    feats: list[dict] = []
    nbytes = 0
    for i in range(MAX_PAGES):
        url = socrata_url(layer.key, bbox, i * page) if city.service == "socrata" else arcgis_url(layer.key, bbox, i * page)
        data, n = fetch(url)
        nbytes += n
        got = data.get("features") or []
        feats.extend(f for f in got if f.get("geometry"))
        if len(got) < page and not data.get("exceededTransferLimit"):
            break
    return feats, nbytes


def fetch_city(
    city: City,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    workers: int = WORKERS,
    fetch: Callable[[str], tuple[dict, int]] = _get_json,
) -> tuple[dict[str, list[dict]], int]:
    """``{"<město>:<vrstva>": [objekt, …]}`` pro obálku výřezu (vrstvy souběžně, stránkování)."""
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix=f"us-{city.key}") as ex:
        results = list(ex.map(lambda layer: _fetch_layer(city, layer, bbox_wgs84, fetch), city.layers))
    data = {f"{city.key}:{layer.key}": feats for layer, (feats, _n) in zip(city.layers, results)}
    return data, sum(n for _f, n in results)


def _code(feat: dict, field: str) -> int | None:
    value = (feat.get("properties") or {}).get(field)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def classify(data: dict[str, list[dict]]) -> dict[str, list]:
    """Objekty roztříděné do souborů DTM (``roads``, ``sidewalks``, …) + ``public``."""
    keys = (
        "buildings", "roads", "sidewalks", "stairs", "parking", "small_paved", "green", "other_cover",
        "garden_cover", "yards", "decks", "fences", "public",
    )
    out: dict[str, list] = {k: [] for k in keys}
    for city in CITIES:
        for layer in city.layers:
            for f in data.get(f"{city.key}:{layer.key}") or []:
                target = layer.codes.get(_code(f, layer.code_field))
                if target:
                    out[target].extend(feature_polygons(f))
    for key in list(out):
        if key != "public":
            out[key] = clean_polygons(out[key])
    out["public"] = [p for key in sorted(PUBLIC_KEYS) for p in out[key]]
    return out


LOG_LABELS = {
    "buildings": "budovy (building footprints)",
    "roads": "vozovky (roadbed)",
    "sidewalks": "chodníky a pěší plochy (sidewalk, plaza)",
    "stairs": "schody",
    "parking": "parkoviště (parking lot)",
    "small_paved": "ostrůvky (median, island)",
    "green": "travnaté ostrůvky",
}


def prepare(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log: LogFn | None = None,
    force: bool = False,  # noqa: ARG001 – stejný podpis jako příprava DTM (data se necachují mezi joby)
) -> Path | None:
    """Planimetrics měst pro výřez → soubory ``work_dir/dtm``; None mimo města (kresba z OSM)."""
    from pyproj import Transformer

    from app.pipeline.crs_5514 import CRS_LABEL
    from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514

    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    fetch_box = Transformer.from_crs(CRS_LABEL, SOURCE_CRS, always_xy=True).transform_bounds(*bounds, densify_pts=21)
    cities = cities_for(fetch_box)
    if not cities:
        if log:
            log("Planimetrics: výřez mimo města s planimetrickými daty, kresba z OSM.")
        return None
    t0 = time.monotonic()
    build_stats.note_background("dtm", source="us_city")
    data: dict[str, list[dict]] = {}
    nbytes = 0
    for city in cities:
        part, n = fetch_city(city, fetch_box)
        data.update(part)
        nbytes += n
    reproject_features(data, SOURCE_CRS, CRS_LABEL)
    build_stats.note_bytes("us_city", nbytes, cached=False, key="city_us")
    count = sum(len(v) for v in data.values())
    names = ", ".join(c.name for c in cities)
    build_stats.note_background(
        "dtm", packages=sum(len(c.layers) for c in cities), regions=[f"US-{c.key.upper()}" for c in cities],
        source="us_city", objects=count,
    )
    if log:
        log(f"Planimetrics ({names}): {count} objektů ({nbytes / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s)")
    if not count:
        if log:
            log("Planimetrics: ve výřezu nic, kresba z OSM.")
        return None
    sorted_data = classify(data)
    try:
        sorted_data = node_coverage(sorted_data)
    except Exception as exc:  # bez uzlování se kreslí z původních ploch
        if log:
            log(f"Planimetrics: uzlování ploch přeskočeno ({type(exc).__name__}: {exc})")
    return write_dtm_files(
        work_dir, sorted_data, bounds, log=log, source="us_city", label="Planimetrics", labels=LOG_LABELS,
        yard_label="soukromé pozemky (planimetrics je nevede)",
    )
