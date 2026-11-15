"""Finsko (Helsinky): rejstřík veřejných ploch YLRE a budovy Helsinek jako obdoba české DTM pro kresbu.

Helsinky vedou veřejné plochy po částech (YLRE, Yleisten alueiden rekisteri): vozovky (``Ajorata``), parkoviště
(``Pysäköintialue``), ostrůvky (``Koroke``), chodníky, náměstí a cyklostezky (``Kevyt liikenne``), schody (``Portaat``),
trávníky a záhony v ulicích, k tomu uliční prostor (``Katualue``, od hranice k hranici pozemku) a budovy
(``Rakennukset_alue``). Data jsou z WFS kartta.hel.fi (avoindata) bez klíče, CC BY 4.0 („Helsingin kaupunki“).

Stahuje se v nativním EPSG:3879 (ETRS-GK25) a převádí přes PROJ (poučení z Vídně: cizí server při převodu nemusí
umět posun data). Uliční prostor jde do sprintových „parcel komunikací“ jako u ETAK a GRB, ostatní do ``work_dir/dtm``.
Parkové cesty a pěšiny (``Puistokäytävät``, ``Polku``) se neberou – kreslí je čára z OSM.
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
from app.pipeline.etak_ee import write_streets
from app.pipeline.fmzk_at import reproject_features

HEL_WFS = "https://kartta.hel.fi/ws/geoserver/avoindata/wfs"
SOURCE_CRS = "EPSG:3879"
WORKERS = 4
PAGE_SIZE = 10000
MAX_PAGES = 30
HTTP_TIMEOUT_S = 180
HTTP_RETRIES = 3
MAX_RESPONSE_BYTES = 300_000_000
BUILDINGS = "Rakennukset_alue"
ROADS = "YLRE_Katu_ja_viherosat_ajorata_alue"
WALKS = "YLRE_Katu_ja_viherosat_kevytliikenne_alue"
OTHER = "YLRE_Katu_ja_viherosat_eiliikenne_alue"
STREETS = "YLRE_Katualue_alue"
LAYERS = (BUILDINGS, ROADS, WALKS, OTHER, STREETS)
# alatyyppi (bez přípony „ (Silta)“) → soubor DTM
ROAD_TYPES = {
    "Ajorata": "roads",
    "Ajorata, muu": "roads",
    "Tonttiliittymä": "roads",
    "Pysäköintialue": "parking",
    "Koroke": "small_paved",
}
WALK_TYPES = {
    "Jalankulkualue": "sidewalks",
    "Aukiot": "sidewalks",
    "Erotettu jalkakäytävä": "sidewalks",
    "Erotettu pyörätie": "sidewalks",
    "Yhdistetty jk ja pp": "sidewalks",
    "Pyöräilyalue/kaista": "sidewalks",
    "Kevyt liikenne": "sidewalks",
    "Pelastusreitit": "sidewalks",
    "Portaat": "stairs",
    "Lämmitettävät portaat": "stairs",
}
GREEN_MAIN_TYPES = frozenset({"Nurmikot ja niityt"})  # trávníky a louky v ulicích a parcích
PUBLIC_KEYS = frozenset({"roads", "parking", "small_paved", "sidewalks", "stairs"})

LogFn = Callable[[str], None]


def wfs_url(layer: str, bounds: tuple[float, float, float, float], start: int = 0) -> str:
    xmin, ymin, xmax, ymax = bounds
    query = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": f"avoindata:{layer}",
        "outputFormat": "application/json",
        "srsName": SOURCE_CRS,
        "bbox": f"{xmin:.1f},{ymin:.1f},{xmax:.1f},{ymax:.1f},{SOURCE_CRS}",
        "count": str(PAGE_SIZE),
        "startIndex": str(start),
    }
    return f"{HEL_WFS}?{urllib.parse.urlencode(query)}"


def _get_json(url: str) -> tuple[dict, int]:
    from app.pipeline.fetch_openzu import USER_AGENT

    last: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"Helsinky WFS: odpověď větší než {MAX_RESPONSE_BYTES // 1_000_000} MB")
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
    """``{vrstva: [objekt, …]}`` pro obálku výřezu v EPSG:3879 (vrstvy souběžně, stránkování po ``PAGE_SIZE``)."""
    layers = tuple(layers)
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="ylre-fi") as ex:
        results = list(ex.map(lambda layer: _fetch_layer(layer, bounds, fetch), layers))
    return {layer: feats for layer, (feats, _n) in zip(layers, results)}, sum(n for _f, n in results)


def _subtype(feat: dict) -> str:
    """``alatyyppi`` bez přípony mostu („Ajorata (Silta)“ → „Ajorata“)."""
    value = str((feat.get("properties") or {}).get("alatyyppi") or "")
    return value.removesuffix(" (Silta)").strip()


def classify(data: dict[str, list[dict]]) -> dict[str, list]:
    """Objekty Helsinek roztříděné do souborů DTM + ``streets`` (uliční prostor pro sprint) a ``public``."""
    keys = (
        "buildings", "roads", "sidewalks", "stairs", "parking", "small_paved", "green", "other_cover",
        "garden_cover", "yards", "decks", "fences", "public", "streets",
    )
    out: dict[str, list] = {k: [] for k in keys}
    for f in data.get(BUILDINGS) or []:
        out["buildings"].extend(feature_polygons(f))
    for layer, table in ((ROADS, ROAD_TYPES), (WALKS, WALK_TYPES)):
        for f in data.get(layer) or []:
            key = table.get(_subtype(f))
            if key:
                out[key].extend(feature_polygons(f))
    for f in data.get(OTHER) or []:
        if str((f.get("properties") or {}).get("paatyyppi") or "") in GREEN_MAIN_TYPES:
            out["green"].extend(feature_polygons(f))
    out["streets"] = [p for f in data.get(STREETS) or [] for p in feature_polygons(f)]
    for key in list(out):
        if key != "public":
            out[key] = clean_polygons(out[key])
    out["public"] = [p for key in sorted(PUBLIC_KEYS) for p in out[key]]
    return out


LOG_LABELS = {
    "buildings": "budovy (rakennukset)",
    "roads": "vozovky (ajorata)",
    "parking": "parkoviště (pysäköintialue)",
    "small_paved": "ostrůvky (koroke)",
    "sidewalks": "chodníky, náměstí a cyklostezky (kevyt liikenne)",
    "stairs": "schody (portaat)",
    "green": "trávníky (nurmikot)",
}


def prepare(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log: LogFn | None = None,
    force: bool = False,  # noqa: ARG001 – stejný podpis jako příprava DTM (data se necachují mezi joby)
) -> Path | None:
    """Helsinky pro výřez → soubory ``work_dir/dtm`` a uliční prostor pro sprint; None, když služba nic nevrátí."""
    from pyproj import Transformer

    from app.pipeline.crs_5514 import CRS_LABEL
    from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514

    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    fetch_box = Transformer.from_crs(CRS_LABEL, SOURCE_CRS, always_xy=True).transform_bounds(*bounds, densify_pts=21)
    t0 = time.monotonic()
    build_stats.note_background("dtm", source="fi_hel")
    data, nbytes = fetch_layers(fetch_box)
    reproject_features(data, SOURCE_CRS, CRS_LABEL)
    build_stats.note_bytes("fi_hel", nbytes, cached=False, key="ylre_fi")
    count = sum(len(v) for v in data.values())
    build_stats.note_background("dtm", packages=len(LAYERS), regions=["FI"], source="fi_hel", objects=count)
    if log:
        log(f"Helsinky YLRE: {count} objektů z {len(LAYERS)} vrstev ({nbytes / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s)")
    if not data.get(BUILDINGS) and not data.get(ROADS):
        if log:
            log("Helsinky YLRE: ve výřezu nic (mimo Helsinky), kresba z OSM.")
        return None
    sorted_data = classify(data)
    streets = sorted_data.pop("streets")
    if write_streets(work_dir, streets) is not None and log:
        log(f"Helsinky uliční prostor (katualue): {len(streets)} ploch – sprint ho kreslí jako ulici v celé šířce")
    try:
        sorted_data = node_coverage(sorted_data)
    except Exception as exc:  # bez uzlování se kreslí z původních ploch
        if log:
            log(f"Helsinky YLRE: uzlování ploch přeskočeno ({type(exc).__name__}: {exc})")
    return write_dtm_files(
        work_dir, sorted_data, bounds, log=log, source="fi_hel", label="Helsinky YLRE", labels=LOG_LABELS,
        yard_label="soukromé pozemky (YLRE je nevede)",
    )
