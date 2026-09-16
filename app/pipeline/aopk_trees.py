"""AOPK památné stromy → OOM 417 / MTBO 418."""

from __future__ import annotations

import json
from pathlib import Path

from app.pipeline.geom_clip import Bounds, point_inside
from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import OomObjectPart, _point_object
from app.pipeline.oom_symbol_map import symbol_index_for_code

# Dedup OSM landmark_tree v okolí AOPK bodu.
AOPK_OSM_TREE_DEDUP_M = 8.0


def build_aopk_tree_parts(
    geojson_path: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    clip_bounds: Bounds | None = None,
) -> list[OomObjectPart]:
    if not geojson_path.is_file():
        return []
    try:
        data = json.loads(geojson_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    code = "418" if preset_id.startswith("mtbo") else "417"
    symbol_index = symbol_index_for_code(preset_id, scale, code)
    if symbol_index is None:
        return []

    objects: list[str] = []
    for feat in data.get("features") or []:
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        x, y = float(coords[0]), float(coords[1])
        if clip_bounds is not None and not point_inside(x, y, clip_bounds):
            continue
        mx, my = projected_to_map_coord(
            x,
            y,
            ref_x=ref_x,
            ref_y=ref_y,
            scale=scale,
            grivation_deg=grivation_deg,
        )
        obj = _point_object(symbol_index, mx, my)
        if obj:
            objects.append(obj)

    if not objects:
        return []
    return [
        OomObjectPart(
            name="AOPK – památné stromy",
            objects_xml="\n".join(objects),
            count=len(objects),
        )
    ]


def load_aopk_tree_points(geojson_path: Path | None) -> list[tuple[float, float]]:
    """Body AOPK v EPSG:5514 pro dedup OSM landmark_tree."""
    if not geojson_path or not geojson_path.is_file():
        return []
    try:
        data = json.loads(geojson_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[tuple[float, float]] = []
    for feat in data.get("features") or []:
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) >= 2:
            out.append((float(coords[0]), float(coords[1])))
    return out


def filter_osm_landmark_trees_near_aopk(
    features: list[dict],
    aopk_points: list[tuple[float, float]],
    *,
    near_m: float = AOPK_OSM_TREE_DEDUP_M,
) -> tuple[list[dict], int]:
    """Zahodí OSM landmark_tree blízké AOPK bodu."""
    if not aopk_points:
        return features, 0
    kept: list[dict] = []
    dropped = 0
    near2 = near_m * near_m
    for feat in features:
        props = feat.get("properties") or {}
        if str(props.get("kind") or "") != "landmark_tree":
            kept.append(feat)
            continue
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Point":
            kept.append(feat)
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            dropped += 1
            continue
        x, y = float(coords[0]), float(coords[1])
        if any((x - ax) ** 2 + (y - ay) ** 2 <= near2 for ax, ay in aopk_points):
            dropped += 1
            continue
        kept.append(feat)
    return kept, dropped
