from __future__ import annotations

import json
import zipfile
from pathlib import Path

from app.pipeline.karttapullautin_dxf import collect_dxf_for_zip
from app.guide_text import ZIP_ABOUT_TXT
from app.pipeline.contours_gdal import build_gdal_contour_parts
from app.pipeline.osm_paths import build_osm_feature_parts, build_osm_path_parts
from app.pipeline.build_oom_map import write_oom_map
from app.pipeline.crs_5514 import projected_to_wgs84
from app.pipeline.fetch_openzu import crop_bounds_5514
from app.pipeline.geom_clip import expand
from app.pipeline.georef import projected_center_from_raster
from app.pipeline.oom_georef import oom_north_angles
from app.pipeline.oom_import import (
    OomObjectPart,
    build_dxf_object_part,
    build_zabaged_object_parts,
)
from app.pipeline.oom_layers import collect_oom_templates
from app.pipeline.open_land_subtract import collect_kp401_subtract_wkbs
from app.pipeline.reference_layers import reference_metadata
from app.pipeline.vegetation_gdal import build_vegetation_parts

OUTPUT_ZIP_NAME = "podkladarna_output.zip"
OOM_ZIP_NAME = "podkladarna_oom.zip"  # legacy – starší joby
OOM_MAP_NAME = "podkladarna.omap"
# Kolik nechat za objednanou hranicí, ať u kraje mapy nechybí kus prvku.
CLIP_MARGIN_M = 25.0
# Louka / parková zeleň pod KP vegetací – jinak 401 překryje hustníky z LiDARu.
_ZABAGED_UNDER_VEGETATION = frozenset(
    {
        "TrvalyTravniPorost",
        "UdrzovanaZelen",
    }
)
# Obdělávaná půda z OSM (412) taky pod KP – hustníky zůstanou navrch.
_OSM_UNDER_VEGETATION_MARK = "(412)"


def map_scale_from_scalefactor(scalefactor: float) -> int:
    return int(round(float(scalefactor) * 10000))


def oom_metadata(
    preset_id: str,
    preset: dict,
    options: dict,
    job_name: str = "",
    *,
    reference_layers: list[str] | None = None,
) -> dict:
    sf = float(options.get("scalefactor", preset.get("scalefactor", 1)))
    meta = {
        "name": job_name,
        "preset_id": preset_id,
        "label": preset.get("label", preset_id),
        "crs": "EPSG:5514",
        "scale": map_scale_from_scalefactor(sf),
        "scalefactor": sf,
        "contour_interval_m": options.get(
            "contour_interval", preset.get("contour_interval")
        ),
        "formline": options.get("formline", preset.get("formline")),
        **reference_metadata(),
    }
    if reference_layers:
        meta["reference_layers"] = reference_layers
    return meta


def oom_readme(meta: dict) -> str:
    scale = meta.get("scale") or "?"
    label = meta.get("label") or meta.get("preset_id") or ""
    interval = meta.get("contour_interval_m")
    interval_txt = f"{interval} m" if interval is not None else "?"
    refs = meta.get("reference_layers") or []
    ref_block = ""
    if refs:
        ref_block = (
            "\nReferenční podklady (složka references/)\n"
            "----------------------------------------\n"
            + "\n".join(f"- {name}" for name in refs)
            + "\n"
        )
    return (
        "Podkladárna – balíček pro OpenOrienteering Mapper\n"
        "=================================================\n\n"
        "Nejprve přečtěte CO_JE_PODKLADARNA.txt v kořeni ZIPu.\n\n"
        f"Typ mapy: {label}\n"
        f"Měřítko: 1:{scale}\n"
        f"Ekvidistance: {interval_txt}\n"
        "Souřadnicový systém: EPSG:5514 (S-JTSK / Křovák)\n\n"
        f"Stínovaný reliéf DMR 5G (ČÚZK WMS): základní, Z10 a Z20 ve složce references/.\n"
        "Mapové podklady: OpenStreetMap, Základní topografická mapa ČR (ZTM), katastrální mapa a náhled DMP OK.\n"
        f"{ref_block}\n"
        "Doporučený postup v OOM\n"
        "-----------------------\n"
        "1. Rozbalte celý ZIP do jedné složky. Otevřete podkladarna.omap.\n"
        "   Výchozí pohled: jen vektory (vrstevnice, zeleň, ZABAGED, srázy, …).\n"
        "   Vrstevnice (101/102) jsou zamčené (is_protected) – odemkni v panelu symbolů.\n"
        "2. PNG podklady (OSM, KP náhled, ortofoto, hillshade, …) zapněte dle potřeby\n"
        "   v Šablony → Nastavení šablon (Template Setup).\n"
        "3. Deprese: šablona „Karttapullautin deprese“.\n"
        "4. Shapefile ZABAGED (zabaged/) jsou v ZIPu pro ruční práci mimo OOM.\n"
        "   Vrstevnice PDAL/GDAL jsou v contours/;\n"
        "   zeleň KP (polygony) ve vegetation/;\n"
        "   srázy a knolíky z Karttapullautinu v karttapullautin/\n"
        "   a zároveň jako editovatelné objekty v mapě.\n"
        "   OSM pěšiny (geojson + shapefile OSM_cesty) v osm_paths/ a jako objekty 507.\n\n"
        "OCAD: soubor .omap neotevře – importujte DXF, SHP nebo georeferencované PNG+PGW.\n"
        "Nebo v OOM exportujte do formátu OCD (v8–12).\n\n"
        "Data: ČÚZK (DMR 5G, DMP OK, ZABAGED®, ortofoto), CC BY 4.0. "
        "OSM © přispěvatelé (ODbL). Při šíření mapy uveďte zdroj: ČÚZK, [rok].\n"
        "Reliéf a vegetace: Karttapullautin (GPL-3.0).\n"
    )


def _write_if_exists(zf: zipfile.ZipFile, src: Path, arcname: str) -> bool:
    if src.is_file():
        zf.write(src, arcname)
        return True
    return False


def _add_shapefiles_from_zip(zf: zipfile.ZipFile, src_zip: Path, dest_dir: str) -> int:
    n = 0
    keep = {".shp", ".shx", ".dbf", ".prj", ".cpg"}
    with zipfile.ZipFile(src_zip) as src:
        for info in src.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name
            if not name or name.startswith("."):
                continue
            if Path(name).suffix.lower() not in keep:
                continue
            data = src.read(info)
            zf.writestr(f"{dest_dir}/{name}", data)
            n += 1
    return n


def prepare_oom_map(
    kp_cwd: Path,
    dest: Path,
    *,
    map_name: str,
    scale: int,
    preset_id: str,
    bbox_wgs84: tuple[float, float, float, float],
    built_refs: dict[str, Path] | None = None,
    zabaged_clean: Path | None = None,
    vectorconf_name: str = "zabaged.txt",
    include_dxf: bool = True,
    contour_interval_m: float | None = None,
    formline: float = 0,
    indexcontours_m: float | None = None,
    cliff_symbol: str = "earth_bank",
) -> Path | None:
    del formline
    west, south, east, north = bbox_wgs84
    xmin, ymin, xmax, ymax = crop_bounds_5514(west, south, east, north)
    pullautus_png = kp_cwd / "pullautus.png"
    pullautus_pgw = kp_cwd / "pullautus.pgw"
    if pullautus_png.is_file() and pullautus_pgw.is_file():
        try:
            ref_x, ref_y = projected_center_from_raster(pullautus_png, pullautus_pgw)
        except ValueError:
            ref_x = (xmin + xmax) / 2
            ref_y = (ymin + ymax) / 2
    else:
        ref_x = (xmin + xmax) / 2
        ref_y = (ymin + ymax) / 2
    ref_lat, ref_lon = projected_to_wgs84(ref_x, ref_y)
    _, grivation = oom_north_angles(ref_x, ref_y)
    # KP i LiDAR se počítají na širším výřezu, než uživatel objednal. Bez ořezu
    # by se do mapy dostaly artefakty z okraje dat, kde už žádný LiDAR není.
    # xmin..ymax má v sobě CROP_BUFFER_M navíc, tady chceme přesný výběr.
    clip_bounds = expand(
        crop_bounds_5514(west, south, east, north, buffer_m=0.0), CLIP_MARGIN_M
    )
    templates = collect_oom_templates(
        kp_cwd,
        built_refs=built_refs,
        include_dxf=include_dxf,
        include_dxf_templates=False,
    )
    if not templates:
        return None

    object_parts: list[OomObjectPart] = []
    zabaged_under: list[OomObjectPart] = []
    zabaged_rest: list[OomObjectPart] = []
    if zabaged_clean and zabaged_clean.is_file():
        for part in build_zabaged_object_parts(
            zabaged_clean,
            vectorconf_name=vectorconf_name,
            preset_id=preset_id,
            scale=scale,
            ref_x=ref_x,
            ref_y=ref_y,
            grivation_deg=grivation,
            work_dir=kp_cwd.parent,
            clip_bounds=clip_bounds,
        ):
            layer = part.name.removeprefix("ZABAGED – ").strip()
            if layer in _ZABAGED_UNDER_VEGETATION:
                zabaged_under.append(part)
            else:
                zabaged_rest.append(part)

    # Louky/zeleň ze ZABAGED + OSM 412 pod KP (hustníky z LiDARu musí zůstat vidět).
    object_parts.extend(zabaged_under)
    osm_feat = build_osm_feature_parts(
        kp_cwd,
        preset_id=preset_id,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        grivation_deg=grivation,
        clip_bounds=clip_bounds,
    )
    osm_under: list[OomObjectPart] = []
    osm_feat_rest: list[OomObjectPart] = []
    for part in osm_feat:
        if _OSM_UNDER_VEGETATION_MARK in part.name:
            osm_under.append(part)
        else:
            osm_feat_rest.append(part)
    object_parts.extend(osm_under)
    # KP zeleň (+ 401 s odečtem ZABAGED/OSM ploch) pod vrstevnicemi.
    subtract_wkbs = collect_kp401_subtract_wkbs(
        zabaged_clean=zabaged_clean if zabaged_clean and zabaged_clean.is_file() else None,
        work_dir=kp_cwd,
    )
    object_parts.extend(
        build_vegetation_parts(
            kp_cwd,
            preset_id=preset_id,
            scale=scale,
            ref_x=ref_x,
            ref_y=ref_y,
            grivation_deg=grivation,
            subtract_wkbs=subtract_wkbs or None,
        )
    )
    object_parts.extend(
        build_gdal_contour_parts(
            kp_cwd,
            preset_id=preset_id,
            scale=scale,
            ref_x=ref_x,
            ref_y=ref_y,
            grivation_deg=grivation,
            interval_m=float(contour_interval_m or 5),
            formline=0,
            index_m=float(indexcontours_m) if indexcontours_m else None,
        )
    )
    if include_dxf:
        dxf_part = build_dxf_object_part(
            kp_cwd,
            preset_id=preset_id,
            scale=scale,
            ref_x=ref_x,
            ref_y=ref_y,
            grivation_deg=grivation,
            cliff_symbol=cliff_symbol,
            clip_bounds=clip_bounds,
        )
        if dxf_part:
            object_parts.append(dxf_part)
    object_parts.extend(zabaged_rest)
    osm_parts = build_osm_path_parts(
        kp_cwd,
        preset_id=preset_id,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        grivation_deg=grivation,
        clip_bounds=clip_bounds,
    )
    if osm_parts:
        object_parts.extend(osm_parts)
    if osm_feat_rest:
        object_parts.extend(osm_feat_rest)
    return write_oom_map(
        dest,
        map_name=map_name,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        ref_lat=ref_lat,
        ref_lon=ref_lon,
        templates=templates,
        object_parts=object_parts or None,
        preset_id=preset_id,
    )


def build_oom_zip(
    kp_cwd: Path,
    dest_zip: Path,
    *,
    zabaged_clean: Path | None,
    metadata: dict,
    reference_dir: Path | None = None,
    omap_path: Path | None = None,
    include_zabaged_archive: bool = False,
    include_png: bool = True,
    include_dxf: bool = True,
    include_cliffs: bool = True,
) -> Path:
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    if dest_zip.exists():
        dest_zip.unlink()

    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("CO_JE_PODKLADARNA.txt", ZIP_ABOUT_TXT)
        if omap_path and omap_path.is_file():
            zf.write(omap_path, OOM_MAP_NAME)
        zf.writestr("README_OOM.txt", oom_readme(metadata))
        zf.writestr(
            "metadata.json",
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        )
        for name in ("pullautus.png", "pullautus.pgw"):
            if include_png:
                _write_if_exists(zf, kp_cwd / name, f"basemap/{name}")
        for name in ("pullautus_depr.png", "pullautus_depr.pgw"):
            if include_png:
                _write_if_exists(zf, kp_cwd / name, f"relief/{name}")
        if reference_dir and reference_dir.is_dir():
            for png in sorted(reference_dir.glob("*.png")):
                zf.write(png, f"references/{png.name}")
                pgw = png.with_suffix(".pgw")
                if pgw.is_file():
                    zf.write(pgw, f"references/{pgw.name}")
        temp = kp_cwd / "temp"
        if include_dxf and temp.is_dir():
            for zip_name, src in sorted(
                collect_dxf_for_zip(temp, include_cliffs=include_cliffs).items()
            ):
                zf.write(src, f"karttapullautin/{zip_name}")
        contours_dir = kp_cwd / "contours"
        if contours_dir.is_dir():
            for path in sorted(contours_dir.iterdir()):
                if path.is_file() and path.suffix.lower() in {
                    ".shp",
                    ".shx",
                    ".dbf",
                    ".prj",
                    ".cpg",
                }:
                    zf.write(path, f"contours/{path.name}")
        vege_dir = kp_cwd / "vegetation"
        if vege_dir.is_dir():
            for path in sorted(vege_dir.iterdir()):
                if path.is_file() and path.suffix.lower() in {
                    ".shp",
                    ".shx",
                    ".dbf",
                    ".prj",
                    ".cpg",
                }:
                    zf.write(path, f"vegetation/{path.name}")
        osm_dir = kp_cwd / "osm_paths"
        if osm_dir.is_dir():
            for name in ("paths.geojson", "features.geojson"):
                gj = osm_dir / name
                if gj.is_file():
                    zf.write(gj, f"osm_paths/{name}")
        # osm_kp.zip je vstup pro Karttapullautin; do výstupu rozbalit SHP
        # stejně jako zabaged/ – ať jde importovat bez vnořeného ZIPu.
        osm_kp = kp_cwd / "osm_kp.zip"
        if osm_kp.is_file():
            _add_shapefiles_from_zip(zf, osm_kp, "osm_paths")
        if zabaged_clean and zabaged_clean.is_file():
            _add_shapefiles_from_zip(zf, zabaged_clean, "zabaged")
            if include_zabaged_archive:
                zf.write(zabaged_clean, "zabaged_clean.zip")

    return dest_zip
