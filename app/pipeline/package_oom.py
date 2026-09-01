from __future__ import annotations

import json
import zipfile
from pathlib import Path

from app.pipeline.build_oom_map import write_oom_map
from app.pipeline.fetch_openzu import crop_bounds_5514
from app.pipeline.reference_layers import HILLSHADE_ALTITUDE, HILLSHADE_AZIMUTH, reference_metadata

OUTPUT_ZIP_NAME = "podkladarna_output.zip"
OOM_ZIP_NAME = "podkladarna_oom.zip"  # legacy – starší joby
OOM_MAP_NAME = "podkladarna.omap"


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
        "contour_interval_m": preset.get("contour_interval"),
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
        f"Typ mapy: {label}\n"
        f"Měřítko: 1:{scale}\n"
        f"Ekvidistance: {interval_txt}\n"
        "Souřadnicový systém: EPSG:5514 (S-JTSK / Křovák)\n\n"
        f"Stínovaný reliéf DMR 5G: azimut {HILLSHADE_AZIMUTH}°, "
        f"výška slunce {HILLSHADE_ALTITUDE}° (kartografický standard GDAL).\n"
        f"{ref_block}\n"
        "Doporučený postup v OOM\n"
        "-----------------------\n"
        "1. Rozbalte ZIP. Otevřete podkladarna.omap – načte referenční vrstvy.\n"
        "   Nebo File → New (EPSG:5514, měřítko dle metadata.json) a Template → Open…\n"
        "2. Pořadí podkladů (zdola): ortofoto → OSM → hillshade → Karttapullautin PNG.\n"
        "   Referenční vrstvy nejsou součástí finální mapy – jen pro kreslení.\n"
        "3. File → Import… → karttapullautin/*.dxf (vrstevnice, srázy).\n"
        "4. File → Import… → vectors/*.shp (ZABAGED). Symboliku přiřaďte ručně.\n\n"
        "OCAD: soubor .omap neotevře – importujte DXF, SHP nebo georeferencované PNG+PGW.\n"
        "Nebo v OOM exportujte do formátu OCD (v8–12).\n\n"
        "Data: ČÚZK (DMR 5G, DMP 1G, ZABAGED®, ortofoto), CC BY 4.0. "
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
    with zipfile.ZipFile(src_zip) as src:
        for info in src.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name
            if not name or name.startswith("."):
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
    bbox_wgs84: tuple[float, float, float, float],
    reference_dir: Path | None,
) -> Path | None:
    west, south, east, north = bbox_wgs84
    xmin, ymin, xmax, ymax = crop_bounds_5514(west, south, east, north)
    ref_x = (xmin + xmax) / 2
    ref_y = (ymin + ymax) / 2
    templates: list[tuple[str, str, bool]] = []
    if reference_dir and reference_dir.is_dir():
        order = (
            ("Ortofoto ČÚZK", "references/orthophoto.png"),
            ("OpenStreetMap", "references/osm.png"),
            ("Hillshade DMR 5G", "references/hillshade_dmr5g.png"),
        )
        for label, relpath in order:
            if (reference_dir / Path(relpath).name).is_file():
                templates.append((label, relpath, True))
    if (kp_cwd / "pullautus.png").is_file():
        templates.append(("Karttapullautin", "basemap/pullautus.png", True))
    if not templates:
        return None
    return write_oom_map(
        dest,
        map_name=map_name,
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        templates=templates,
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
) -> Path:
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    if dest_zip.exists():
        dest_zip.unlink()

    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
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
            for path in sorted(temp.glob("*.dxf")):
                zf.write(path, f"karttapullautin/{path.name}")
        if zabaged_clean and zabaged_clean.is_file():
            _add_shapefiles_from_zip(zf, zabaged_clean, "vectors")
            if include_zabaged_archive:
                zf.write(zabaged_clean, "zabaged_clean.zip")

    return dest_zip
