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
from app.download_cache import is_fresh, read_meta, write_meta, zabaged_cache_dir
from app.pipeline.crs_5514 import write_prj
from app.pipeline.fetch_openzu import (
    DOWNLOAD_TIMEOUT_S,
    FetchError,
    QUERY_TIMEOUT_S,
    USER_AGENT,
    VECTOR_FETCH_BUFFER_M,
    crop_bounds_5514,
    expand_bbox_wgs84,
)
from app.tool_env import gis_subprocess_env, which_tool

PAGE_SIZE = 2000
MAX_PAGES = 25
# Do auto .omap defaultně jen menší plochy (~5 ha). Střední/velké volitelně z GUI.
MAX_OSTATNI_PLOCHA_M2 = 50_000.0
OSTATNI_MEDIUM_MAX_M2 = 500_000.0
# Při stahování zahodit jen absurdní celoměstské km² – SHP pásma potřebují i velké.
MAX_OSTATNI_FETCH_M2 = 1_000_000.0
OSTATNI_PLOCHA_CHOICES = frozenset({"none", "small", "medium", "large"})
# ZIP zdroje: tři SHP podle velikosti (prázdné pásmo se nevygeneruje).
OSTATNI_SHP_BANDS: tuple[tuple[str, float, float], ...] = (
    ("OstatniPlochaVSidlech_mensi", 0.0, MAX_OSTATNI_PLOCHA_M2),
    ("OstatniPlochaVSidlech_stredni", MAX_OSTATNI_PLOCHA_M2, OSTATNI_MEDIUM_MAX_M2),
    ("OstatniPlochaVSidlech_velke", OSTATNI_MEDIUM_MAX_M2, float("inf")),
)
OSTATNI_LAYER_STEM = "OstatniPlochaVSidlech"


def resolve_ostatni_plocha(options: dict | None) -> str:
    raw = str((options or {}).get("ostatni_plocha") or "small").strip().lower()
    return raw if raw in OSTATNI_PLOCHA_CHOICES else "small"


def ostatni_plocha_max_m2(choice: str) -> float | None:
    """Horní limit plochy do auto .omap. None = vrstvu vůbec nekreslit."""
    c = (choice or "small").strip().lower()
    if c == "none":
        return None
    if c == "medium":
        return OSTATNI_MEDIUM_MAX_M2
    if c == "large":
        return float("inf")
    return MAX_OSTATNI_PLOCHA_M2


def _ags_config() -> dict:
    path = settings.CONFIG_DIR / "zabaged_ags.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def fetch_zabaged_for_bbox(
    bbox: tuple[float, float, float, float],
    log: callable | None = None,
) -> Path:
    """Stáhne ZABAGED pro WGS84 bbox (+ VECTOR_FETCH_BUFFER_M) do sdílené cache."""
    cfg_path = settings.CONFIG_DIR / "zabaged_ags.yaml"
    fetch_bbox = expand_bbox_wgs84(bbox, VECTOR_FETCH_BUFFER_M)
    cache_dir = zabaged_cache_dir(fetch_bbox, cfg_path)
    dest_zip = cache_dir / "Zabaged_ags.zip"
    if is_fresh(cache_dir, dest_zip, settings.ZABAGED_CACHE_MAX_AGE_DAYS, min_size=500):
        if log:
            meta = read_meta(cache_dir) or {}
            age = (meta.get("downloaded_at") or "?")[:10]
            log(f"ZABAGED cache ({cache_dir.name}, staženo {age})")
        return dest_zip

    ogr2ogr = which_tool("ogr2ogr")
    if not ogr2ogr:
        raise FetchError(
            "ogr2ogr (GDAL) není v PATH – nejde sestavit ZABAGED shapefile. "
            "Windows: OSGeo4W, nebo docker compose -f docker-compose.dev.yml up"
        )

    west, south, east, north = fetch_bbox
    xmin, ymin, xmax, ymax = crop_bounds_5514(
        west, south, east, north, buffer_m=0.0
    )
    cfg = _ags_config()
    service = cfg["service"].rstrip("/")
    layers: dict[str, int] = cfg["layers"]

    stage = cache_dir / "_stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    kept = 0
    try:
        for name, layer_id in layers.items():
            gj = query_layer_geojson(service, int(layer_id), west, south, east, north)
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
            write_prj(shp)
            geojson_path.unlink(missing_ok=True)
            # Ostatní plocha: filtr plochy až PO ořezu (Shape_Area je z celého zdroje).
            if name == "OstatniPlochaVSidlech":
                n_dropped = _drop_oversized_ostatni_shp(
                    shp, max_area_m2=MAX_OSTATNI_FETCH_M2
                )
                if log and n_dropped:
                    log(
                        f"  OstatniPlochaVSidlech: vynechano {n_dropped} "
                        f"obrich polygonu (po ořezu > {MAX_OSTATNI_FETCH_M2:g} m²)"
                    )
                n = _shp_feature_count(shp)
                if n <= 0:
                    if log:
                        log(f"  skip (prazdne): {name}")
                    for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                        (stage / f"{name}{ext}").unlink(missing_ok=True)
                    continue
            if log:
                log(f"  OK {name}: {n} prvku")
            kept += 1
        if kept == 0:
            raise FetchError("ZABAGED v tomto výřezu nemá žádné použitelné vrstvy")

        cache_dir.mkdir(parents=True, exist_ok=True)
        if dest_zip.exists():
            dest_zip.unlink()
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in stage.iterdir():
                if f.is_file() and f.suffix.lower() != ".geojson":
                    zf.write(f, f.name)
        write_meta(
            cache_dir,
            source="zabaged_ags",
            bbox_wgs84=list(fetch_bbox),
            layers=kept,
        )
        if log:
            log(
                f"ZABAGED: {kept} vrstev (+{VECTOR_FETCH_BUFFER_M:.0f} m přesah), "
                f"{dest_zip.stat().st_size / 1e3:.0f} kB"
            )
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


def feature_area_m2(props: dict, geom_area_m2: float | None = None) -> float | None:
    """Plocha pro pásma/filtr: po ořezu má přednost geometrie, ne Shape_Area zdroje.

    ``Shape_Area`` z ArcGIS je plocha celého ZABAGED polygonu (často km² sídliště),
    zatímco do výřezu zasahuje jen malý kousek.
    """
    if geom_area_m2 is not None and geom_area_m2 > 0:
        return float(geom_area_m2)
    area = props.get("Shape_Area", props.get("shape_area"))
    if isinstance(area, (int, float)) and area > 0:
        return float(area)
    return None


def ostatni_band_stem(area_m2: float) -> str:
    """Stem SHP pásma: mensi ≤5 ha, stredni ≤50 ha, jinak velke."""
    if area_m2 <= MAX_OSTATNI_PLOCHA_M2:
        return "OstatniPlochaVSidlech_mensi"
    if area_m2 <= OSTATNI_MEDIUM_MAX_M2:
        return "OstatniPlochaVSidlech_stredni"
    return "OstatniPlochaVSidlech_velke"


def drop_oversized_ostatni_plocha(
    gj: dict,
    max_area_m2: float = MAX_OSTATNI_PLOCHA_M2,
) -> dict:
    """Zahodí polygony nad limitem (preferuje plochu geometrie před Shape_Area)."""
    kept = []
    for feat in gj.get("features") or []:
        if ostatni_plocha_too_large(
            feat.get("properties") or {},
            geom_area_m2=_geojson_area_m2(feat.get("geometry")),
            max_area_m2=max_area_m2,
        ):
            continue
        kept.append(feat)
    gj["features"] = kept
    return gj


def ostatni_plocha_too_large(
    props: dict,
    *,
    geom_area_m2: float | None = None,
    max_area_m2: float = MAX_OSTATNI_PLOCHA_M2,
) -> bool:
    """True = polygon zahodit. Po ořezu má přednost ``geom_area_m2`` (ne Shape_Area)."""
    area = feature_area_m2(props, geom_area_m2)
    if area is None:
        return False
    return area > max_area_m2


def _geojson_area_m2(geometry: object) -> float | None:
    if not isinstance(geometry, dict):
        return None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return None

    def _ring_area(ring: list) -> float:
        if not ring or len(ring) < 3:
            return 0.0
        a = 0.0
        for i in range(len(ring) - 1):
            x1, y1 = float(ring[i][0]), float(ring[i][1])
            x2, y2 = float(ring[i + 1][0]), float(ring[i + 1][1])
            a += x1 * y2 - x2 * y1
        return abs(a) * 0.5

    try:
        if gtype == "Polygon":
            return _ring_area(coords[0])
        if gtype == "MultiPolygon":
            return sum(_ring_area(poly[0]) for poly in coords if poly)
    except (TypeError, ValueError, IndexError):
        return None
    return None


def tag_features_with_layer(gj: dict, layer_name: str) -> dict:
    """Karttapullautin matchuje atributy, ne název SHP – `vrstva` drží jméno vrstvy."""
    for feat in gj.get("features") or []:
        props = feat.get("properties")
        if not isinstance(props, dict):
            props = {}
            feat["properties"] = props
        props["vrstva"] = layer_name
    return gj


def _shp_feature_count(shp: Path) -> int:
    try:
        from osgeo import ogr
    except ImportError:
        return 0
    ds = ogr.Open(str(shp))
    if not ds:
        return 0
    layer = ds.GetLayer(0)
    n = layer.GetFeatureCount() if layer is not None else 0
    ds = None
    return int(n or 0)


def _drop_oversized_ostatni_shp(shp: Path, max_area_m2: float) -> int:
    """Smaže prvky SHP s ořezanou plochou nad limitem. Vrací počet smazaných."""
    try:
        from osgeo import ogr
    except ImportError:
        return 0
    ogr.UseExceptions()
    ds = ogr.Open(str(shp), 1)
    if not ds:
        return 0
    layer = ds.GetLayer(0)
    if layer is None:
        ds = None
        return 0
    doomed: list[int] = []
    for feat in layer:
        geom = feat.GetGeometryRef()
        area = float(geom.GetArea()) if geom is not None else 0.0
        if area > max_area_m2:
            doomed.append(int(feat.GetFID()))
    for fid in doomed:
        layer.DeleteFeature(fid)
    layer.SyncToDisk()
    ds = None
    return len(doomed)


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
