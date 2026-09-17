"""RÚIAN budovy → OOM objekty (521 / MTBO 526) + oliva dvorů z děr."""

from __future__ import annotations

import json
from pathlib import Path

from app.pipeline.geom_clip import Bounds, clip_ring
from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import (
    OomObjectPart,
    _area_object_with_holes,
    _hole_rings_as_area_objects,
)
from app.pipeline.oom_symbol_map import symbol_index_for_code

# ZABAGED vrstvy budov – v OOM je nahrazuje RÚIAN; v ZIPu zůstanou ve zabaged/ mezi ostatními.
ZABAGED_OMIT_BUILDING_LAYERS = frozenset(
    {
        "BudovaJednotlivaNeboBlokBudov",
        "KulnaSklenikFoliovnikPristresek",
        "StavebniObjektZakryty",
        "Hrad",
        "Zamek",
    }
)


def _geojson_polygon_parts(geom: dict) -> list | None:
    """GeoJSON Polygon/MultiPolygon → parts jako WKB (line+close / hole)."""
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    rings_list: list[list[list[tuple[float, float]]]] = []
    if gtype == "Polygon":
        if not coords:
            return None
        rings_list.append(
            [[(float(x), float(y)) for x, y in ring] for ring in coords]
        )
    elif gtype == "MultiPolygon":
        for poly in coords:
            if not poly:
                continue
            rings_list.append(
                [[(float(x), float(y)) for x, y in ring] for ring in poly]
            )
    else:
        return None

    parts: list = []
    for rings in rings_list:
        if not rings or len(rings[0]) < 3:
            continue
        parts.append(("line", rings[0], True))
        for hole in rings[1:]:
            if len(hole) >= 3:
                parts.append(("hole", hole))
    return parts or None


def build_ruian_building_parts(
    geojson_path: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    clip_bounds: Bounds | None = None,
    courtyard_olive: bool = False,
) -> list[OomObjectPart]:
    """Budovy z RÚIAN GeoJSON → OOM 521 (les/sprint) / 526 (MTBO)."""
    if not geojson_path.is_file():
        return []
    try:
        data = json.loads(geojson_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    mtbo = preset_id.startswith("mtbo")
    code = "526" if mtbo else "521"
    symbol_index = symbol_index_for_code(preset_id, scale, code)
    if symbol_index is None:
        return []

    olive_code = "527" if mtbo else "520"
    olive_index = (
        symbol_index_for_code(preset_id, scale, olive_code)
        if courtyard_olive
        else None
    )

    def to_map(pts: list[tuple[float, float]]):
        return [
            projected_to_map_coord(
                x,
                y,
                ref_x=ref_x,
                ref_y=ref_y,
                scale=scale,
                grivation_deg=grivation_deg,
            )
            for x, y in pts
        ]

    objects: list[str] = []
    courtyard_objects: list[str] = []

    for feat in data.get("features") or []:
        geom = feat.get("geometry") or {}
        parts = _geojson_polygon_parts(geom)
        if not parts:
            continue
        # Seskupit polygony (MultiPolygon): každý outer + jeho holes.
        i = 0
        while i < len(parts):
            if parts[i][0] != "line":
                i += 1
                continue
            outer = list(parts[i][1])
            i += 1
            holes: list[list[tuple[float, float]]] = []
            while i < len(parts) and parts[i][0] == "hole":
                holes.append(list(parts[i][1]))
                i += 1
            if clip_bounds is not None:
                outer = clip_ring(outer, clip_bounds)
                if len(outer) < 3:
                    continue
                holes = [
                    h
                    for h in (clip_ring(h, clip_bounds) for h in holes)
                    if len(h) >= 3
                ]
            rings_map = [to_map(outer)] + [to_map(h) for h in holes]
            obj = _area_object_with_holes(symbol_index, rings_map)
            if obj:
                objects.append(obj)
            if olive_index is not None and holes:
                hole_parts: list = [("line", outer, True)]
                for h in holes:
                    hole_parts.append(("hole", h))
                courtyard_objects.extend(
                    _hole_rings_as_area_objects(
                        hole_parts,
                        olive_index,
                        ref_x=ref_x,
                        ref_y=ref_y,
                        scale=scale,
                        grivation_deg=grivation_deg,
                        clip_bounds=None,  # už oříznuté
                    )
                )

    out: list[OomObjectPart] = []
    if objects:
        out.append(
            OomObjectPart(
                name="RÚIAN – budovy",
                objects_xml="\n".join(objects),
                count=len(objects),
            )
        )
    if courtyard_objects:
        out.append(
            OomObjectPart(
                name="RÚIAN – dvory (oliva)",
                objects_xml="\n".join(courtyard_objects),
                count=len(courtyard_objects),
            )
        )
    return out
