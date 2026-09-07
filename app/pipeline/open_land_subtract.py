"""Masky pro odečet od KP 401 (ZABAGED louka/zeleň + OSM orná)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from app.pipeline.geom_diff import rings_to_polygon_wkb
from app.pipeline.oom_import import _extract_shp_from_zip

# Plochy, které se do OOM kreslí jinak než KP žlutá – z KP 401 se odečtou.
_SUBTRACT_ZABAGED_LAYERS = (
    "TrvalyTravniPorost",
    "UdrzovanaZelen",
)


def collect_kp401_subtract_wkbs(
    *,
    zabaged_clean: Path | None,
    work_dir: Path,
) -> list[bytes]:
    out: list[bytes] = []
    if zabaged_clean is not None and zabaged_clean.is_file():
        out.extend(_zabaged_layer_wkbs(zabaged_clean, work_dir, _SUBTRACT_ZABAGED_LAYERS))
    out.extend(_osm_farmland_wkbs(work_dir))
    return out


def _zabaged_layer_wkbs(
    zabaged_clean: Path,
    work_dir: Path,
    layers: tuple[str, ...],
) -> list[bytes]:
    stage = work_dir / "_kp401_subtract"
    stage.mkdir(parents=True, exist_ok=True)
    wanted = set(layers)
    out: list[bytes] = []
    with zipfile.ZipFile(zabaged_clean) as zf:
        shp_names = [
            Path(n).name
            for n in zf.namelist()
            if n.lower().endswith(".shp") and Path(n).stem in wanted
        ]
    for shp_name in shp_names:
        layer = Path(shp_name).stem
        shp = _extract_shp_from_zip(zabaged_clean, shp_name, stage / layer)
        if not shp:
            continue
        out.extend(_shp_wkbs(shp))
    return out


def _shp_wkbs(shp: Path) -> list[bytes]:
    """WKB z SHP – v Docker image je osgeo/GDAL, pyogrio tam není."""
    try:
        from osgeo import ogr
    except ImportError:
        ogr = None
    if ogr is not None:
        ds = ogr.Open(str(shp))
        if ds:
            layer = ds.GetLayer(0)
            if layer is not None:
                out: list[bytes] = []
                for feature in layer:
                    geom = feature.GetGeometryRef()
                    if geom is None:
                        continue
                    out.append(bytes(geom.ExportToWkb()))
                return out
    try:
        from app.pipeline.oom_import import _pyogrio_layer_rows
    except ImportError:
        return []
    try:
        return [bytes(wkb) for _props, wkb in _pyogrio_layer_rows(shp) if wkb]
    except ImportError:
        return []


def _osm_farmland_wkbs(work_dir: Path) -> list[bytes]:
    gj = work_dir / "osm_paths" / "features.geojson"
    if not gj.is_file():
        return []
    try:
        data = json.loads(gj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[bytes] = []
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        if str(props.get("kind") or "") != "farmland":
            continue
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Polygon":
            continue
        raw_rings = geom.get("coordinates") or []
        if not raw_rings:
            continue
        rings = [[(float(x), float(y)) for x, y in ring] for ring in raw_rings]
        wkb = rings_to_polygon_wkb(rings)
        if wkb:
            out.append(wkb)
    return out
