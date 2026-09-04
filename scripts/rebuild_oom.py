#!/usr/bin/env python3
"""Rychlé přegenerování podkladarna.omap z existujícího výstupního ZIPu / složky.

Nepotřebuje LiDAR, Karttapullautin ani stahování dat – jen:
  basemap/pullautus.png + .pgw
  vectors/*.shp (+ .dbf, .shx, …)
  karttapullautin/*.dxf (volitelně)

Příklad:
  python scripts/rebuild_oom.py D:\\Downloads\\podkladarna_48a22529376d
  python scripts/rebuild_oom.py D:\\Downloads\\podkladarna_48a22529376d -o out.omap --refs
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pipeline.crs_5514 import projected_to_wgs84
from app.pipeline.georef import png_pixel_size, read_pgw
from app.pipeline.ini_builder import load_presets
from app.pipeline.karttapullautin_dxf import DXF_PRODUCTS
from app.pipeline.package_oom import prepare_oom_map

# Název v ZIPu → soubor v temp/ pro collect_dxf_for_zip
_DXF_ZIP_TO_TEMP = {zip_name: src_name for src_name, zip_name in DXF_PRODUCTS}


def _bbox_wgs84_from_raster(png: Path, pgw: Path) -> tuple[float, float, float, float]:
    georef = read_pgw(pgw)
    width, height = png_pixel_size(png)
    xs = (
        georef.origin_x,
        georef.origin_x + width * georef.pixel_x,
    )
    ys = (
        georef.origin_y,
        georef.origin_y + height * georef.pixel_y,
    )
    lats: list[float] = []
    lons: list[float] = []
    for x in xs:
        for y in ys:
            lat, lon = projected_to_wgs84(x, y)
            lats.append(lat)
            lons.append(lon)
    return min(lons), min(lats), max(lons), max(lats)


def _zip_vectors(vectors_dir: Path, dest_zip: Path) -> None:
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    if dest_zip.exists():
        dest_zip.unlink()
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(vectors_dir.iterdir()):
            if path.is_file():
                zf.write(path, path.name)


def _stage_from_output(src: Path, work: Path, *, include_refs: bool) -> dict:
    if not src.is_dir():
        raise FileNotFoundError(f"Složka neexistuje: {src}")

    meta_path = src / "metadata.json"
    meta = (
        json.loads(meta_path.read_text(encoding="utf-8"))
        if meta_path.is_file()
        else {}
    )

    png_src = src / "basemap" / "pullautus.png"
    pgw_src = src / "basemap" / "pullautus.pgw"
    if not png_src.is_file() or not pgw_src.is_file():
        raise FileNotFoundError("Chybí basemap/pullautus.png nebo .pgw")

    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    shutil.copy2(png_src, work / "pullautus.png")
    shutil.copy2(pgw_src, work / "pullautus.pgw")
    depr = src / "relief" / "pullautus_depr.png"
    if depr.is_file():
        shutil.copy2(depr, work / "pullautus_depr.png")
        depr_pgw = src / "relief" / "pullautus_depr.pgw"
        if depr_pgw.is_file():
            shutil.copy2(depr_pgw, work / "pullautus_depr.pgw")

    vectors = src / "vectors"
    zabaged_zip = work / "zabaged_clean.zip"
    if vectors.is_dir() and any(vectors.glob("*.shp")):
        _zip_vectors(vectors, zabaged_zip)
    else:
        zabaged_zip = None

    dxf_src = src / "karttapullautin"
    if dxf_src.is_dir():
        temp = work / "temp"
        temp.mkdir()
        for path in sorted(dxf_src.glob("*.dxf")):
            temp_name = _DXF_ZIP_TO_TEMP.get(path.name)
            if temp_name and path.name != "contours.dxf":
                shutil.copy2(path, temp / temp_name)

    contours_src = src / "contours"
    if contours_src.is_dir():
        dest_c = work / "contours"
        dest_c.mkdir(exist_ok=True)
        for path in contours_src.iterdir():
            if path.is_file():
                shutil.copy2(path, dest_c / path.name)

    osm_src = src / "osm_paths"
    if osm_src.is_dir():
        dest_o = work / "osm_paths"
        dest_o.mkdir(exist_ok=True)
        gj = osm_src / "paths.geojson"
        if gj.is_file():
            shutil.copy2(gj, dest_o / "paths.geojson")

    built_refs: dict[str, Path] | None = None
    if include_refs:
        refs_src = src / "references"
        if refs_src.is_dir():
            refs_dst = work / "references"
            refs_dst.mkdir()
            for png in refs_src.glob("*.png"):
                shutil.copy2(png, refs_dst / png.name)
                pgw = png.with_suffix(".pgw")
                if pgw.is_file():
                    shutil.copy2(pgw, refs_dst / pgw.name)
            # Klíče odpovídají collect_oom_templates / build_reference_layers
            key_map = {
                "orthophoto.png": "orthophoto",
                "osm.png": "osm",
                "mapa_ztm.png": "ztm",
                "katastr.png": "katastr",
                "dmpok_nahled.png": "dmpok",
                "hillshade_dmr5g.png": "hillshade",
                "hillshade_dmr5g_z10.png": "hillshade_z10",
                "hillshade_dmr5g_z20.png": "hillshade_z20",
            }
            built_refs = {}
            for filename, key in key_map.items():
                p = refs_dst / filename
                if p.is_file():
                    built_refs[key] = p

    meta["_work"] = work
    meta["_zabaged"] = zabaged_zip
    meta["_built_refs"] = built_refs
    return meta


def rebuild_oom(
    src: Path,
    dest: Path,
    *,
    include_refs: bool = False,
    work_dir: Path | None = None,
) -> Path:
    work = work_dir or (src / "_rebuild_work")
    meta = _stage_from_output(src, work, include_refs=include_refs)

    preset_id = meta.get("preset_id", "sprint_2m")
    preset = load_presets().get(preset_id, {})
    scale = int(meta.get("scale") or round(float(preset.get("scalefactor", 0.4)) * 10000))
    vectorconf = Path(str(preset.get("vectorconf", "zabaged.txt"))).name

    png = work / "pullautus.png"
    pgw = work / "pullautus.pgw"
    bbox = _bbox_wgs84_from_raster(png, pgw)

    out = prepare_oom_map(
        work,
        dest,
        map_name=meta.get("name", preset_id),
        scale=scale,
        preset_id=preset_id,
        bbox_wgs84=bbox,
        built_refs=meta.get("_built_refs"),
        zabaged_clean=meta.get("_zabaged"),
        vectorconf_name=vectorconf,
        include_dxf=True,
        contour_interval_m=meta.get("contour_interval_m"),
        formline=float(meta.get("formline") or preset.get("formline") or 0),
        indexcontours_m=preset.get("indexcontours"),
    )
    if out is None:
        raise RuntimeError("prepare_oom_map nevrátil soubor – chybí šablony?")
    try:
        from osgeo import ogr  # noqa: F401
    except ImportError:
        try:
            import pyogrio.raw  # noqa: F401
        except ImportError:
            print(
                "Varování: chybí GDAL (osgeo) i pyogrio – .omap bude bez vektorových objektů.",
                file=sys.stderr,
            )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source",
        type=Path,
        help="Rozbalená složka výstupního ZIPu (s basemap/, vectors/, …)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Cílový .omap (výchozí: <source>/podkladarna.omap)",
    )
    parser.add_argument(
        "--refs",
        action="store_true",
        help="Zahrnout referenční PNG ze references/ (pomalejší, větší .omap)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="Pracovní složka (výchozí: <source>/_rebuild_work)",
    )
    parser.add_argument(
        "--keep-work",
        action="store_true",
        help="Nesmazat pracovní složku po dokončení",
    )
    args = parser.parse_args()

    src = args.source.resolve()
    dest = (args.output or (src / "podkladarna.omap")).resolve()
    work = args.work_dir.resolve() if args.work_dir else None

    out = rebuild_oom(src, dest, include_refs=args.refs, work_dir=work)
    print(f"OK: {out}")
    if not args.keep_work and work is None:
        staged = src / "_rebuild_work"
        if staged.is_dir():
            shutil.rmtree(staged)


if __name__ == "__main__":
    main()
