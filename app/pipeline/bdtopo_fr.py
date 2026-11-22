"""Francie mimo Paříž: BD TOPO (IGN) jako obdoba české DTM pro kresbu, obdobně jako ZBGIS na Slovensku.

BD TOPO je celostátní topografická databáze IGN (Licence Ouverte Etalab 2.0, WFS Géoplateforme bez klíče). Z ní se
bere jen to, co je spolehlivé: budovy v provozu (``batiment``) a vozovky z os silnic se šířkou vozovky
(``troncon_de_route.largeur_de_chaussee``, jako ``zbgis_sk.road_polygons``). Plochy chodníků BD TOPO nevede, polní
a lesní cesty, pěšiny a schody kreslí OSM. Parkoviště (``equipement_de_transport``) se neberou: v Lyonu jsou mezi nimi
podzemní a patrové garáže bez označení. Lyon PCRS má jen čáry obrubníků a fasád, které se neuzavírají (2026-11-22).

V Paříži zůstává Plan de voirie (``pdv_paris``, ``technical_map``); BD TOPO je ``technical_map_outside`` země.
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

WFS_URL = "https://data.geopf.fr/wfs/ows"
SOURCE_EPSG = 2154  # RGF93 v1 / Lambert-93; os v pořadí východ, sever i u urn:ogc:def:crs
SOURCE_CRS = f"EPSG:{SOURCE_EPSG}"
SOURCE_LABEL = "BD TOPO (IGN)"
WORKERS = 2
PAGE_SIZE = 5000  # CountDefault Géoplateforme
MAX_PAGES = 20
HTTP_TIMEOUT_S = 180
HTTP_RETRIES = 3
MAX_RESPONSE_BYTES = 300_000_000
LAYERS = ("batiment", "troncon_de_route")

IN_SERVICE = "En service"
# Silnice s vozovkou; „Route empierrée“ (štěrková), „Chemin“, „Sentier“, „Escalier“ a cyklostezky kreslí OSM
# (pás „silnice z DTM“ by mazal pěšiny – stejně jako ``zbgis_sk.SKIP_FORMS``).
ROAD_NATURES = frozenset({"Route à 1 chaussée", "Route à 2 chaussées", "Bretelle", "Rond-point", "Type autoroutier"})

LogFn = Callable[[str], None]


def wfs_url(layer: str, bounds: tuple[float, float, float, float], start: int = 0, count: int | None = None) -> str:
    """GetFeature GeoJSON vrstvy BD TOPO v obálce Lambert-93 (stránkování seřazené podle ``cleabs``)."""
    x0, y0, x1, y1 = bounds
    query = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": f"BDTOPO_V3:{layer}",
        "OUTPUTFORMAT": "application/json",
        "SRSNAME": SOURCE_CRS,
        "BBOX": f"{x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f},urn:ogc:def:crs:EPSG::{SOURCE_EPSG}",
        "COUNT": str(count or PAGE_SIZE),
        "SORTBY": "cleabs",
    }
    if start:
        query["STARTINDEX"] = str(start)
    return f"{WFS_URL}?{urllib.parse.urlencode(query)}"


def probe_url() -> str:
    """Lehký dotaz pro stav zdrojů: jedna budova u radnice v Lyonu."""
    x, y = 842_400.0, 6_519_500.0
    return wfs_url("batiment", (x - 100, y - 100, x + 100, y + 100), count=1)


def _get(url: str) -> bytes:
    from app.pipeline.fetch_openzu import USER_AGENT

    last: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"BD TOPO: odpověď větší než {MAX_RESPONSE_BYTES // 1_000_000} MB")
            return raw
        except Exception as exc:  # síť / 5xx: krátce počkat a zkusit znovu
            last = exc
            if attempt < HTTP_RETRIES:
                time.sleep(2.0 * (attempt + 1))
    assert last is not None
    raise last


def _fetch_layer(layer: str, bounds, fetch, log: LogFn | None = None) -> tuple[list[dict], int]:
    feats: list[dict] = []
    nbytes = 0
    for page in range(MAX_PAGES):
        raw = fetch(wfs_url(layer, bounds, page * PAGE_SIZE))
        nbytes += len(raw)
        data = json.loads(raw)
        if "features" not in data:
            raise RuntimeError(f"BD TOPO WFS: {str(data)[:200]}")
        batch = data["features"] or []
        feats.extend(f for f in batch if f.get("geometry"))
        if len(batch) < PAGE_SIZE:
            break
    else:
        if log:
            log(f"BD TOPO {layer}: dosažen limit {MAX_PAGES} stránek po {PAGE_SIZE}, zbytek výřezu jen z OSM")
    return feats, nbytes


def fetch_layers(
    bounds: tuple[float, float, float, float],
    layers=LAYERS,
    *,
    workers: int = WORKERS,
    fetch: Callable[[str], bytes] = _get,
    log: LogFn | None = None,
) -> tuple[dict[str, list[dict]], int]:
    """``{vrstva: [objekt, …]}`` pro obálku výřezu v Lambert-93."""
    layers = tuple(layers)
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="bdtopo-fr") as ex:
        results = list(ex.map(lambda layer: _fetch_layer(layer, bounds, fetch, log), layers))
    return {layer: feats for layer, (feats, _n) in zip(layers, results)}, sum(n for _f, n in results)


def road_features(troncons: list[dict]) -> list[dict]:
    """Úseky silnic s vozovkou ve tvaru ``zbgis_sk.road_polygons`` (``width``); tunely a neprovozované pryč."""
    out = []
    for f in troncons:
        props = f.get("properties") or {}
        if props.get("nature") not in ROAD_NATURES or props.get("etat_de_l_objet") != IN_SERVICE:
            continue
        try:
            if int(props.get("position_par_rapport_au_sol") or 0) < 0:
                continue
        except (TypeError, ValueError):
            pass
        out.append({"type": "Feature", "geometry": f["geometry"], "properties": {"width": props.get("largeur_de_chaussee")}})
    return out


def classify(data: dict[str, list[dict]]) -> dict[str, list]:
    from app.pipeline.bgt_nl import clean_polygons, feature_polygons
    from app.pipeline.zbgis_sk import road_polygons

    buildings = clean_polygons(
        [
            p
            for f in data.get("batiment") or []
            if (f.get("properties") or {}).get("etat_de_l_objet", IN_SERVICE) == IN_SERVICE
            for p in feature_polygons(f)
        ]
    )
    roads = road_polygons(road_features(data.get("troncon_de_route") or []))
    return {"buildings": buildings, "roads": roads, "public": list(roads)}


LOG_LABELS = {"buildings": "budovy", "roads": "vozovky ze šířky silnic"}


def prepare(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log: LogFn | None = None,
    force: bool = False,  # noqa: ARG001 – stejný podpis jako příprava DTM (data se necachují mezi joby)
) -> Path | None:
    """Budovy a vozovky BD TOPO pro výřez → soubory ``work_dir/dtm``; None, když ve výřezu nic není."""
    from pyproj import Transformer

    from app.pipeline.bgt_nl import write_dtm_files
    from app.pipeline.crs_5514 import CRS_LABEL
    from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514
    from app.pipeline.fmzk_at import reproject_features

    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    fetch_box = Transformer.from_crs(CRS_LABEL, SOURCE_CRS, always_xy=True).transform_bounds(*bounds, densify_pts=21)
    t0 = time.monotonic()
    build_stats.note_background("dtm", source="fr_bdtopo")
    data, nbytes = fetch_layers(fetch_box, log=log)
    reproject_features(data, SOURCE_CRS, CRS_LABEL)
    build_stats.note_bytes("fr_bdtopo", nbytes, cached=False, key="bdtopo_fr")
    count = sum(len(v) for v in data.values())
    build_stats.note_background("dtm", packages=len(LAYERS), regions=["FR"], source="fr_bdtopo", objects=count)
    if log:
        log(
            f"BD TOPO: {len(data['batiment'])} budov a {len(data['troncon_de_route'])} úseků silnic "
            f"({nbytes / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s)"
        )
    if not count:
        if log:
            log("BD TOPO: ve výřezu nic, kresba z OSM.")
        return None
    return write_dtm_files(
        work_dir, classify(data), bounds, log=log, source="fr_bdtopo", label="BD TOPO", labels=LOG_LABELS,
        yard_label="zahrady (v BD TOPO nejsou)",
    )
