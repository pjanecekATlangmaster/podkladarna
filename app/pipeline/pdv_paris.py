"""Paříž: Plan de voirie (Ville de Paris) jako obdoba české DTM pro kresbu.

Francie nemá celostátní velkoměřítkovou mapu s plochami chodníků; PCRS je jen čárový a po obcích. Paříž vede
Plan de voirie jako plochy (Open Data Paris, ODbL, bez klíče, aktualizace průběžně): vozovky (chaussées), chodníky
(trottoirs – emprises), smíšené plochy pro pěší a vozidla, schodiště (voies en escalier), zeleň (emprises espaces
verts) a dělicí ostrůvky. Budovy Plan de voirie nevede, kreslí je OSM (v Paříži z katastru).

Služba (Opendatasoft) vrací WGS84; převod do souřadnic jobu (Lambert-93) dělá PROJ (``fmzk_at.reproject_features``),
RGF93 a WGS84 se liší o centimetry. Výsledek se zapíše do ``work_dir/dtm`` stejně jako BGT, AV, FMZK a Berlín.
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
from app.pipeline.bgt_nl import clean_polygons, feature_polygons, node_coverage, write_dtm_files
from app.pipeline.fmzk_at import reproject_features

PARIS_API = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets"
SOURCE_CRS = "EPSG:4326"
SOURCE_LABEL = "Plan de voirie (Ville de Paris)"
WORKERS = 4
HTTP_TIMEOUT_S = 180
HTTP_RETRIES = 3
MAX_RESPONSE_BYTES = 300_000_000

# datová sada → soubor DTM (CZ obdoba). Pruhy pro kola a autobusy leží na vozovce, terre-pleins bývají osázené – ne.
DATASETS: dict[str, str] = {
    "plan-de-voirie-chaussees": "roads",
    "plan-de-voirie-trottoirs-emprises": "sidewalks",
    "plan-de-voirie-aires-mixtes-vehicules-et-pietons": "sidewalks",
    "plan-de-voirie-voies-en-escalier": "stairs",
    "plan-de-voirie-ilots-directionnels": "small_paved",
    "plan-de-voirie-emprises-espaces-verts": "green",
}
PUBLIC_KEYS = frozenset({"roads", "sidewalks", "small_paved"})

LogFn = Callable[[str], None]


def export_url(dataset: str, bbox_wgs84: tuple[float, float, float, float]) -> str:
    """Export GeoJSON objektů datové sady v obálce (``in_bbox`` chce jih, západ, sever, východ)."""
    west, south, east, north = bbox_wgs84
    query = {"where": f"in_bbox(geo_shape,{south:.6f},{west:.6f},{north:.6f},{east:.6f})", "limit": "-1"}
    return f"{PARIS_API}/{dataset}/exports/geojson?{urllib.parse.urlencode(query)}"


def _get_json(url: str) -> tuple[dict, int]:
    from app.pipeline.fetch_openzu import USER_AGENT

    last: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"Plan de voirie: odpověď větší než {MAX_RESPONSE_BYTES // 1_000_000} MB")
            return json.loads(raw), len(raw)
        except Exception as exc:  # síť / 5xx: krátce počkat a zkusit znovu
            last = exc
            if attempt < HTTP_RETRIES:
                time.sleep(2.0 * (attempt + 1))
    assert last is not None
    raise last


def fetch_datasets(
    bbox_wgs84: tuple[float, float, float, float],
    datasets=tuple(DATASETS),
    *,
    workers: int = WORKERS,
    fetch: Callable[[str], tuple[dict, int]] = _get_json,
) -> tuple[dict[str, list[dict]], int]:
    """``{datová sada: [objekt, …]}`` pro obálku výřezu (sady souběžně)."""
    datasets = tuple(datasets)
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="pdv-paris") as ex:
        results = list(ex.map(lambda ds: fetch(export_url(ds, bbox_wgs84)), datasets))
    out = {ds: [f for f in (data.get("features") or []) if f.get("geometry")] for ds, (data, _n) in zip(datasets, results)}
    return out, sum(n for _d, n in results)


def classify(data: dict[str, list[dict]]) -> dict[str, list]:
    """Objekty roztříděné do souborů DTM (``roads``, ``sidewalks``, …) + ``public``."""
    keys = (
        "buildings", "roads", "sidewalks", "stairs", "parking", "small_paved", "green", "other_cover",
        "garden_cover", "yards", "decks", "fences", "public",
    )
    out: dict[str, list] = {k: [] for k in keys}
    for dataset, target in DATASETS.items():
        for f in data.get(dataset) or []:
            polys = clean_polygons(feature_polygons(f))
            out[target].extend(polys)
            if target in PUBLIC_KEYS:
                out["public"].extend(polys)
    return out


LOG_LABELS = {
    "buildings": "budovy",
    "roads": "vozovky (chaussées)",
    "sidewalks": "chodníky a smíšené plochy (trottoirs)",
    "stairs": "schodiště (voies en escalier)",
    "parking": "parkování",
    "small_paved": "dělicí ostrůvky",
    "green": "zeleň (espaces verts)",
    "other_cover": "jiný kryt",
}


def prepare(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log: LogFn | None = None,
    force: bool = False,  # noqa: ARG001 – stejný podpis jako příprava DTM (data se necachují mezi joby)
) -> Path | None:
    """Plan de voirie pro výřez → soubory ``work_dir/dtm``; None mimo Paříž (služba nic nevrátí)."""
    from app.pipeline.crs_5514 import CRS_LABEL
    from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514

    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    # obálka s rezervou zpět ve WGS84 (rezerva výřezu platí i pro stažení)
    from pyproj import Transformer

    fetch_box = Transformer.from_crs(CRS_LABEL, SOURCE_CRS, always_xy=True).transform_bounds(*bounds, densify_pts=21)
    t0 = time.monotonic()
    build_stats.note_background("dtm", source="fr_pdv")
    data, nbytes = fetch_datasets(fetch_box)
    reproject_features(data, SOURCE_CRS, CRS_LABEL)
    build_stats.note_bytes("fr_pdv", nbytes, cached=False, key="pdv_paris")
    count = sum(len(v) for v in data.values())
    build_stats.note_background("dtm", packages=len(DATASETS), regions=["FR-75"], source="fr_pdv", objects=count)
    if log:
        log(f"Plan de voirie: {count} objektů z {len(DATASETS)} sad ({nbytes / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s)")
    if not count:
        if log:
            log("Plan de voirie: výřez mimo Paříž, kresba z OSM.")
        return None
    sorted_data = classify(data)
    try:
        sorted_data = node_coverage(sorted_data)
    except Exception as exc:  # bez uzlování se kreslí z původních ploch
        if log:
            log(f"Plan de voirie: uzlování ploch přeskočeno ({type(exc).__name__}: {exc})")
    return write_dtm_files(
        work_dir, sorted_data, bounds, log=log, source="fr_pdv", label="PDV", labels=LOG_LABELS,
        yard_label="soukromé pozemky",
    )
