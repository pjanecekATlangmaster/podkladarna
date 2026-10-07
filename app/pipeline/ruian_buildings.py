"""RÚIAN budovy → SHP podklad do ZIPu (zabaged/)."""

from __future__ import annotations

import json
from pathlib import Path

# ZABAGED vrstvy budov – v OOM nahrazuje OSM; v ZIPu zůstanou ve zabaged/ (+ RÚIAN SHP).
ZABAGED_OMIT_BUILDING_LAYERS = frozenset(
    {
        "BudovaJednotlivaNeboBlokBudov",
        "KulnaSklenikFoliovnikPristresek",
        "StavebniObjektZakryty",
        "Hrad",
        "Zamek",
    }
)


def write_ruian_buildings_shapefile(
    geojson_path: Path,
    dest_dir: Path,
    *,
    log=None,
) -> Path | None:
    """RÚIAN GeoJSON → SHP do dest_dir (např. pro zabaged/RUIAN_budovy.shp)."""
    if not geojson_path.is_file():
        return None
    try:
        data = json.loads(geojson_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    feats = list(data.get("features") or [])
    if not feats:
        return None
    from app.pipeline.osm_paths import _geojson_to_shapefile
    from app.pipeline.prepare_lidar import log_step

    dest_dir.mkdir(parents=True, exist_ok=True)
    shp = dest_dir / "RUIAN_budovy.shp"
    log_step(log, "Převádím budovy RÚIAN na shapefile (obrysy domů do mapy)")
    ok = _geojson_to_shapefile(
        feats, shp, nlt="POLYGON", log=log, label="RÚIAN budovy"
    )
    if ok and log:
        log(f"RÚIAN budovy→SHP: {len(feats)} polygonů → {dest_dir.name}/")
    return shp if ok else None


