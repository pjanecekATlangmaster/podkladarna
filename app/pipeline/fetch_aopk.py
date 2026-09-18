"""AOPK památné stromy (jedinci) přes ArcGIS REST."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

from app import settings
from app.download_cache import (
    bbox_cache_key,
    is_fresh,
    read_meta,
    write_meta,
)
from app.pipeline.fetch_openzu import FetchError, VECTOR_FETCH_BUFFER_M, expand_bbox_wgs84
from app.pipeline.fetch_zabaged import query_layer_geojson

AOPK_TREES_SERVICE = (
    "https://gis.nature.cz/arcgis/rest/services/"
    "PamatneStromy/PamatneStromy/MapServer"
)
# Vrstva 1 = Památné stromy – jedinci (body).
AOPK_TREES_LAYER_ID = 1
AOPK_GEOJSON_NAME = "aopk_pamatne_stromy.geojson"


def aopk_cache_dir(bbox: tuple[float, float, float, float]) -> Path:
    key = bbox_cache_key(bbox)
    return settings.DOWNLOADS_DIR / "aopk" / key


def fetch_aopk_trees_for_bbox(
    bbox: tuple[float, float, float, float],
    log=None,
) -> Path | None:
    """Stáhne AOPK památné stromy (body, GeoJSON EPSG:5514). Při chybě None."""
    fetch_bbox = expand_bbox_wgs84(bbox, VECTOR_FETCH_BUFFER_M)
    cache_dir = aopk_cache_dir(fetch_bbox)
    dest = cache_dir / AOPK_GEOJSON_NAME
    max_age = settings.AOPK_CACHE_MAX_AGE_DAYS
    if is_fresh(cache_dir, dest, max_age, min_size=20):
        if log:
            meta = read_meta(cache_dir) or {}
            age = (meta.get("downloaded_at") or "?")[:10]
            log(f"AOPK stromy cache ({cache_dir.name}, staženo {age})")
        return dest

    west, south, east, north = fetch_bbox
    try:
        gj = query_layer_geojson(
            AOPK_TREES_SERVICE,
            AOPK_TREES_LAYER_ID,
            west,
            south,
            east,
            north,
        )
    except FetchError as exc:
        if log:
            log(f"AOPK stromy: přeskočeno ({exc})")
        return None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        if log:
            log(f"AOPK stromy: přeskočeno ({exc})")
        return None

    n = len(gj.get("features") or [])
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(gj), encoding="utf-8")
    write_meta(
        cache_dir,
        source="aopk_ags",
        bbox_wgs84=list(fetch_bbox),
        features=n,
    )
    if log:
        log(f"AOPK památné stromy: {n} jedinců, {dest.stat().st_size / 1e3:.0f} kB")
    return dest
