"""Estonsko: ETAK (Eesti topograafia andmekogu, Maa- ja Ruumiamet) jako obdoba české DTM pro kresbu.

ETAK je celostátní topografická databáze (podrobnost ~1 : 2 000 ve městech) a pokrývá území souvislými plochami:
uliční prostor (``Liiklusala``, od fasády k fasádě, bez dělení na vozovku a chodník), pěší plochy a náměstí,
parkoviště, udržovaná zeleň (``Haljasala``), soukromé dvory a zahrady (``Eraõu`` → oliva), budovy a ploty.

Uliční prostor nemá vozovku zvlášť, proto nejde do ``roads`` (šířky silnic a koridory v lese by vyšly přes chodníky).
Sprint ho kreslí jako ulici v celé šířce (501 pod ostatními objekty) stejnou cestou jako české parcely komunikací
(``street_parcels``): příprava ho zapíše do ``work_dir/ruian_parcels/komunikace.geojson``.

WFS GeoServer Keskkonnaagentuur (gsavalik.envir.ee) bez klíče, EPSG:3301; otevřená licence Maa- ja Ruumiamet
(uvést zdroj a datum). Výsledek se zapíše do ``work_dir/dtm`` stejně jako BGT, AV, FMZK, Berlín a Plan de voirie.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from app.pipeline import build_stats, dtm_fences
from app.pipeline.bgt_nl import clean_polygons, feature_lines, feature_polygons, node_coverage, write_dtm_files
from app.pipeline.fmzk_at import reproject_features

ETAK_WFS = "https://gsavalik.envir.ee/geoserver/etak/ows"
SOURCE_CRS = "EPSG:3301"
SOURCE_LABEL = "ETAK (Maa- ja Ruumiamet)"
WORKERS = 4
PAGE_SIZE = 10000
MAX_PAGES = 20
HTTP_TIMEOUT_S = 180
HTTP_RETRIES = 3
MAX_RESPONSE_BYTES = 300_000_000

# vrstva WFS → (atribut ``tyyp`` → soubor DTM). ETAK kódy viz „ETAK layer names and descriptions“ (geoportál).
LAYERS: dict[str, dict[int, str]] = {
    "e_401_hoone_ka": {10: "buildings", 20: "buildings", 50: "buildings"},  # ne 40 Vare (zřícenina)
    "e_501_tee_a": {10: "streets", 20: "parking", 60: "sidewalks"},  # Liiklusala, Parkla, Jalakäijate ala
    "e_302_ou_a": {10: "yards"},  # Eraõu
    "e_301_muu_kolvik_a": {10: "green"},  # Haljasala
    "e_405_piire_j": {10: "fences"},  # Piirdeaed (zdi Müür / Tehissein kreslí OSM)
}
PUBLIC_KEYS = frozenset({"streets", "parking", "sidewalks"})
STREETS_FILE = ("ruian_parcels", "komunikace.geojson")  # ``street_parcels.CACHE_SUBDIR`` / ``PARCELS_NAME``

LogFn = Callable[[str], None]


def wfs_url(layer: str, bounds_3301: tuple[float, float, float, float], start: int = 0) -> str:
    xmin, ymin, xmax, ymax = bounds_3301
    query = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": f"etak:{layer}",
        "outputFormat": "application/json",
        "srsName": SOURCE_CRS,
        "bbox": f"{xmin:.1f},{ymin:.1f},{xmax:.1f},{ymax:.1f},{SOURCE_CRS}",
        "count": str(PAGE_SIZE),
        "startIndex": str(start),
    }
    return f"{ETAK_WFS}?{urllib.parse.urlencode(query)}"


def _get_json(url: str) -> tuple[dict, int]:
    from app.pipeline.fetch_openzu import USER_AGENT

    last: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"ETAK: odpověď větší než {MAX_RESPONSE_BYTES // 1_000_000} MB")
            return json.loads(raw), len(raw)
        except Exception as exc:  # síť / 5xx: krátce počkat a zkusit znovu
            last = exc
            if attempt < HTTP_RETRIES:
                time.sleep(2.0 * (attempt + 1))
    assert last is not None
    raise last


def _fetch_layer(layer: str, bounds_3301, fetch) -> tuple[list[dict], int]:
    feats: list[dict] = []
    nbytes = 0
    for page in range(MAX_PAGES):
        data, n = fetch(wfs_url(layer, bounds_3301, page * PAGE_SIZE))
        nbytes += n
        batch = [f for f in (data.get("features") or []) if f.get("geometry")]
        feats.extend(batch)
        if len(data.get("features") or []) < PAGE_SIZE:
            break
    return feats, nbytes


def fetch_layers(
    bounds_3301: tuple[float, float, float, float],
    layers=tuple(LAYERS),
    *,
    workers: int = WORKERS,
    fetch: Callable[[str], tuple[dict, int]] = _get_json,
) -> tuple[dict[str, list[dict]], int]:
    """``{vrstva: [objekt, …]}`` pro obálku výřezu v EPSG:3301 (vrstvy souběžně, stránkování po ``PAGE_SIZE``)."""
    layers = tuple(layers)
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="etak-ee") as ex:
        results = list(ex.map(lambda layer: _fetch_layer(layer, bounds_3301, fetch), layers))
    return {layer: feats for layer, (feats, _n) in zip(layers, results)}, sum(n for _f, n in results)


def _tyyp(feat: dict) -> int:
    try:
        return int((feat.get("properties") or {}).get("tyyp") or 0)
    except (TypeError, ValueError):
        return 0


def classify(data: dict[str, list[dict]]) -> dict[str, list]:
    """Objekty roztříděné do souborů DTM (``buildings``, ``sidewalks``, …) + ``streets`` (uliční prostor), ``public``."""
    keys = (
        "buildings", "roads", "sidewalks", "stairs", "parking", "small_paved", "green", "other_cover",
        "garden_cover", "yards", "decks", "fences", "public", "streets",
    )
    out: dict[str, list] = {k: [] for k in keys}
    for layer, by_type in LAYERS.items():
        for f in data.get(layer) or []:
            target = by_type.get(_tyyp(f))
            if target is None:
                continue
            if target == "fences":
                for line in feature_lines(f):
                    out["fences"].append(
                        dtm_fences.Fence(
                            coords=tuple(line), druh="", level=0, boundary=False, zpusob="1",
                            oid=str((f.get("properties") or {}).get("etak_id") or ""),
                        )
                    )
                continue
            polys = clean_polygons(feature_polygons(f))
            out[target].extend(polys)
            if target in PUBLIC_KEYS:
                out["public"].extend(polys)
    return out


def write_streets(work_dir: Path, polys: list) -> Path | None:
    """Uliční prostor jako GeoJSON v souřadnicích jobu pro ``street_parcels`` (sprint: ulice v celé šířce)."""
    dest = Path(work_dir).joinpath(*STREETS_FILE)
    if not polys:
        dest.unlink(missing_ok=True)
        return None
    feats = [
        {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in r] for r in poly]}}
        for poly in polys
    ]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    return dest


LOG_LABELS = {
    "buildings": "budovy (hoone)",
    "roads": "vozovky",
    "sidewalks": "pěší plochy a náměstí (jalakäijate ala)",
    "stairs": "schody",
    "parking": "parkoviště (parkla)",
    "small_paved": "ostatní zpevněné plochy",
    "green": "udržovaná zeleň (haljasala)",
    "other_cover": "jiný kryt",
}


def prepare(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log: LogFn | None = None,
    force: bool = False,  # noqa: ARG001 – stejný podpis jako příprava DTM (data se necachují mezi joby)
) -> Path | None:
    """ETAK pro výřez → soubory ``work_dir/dtm`` a uliční prostor pro sprint; None, když služba nic nevrátí."""
    from pyproj import Transformer

    from app.pipeline.crs_5514 import CRS_LABEL
    from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514

    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    fetch_box = Transformer.from_crs(CRS_LABEL, SOURCE_CRS, always_xy=True).transform_bounds(*bounds, densify_pts=21)
    t0 = time.monotonic()
    build_stats.note_background("dtm", source="ee_etak")
    data, nbytes = fetch_layers(fetch_box)
    reproject_features(data, SOURCE_CRS, CRS_LABEL)
    build_stats.note_bytes("ee_etak", nbytes, cached=False, key="etak_ee")
    count = sum(len(v) for v in data.values())
    build_stats.note_background("dtm", packages=len(LAYERS), regions=["EE"], source="ee_etak", objects=count)
    if log:
        log(f"ETAK: {count} objektů z {len(LAYERS)} vrstev ({nbytes / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s)")
    if not count:
        if log:
            log("ETAK: ve výřezu nic, kresba z OSM.")
        return None
    sorted_data = classify(data)
    streets = sorted_data.pop("streets")
    if write_streets(work_dir, streets) is not None and log:
        log(f"ETAK uliční prostor (liiklusala): {len(streets)} ploch – sprint ho kreslí jako ulici v celé šířce")
    try:
        sorted_data = node_coverage(sorted_data)
    except Exception as exc:  # bez uzlování se kreslí z původních ploch
        if log:
            log(f"ETAK: uzlování ploch přeskočeno ({type(exc).__name__}: {exc})")
    return write_dtm_files(
        work_dir, sorted_data, bounds, log=log, source="ee_etak", label="ETAK", labels=LOG_LABELS,
        yard_label="soukromé dvory a zahrady (eraõu)",
    )
