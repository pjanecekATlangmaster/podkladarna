#!/usr/bin/env python3
"""Lokální přegenerování cesty_osm.omap: Overpass + nová klasifikace silnic, bez Docker/KP.

Použije rozbalený výstup Podkladárny (basemap + zabaged), stáhne OSM znovu,
aplikuje refine road_1..4 a sestaví OOM s path_source=osm.

Příklad:
  py -3 scripts/refresh_osm_roads_oom.py C:\\Users\\PetrJanecek\\Downloads\\podkladarna-barrandov
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rebuild_oom import _bbox_wgs84_from_raster, _stage_from_output
from app.pipeline.ini_builder import load_presets
from app.pipeline.osm_paths import PATH_SOURCE_OSM, prepare_osm_paths
from app.pipeline.package_oom import prepare_oom_map


def _log(msg: str) -> None:
    print(msg, flush=True)


def refresh_osm_roads(
    src: Path,
    dest: Path,
    *,
    work_dir: Path | None = None,
) -> Path:
    work = work_dir or (ROOT / "local_test" / "_barrandov_roads_work")
    if work.exists():
        shutil.rmtree(work)
    meta = _stage_from_output(src, work, include_refs=False)
    zabaged = meta.get("_zabaged")
    png = work / "pullautus.png"
    pgw = work / "pullautus.pgw"
    bbox = _bbox_wgs84_from_raster(png, pgw)

    preset_id = meta.get("preset_id", "sprint_2m")
    preset = load_presets().get(preset_id, {})
    scale = int(meta.get("scale") or round(float(preset.get("scalefactor", 0.4)) * 10000))
    vectorconf = Path(str(preset.get("vectorconf", "zabaged.txt"))).name

    _log(f"OSM refresh bbox={bbox} preset={preset_id}")
    prepare_osm_paths(
        work,
        bbox,
        zabaged if isinstance(zabaged, Path) else None,
        preset_id=preset_id,
        log=_log,
    )

    paths_gj = work / "osm_paths" / "paths_osm.geojson"
    if paths_gj.is_file():
        data = json.loads(paths_gj.read_text(encoding="utf-8"))
        by_hw: dict[str, int] = defaultdict(int)
        for feat in data.get("features") or []:
            hw = str((feat.get("properties") or {}).get("highway") or "?")
            by_hw[hw] += 1
        roads = {k: v for k, v in sorted(by_hw.items()) if k.startswith("road_")}
        _log(f"paths_osm road_* : {roads or '—'}")
        _log(
            "paths_osm all: "
            + ", ".join(f"{k}={v}" for k, v in sorted(by_hw.items())[:20])
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    out = prepare_oom_map(
        work,
        dest,
        map_name=meta.get("name", preset_id),
        scale=scale,
        preset_id=preset_id,
        bbox_wgs84=bbox,
        zabaged_clean=zabaged if isinstance(zabaged, Path) else None,
        vectorconf_name=vectorconf,
        include_dxf=True,
        contour_interval_m=meta.get("contour_interval_m"),
        formline=float(meta.get("formline") or preset.get("formline") or 0),
        indexcontours_m=preset.get("indexcontours"),
        path_source=PATH_SOURCE_OSM,
    )
    if out is None:
        raise RuntimeError("prepare_oom_map nevrátil soubor – chybí šablony?")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source",
        type=Path,
        nargs="?",
        default=Path(r"C:\Users\PetrJanecek\Downloads\podkladarna-barrandov"),
        help="Rozbalený výstup (výchozí: Downloads/podkladarna-barrandov)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=ROOT / "local_test" / "barrandov_roads_osm.omap",
        help="Cílový .omap",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="Pracovní složka (výchozí: local_test/_barrandov_roads_work)",
    )
    parser.add_argument(
        "--keep-work",
        action="store_true",
        help="Nesmazat pracovní složku",
    )
    args = parser.parse_args()
    src = args.source.resolve()
    if not src.is_dir():
        raise SystemExit(f"Složka neexistuje: {src}")

    work = args.work_dir.resolve() if args.work_dir else None
    out = refresh_osm_roads(src, args.output.resolve(), work_dir=work)
    print(f"OK: {out}")
    if not args.keep_work and work is None:
        staged = ROOT / "local_test" / "_barrandov_roads_work"
        # nechat work pro kontrolu geojson – smaž jen když uživatel nechce keep
        # default keep work under local_test for inspection
        _log(f"work ponechán: {staged}")


if __name__ == "__main__":
    main()
