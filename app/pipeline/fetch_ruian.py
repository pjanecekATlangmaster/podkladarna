"""RÚIAN / INSPIRE budovy (StavebniObjekt) přes ArcGIS REST."""

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
from app.pipeline.fetch_openzu import FetchError
from app.pipeline.fetch_zabaged import query_layer_geojson

RUIAN_SERVICE = (
    "https://ags.cuzk.gov.cz/arcgis/rest/services/RUIAN/MapServer"
)
RUIAN_BUILDING_LAYER_ID = 3  # StavebniObjekt
RUIAN_GEOJSON_NAME = "ruian_buildings.geojson"


def ruian_cache_dir(bbox: tuple[float, float, float, float]) -> Path:
    key = bbox_cache_key(bbox)
    return settings.DOWNLOADS_DIR / "ruian" / key


def fetch_ruian_buildings_for_bbox(
    bbox: tuple[float, float, float, float],
    log=None,
) -> Path | None:
    """Stáhne RÚIAN StavebniObjekt (GeoJSON EPSG:5514) do cache. Při chybě None."""
    cache_dir = ruian_cache_dir(bbox)
    dest = cache_dir / RUIAN_GEOJSON_NAME
    max_age = settings.RUIAN_CACHE_MAX_AGE_DAYS
    if is_fresh(cache_dir, dest, max_age, min_size=50):
        if log:
            meta = read_meta(cache_dir) or {}
            age = (meta.get("downloaded_at") or "?")[:10]
            log(f"RÚIAN budovy cache ({cache_dir.name}, staženo {age})")
        return dest

    west, south, east, north = bbox
    try:
        gj = query_layer_geojson(
            RUIAN_SERVICE,
            RUIAN_BUILDING_LAYER_ID,
            west,
            south,
            east,
            north,
        )
    except FetchError as exc:
        if log:
            log(f"RÚIAN budovy: přeskočeno ({exc})")
        return None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        if log:
            log(f"RÚIAN budovy: přeskočeno ({exc})")
        return None

    n = len(gj.get("features") or [])
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(gj), encoding="utf-8")
    write_meta(
        cache_dir,
        source="ruian_ags",
        bbox_wgs84=list(bbox),
        features=n,
    )
    if log:
        log(f"RÚIAN budovy: {n} objektů, {dest.stat().st_size / 1e3:.0f} kB")
    return dest
