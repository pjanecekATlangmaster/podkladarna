from __future__ import annotations

import json
import shutil
import subprocess
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import yaml

from app import settings
from app.pipeline.fetch_openzu import (
    DOWNLOAD_TIMEOUT_S,
    FetchError,
    QUERY_TIMEOUT_S,
    USER_AGENT,
    crop_bounds_5514,
)
from app.tool_env import gis_subprocess_env, which_tool

PAGE_SIZE = 2000
MAX_PAGES = 25
# KP kreslí 529 (parking) přes 401. Celoměstské zbytky vrstvy 115
# (řád km²) by jinak přemalovaly louky a křoviny na zpevněnou plochu.
MAX_OSTATNI_PLOCHA_M2 = 1_000_000.0


def _ags_config() -> dict:
    path = settings.CONFIG_DIR / "zabaged_ags.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def fetch_zabaged_for_bbox(
    bbox: tuple[float, float, float, float],
    dest_zip: Path,
    log: callable | None = None,
) -> Path:
    """Stáhne vybrané vrstvy ZABAGED pro WGS84 bbox a uloží shapefile ZIP."""
    ogr2ogr = which_tool("ogr2ogr")
    if not ogr2ogr:
        raise FetchError(
            "ogr2ogr (GDAL) není v PATH – nejde sestavit ZABAGED shapefile. "
            "Windows: OSGeo4W, nebo docker compose -f docker-compose.dev.yml up"
        )

    west, south, east, north = bbox
    xmin, ymin, xmax, ymax = crop_bounds_5514(west, south, east, north)
    cfg = _ags_config()
    service = cfg["service"].rstrip("/")
    layers: dict[str, int] = cfg["layers"]

    stage = dest_zip.parent / "_zabaged_ags_stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    kept = 0
    try:
        for name, layer_id in layers.items():
            gj = query_layer_geojson(service, int(layer_id), west, south, east, north)
            if name == "OstatniPlochaVSidlech":
                n_before = len(gj.get("features") or [])
                drop_oversized_ostatni_plocha(gj)
                n_dropped = n_before - len(gj.get("features") or [])
                if log and n_dropped:
                    log(f"  OstatniPlochaVSidlech: vynechano {n_dropped} obrich polygonu")
            n = len(gj.get("features") or [])
            if n <= 0:
                if log:
                    log(f"  skip (prazdne): {name}")
                continue
            tag_features_with_layer(gj, name)
            geojson_path = stage / f"{name}.geojson"
            geojson_path.write_text(json.dumps(gj), encoding="utf-8")
            shp = stage / f"{name}.shp"
            _ogr2ogr_shp(ogr2ogr, geojson_path, shp, (xmin, ymin, xmax, ymax))
            geojson_path.unlink(missing_ok=True)
            if log:
                log(f"  OK {name}: {n} prvku")
            kept += 1
        if kept == 0:
            raise FetchError("ZABAGED v tomto výřezu nemá žádné použitelné vrstvy")

        dest_zip.parent.mkdir(parents=True, exist_ok=True)
        if dest_zip.exists():
            dest_zip.unlink()
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in stage.iterdir():
                if f.is_file() and f.suffix.lower() != ".geojson":
                    zf.write(f, f.name)
        if log:
            log(f"ZABAGED ZIP: {kept} vrstev, {dest_zip.stat().st_size / 1e3:.0f} kB")
        return dest_zip
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def query_layer_geojson(
    service: str,
    layer_id: int,
    west: float,
    south: float,
    east: float,
    north: float,
) -> dict:
    features: list[dict] = []
    for page in range(MAX_PAGES):
        params = {
            "geometry": f"{west},{south},{east},{north}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "outSR": "5514",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "f": "geojson",
            "resultRecordCount": str(PAGE_SIZE),
            "resultOffset": str(page * PAGE_SIZE),
        }
        url = f"{service}/{layer_id}/query?" + urllib.parse.urlencode(params)
        data = _http_json(url, timeout=QUERY_TIMEOUT_S)
        if data.get("error"):
            raise FetchError(f"ArcGIS ZABAGED vrstva {layer_id}: {data['error']}")
        batch = data.get("features") or []
        if data.get("error"):
            raise FetchError(f"ArcGIS ZABAGED vrstva {layer_id}: {data['error']}")
        if not batch:
            break
        features.extend(batch)
        if not data.get("exceededTransferLimit"):
            break
    else:
        raise FetchError(f"ZABAGED vrstva {layer_id}: příliš mnoho prvků (>{MAX_PAGES * PAGE_SIZE})")

    return {"type": "FeatureCollection", "features": features}


def drop_oversized_ostatni_plocha(
    gj: dict,
    max_area_m2: float = MAX_OSTATNI_PLOCHA_M2,
) -> dict:
    """Zahodí celoměstské polygony 115, které po ořezu vyplní celý podklad 529."""
    kept = []
    for feat in gj.get("features") or []:
        props = feat.get("properties") or {}
        area = props.get("Shape_Area", props.get("shape_area"))
        if isinstance(area, (int, float)) and area > max_area_m2:
            continue
        kept.append(feat)
    gj["features"] = kept
    return gj


def tag_features_with_layer(gj: dict, layer_name: str) -> dict:
    """Karttapullautin matchuje atributy, ne název SHP – `vrstva` drží jméno vrstvy."""
    for feat in gj.get("features") or []:
        props = feat.get("properties")
        if not isinstance(props, dict):
            props = {}
            feat["properties"] = props
        props["vrstva"] = layer_name
    return gj


def _ogr2ogr_shp(
    ogr2ogr: str,
    geojson: Path,
    shp: Path,
    clip_5514: tuple[float, float, float, float],
) -> None:
    xmin, ymin, xmax, ymax = clip_5514
    cmd = [
        ogr2ogr,
        "-f",
        "ESRI Shapefile",
        "-overwrite",
        "-s_srs",
        "EPSG:5514",
        "-t_srs",
        "EPSG:5514",
        "-clipsrc",
        str(xmin),
        str(ymin),
        str(xmax),
        str(ymax),
        "-lco",
        "ENCODING=UTF-8",
        "-nlt",
        "PROMOTE_TO_MULTI",
        str(shp),
        str(geojson),
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        env=gis_subprocess_env(ogr2ogr),
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "ogr2ogr failed").strip()
        raise FetchError(f"ogr2ogr {shp.stem}: {err[:400]}")


def _http_json(url: str, timeout: int) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout or DOWNLOAD_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} při stahování ZABAGED") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"Síťová chyba (ZABAGED): {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FetchError("ArcGIS ZABAGED vrátil neplatný JSON") from exc
