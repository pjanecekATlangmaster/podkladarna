#!/usr/bin/env python3
"""Vzorek: sprint residual paved na posledním buildu Černý Most.

Rozbalí Downloads/podkladarna-*-most.zip, znovu stáhne OSM (vč. residential),
přegeneruje .omap s ``sprint_residual_paved``.

  python -u scripts/residual_cerny_most_sample.py
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rebuild_oom import _bbox_wgs84_from_raster, _stage_from_output
from app.pipeline.georef import projected_center_from_raster
from app.pipeline.ini_builder import load_presets
from app.pipeline.oom_symbol_map import symbol_index_for_code
from app.pipeline.osm_paths import prepare_osm_paths
from app.pipeline.package_oom import prepare_oom_map
from app.pipeline.residual_paved import _PART_NAME, build_residual_paved_parts


def _find_cerny_most_zip() -> Path:
    downloads = Path.home() / "Downloads"
    hits = sorted(
        downloads.glob("podkladarna*most*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not hits:
        hits = [
            p
            for p in downloads.glob("podkladarna*.zip")
            if "most" in p.name.lower() or "ern" in p.name.lower()
        ]
        hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not hits:
        raise FileNotFoundError(
            f"Nenalezen ZIP Černý Most v {downloads} (podkladarna*most*.zip)"
        )
    return hits[0]


def main() -> None:
    zip_path = _find_cerny_most_zip()
    out_dir = ROOT / "tmp" / "cerny_most_residual"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    extract = out_dir / "src"
    extract.mkdir()
    print(f"ZIP: {zip_path}", flush=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract)
    children = [p for p in extract.iterdir() if p.is_dir()]
    src = (
        children[0]
        if len(children) == 1
        and not (extract / "basemap").is_dir()
        and not (extract / "kp").is_dir()
        else extract
    )
    if not (src / "basemap").is_dir() and not (src / "kp").is_dir():
        for cand in extract.rglob("pullautus.png"):
            src = cand.parent.parent
            break
    print(f"Zdroj: {src}", flush=True)

    work = out_dir / "work"
    meta = _stage_from_output(src, work, include_refs=False)
    png = work / "pullautus.png"
    pgw = work / "pullautus.pgw"
    bbox = _bbox_wgs84_from_raster(png, pgw)
    print(f"BBOX WGS84: {bbox}", flush=True)

    prepare_osm_paths(
        work,
        bbox,
        meta.get("_zabaged"),
        osm_priority=True,
        preset_id="sprint_2m",
        log=lambda msg: print(msg, flush=True),
    )

    feats_path = work / "osm_paths" / "features.geojson"
    resid_count = 0
    if feats_path.is_file():
        feats = json.loads(feats_path.read_text(encoding="utf-8")).get("features") or []
        by_kind: dict[str, int] = {}
        for f in feats:
            k = str((f.get("properties") or {}).get("kind") or "?")
            by_kind[k] = by_kind.get(k, 0) + 1
        resid_count = by_kind.get("residential", 0)
        print(
            "OSM features: "
            + ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())),
            flush=True,
        )
    if resid_count == 0:
        print("VAROVÁNÍ: v výřezu není landuse=residential.", flush=True)

    preset_id = str(meta.get("preset_id") or "sprint_2m")
    if not preset_id.startswith("sprint"):
        preset_id = "sprint_2m"
    preset = load_presets().get(preset_id, {})
    scale = int(
        meta.get("scale")
        or round(float(preset.get("scalefactor", 0.4)) * 10000)
    )
    vectorconf = Path(str(preset.get("vectorconf", "zabaged.txt"))).name
    ref_x, ref_y = projected_center_from_raster(png, pgw)

    preview = build_residual_paved_parts(
        work,
        preset_id=preset_id,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        grivation_deg=0.0,
    )
    if preview:
        print(
            f"Residual preview: {preview[0].count} objektů ({_PART_NAME})",
            flush=True,
        )
    else:
        print("Residual preview: 0 objektů", flush=True)

    dest = out_dir / "cerny_most_residual.omap"
    out = prepare_oom_map(
        work,
        dest,
        map_name="Černý Most residual 501",
        scale=scale,
        preset_id=preset_id,
        bbox_wgs84=bbox,
        zabaged_clean=meta.get("_zabaged"),
        vectorconf_name=vectorconf,
        include_dxf=True,
        contour_interval_m=meta.get("contour_interval_m") or 2.5,
        residual_paved=True,
        courtyard_olive=True,
    )
    if out is None:
        raise SystemExit("prepare_oom_map selhalo")
    xml = out.read_text(encoding="utf-8")
    m = re.search(r'<objects count="(\d+)">', xml)
    obj_n = int(m.group(1)) if m else 0
    si = symbol_index_for_code(preset_id, scale, "501")
    n501 = xml.count(f'symbol="{si}"') if si is not None else 0
    print(f"OK: {out.resolve()}", flush=True)
    print(f"Objekty v Mapa: {obj_n}; symbol 501: {n501}", flush=True)
    print("Otevři v OOM – zkontroluj černé obrysy silnic nad šedou 501.", flush=True)


if __name__ == "__main__":
    main()
