from __future__ import annotations

import json
import zipfile
from pathlib import Path

OOM_ZIP_NAME = "podkladarna_oom.zip"


def map_scale_from_scalefactor(scalefactor: float) -> int:
    return int(round(float(scalefactor) * 10000))


def oom_metadata(
    preset_id: str,
    preset: dict,
    options: dict,
    job_name: str = "",
) -> dict:
    sf = float(options.get("scalefactor", preset.get("scalefactor", 1)))
    return {
        "name": job_name,
        "preset_id": preset_id,
        "label": preset.get("label", preset_id),
        "crs": "EPSG:5514",
        "scale": map_scale_from_scalefactor(sf),
        "scalefactor": sf,
        "contour_interval_m": preset.get("contour_interval"),
        "formline": options.get("formline", preset.get("formline")),
    }


def oom_readme(meta: dict) -> str:
    scale = meta.get("scale") or "?"
    label = meta.get("label") or meta.get("preset_id") or ""
    interval = meta.get("contour_interval_m")
    interval_txt = f"{interval} m" if interval is not None else "?"
    return (
        "Podkladárna – balíček pro OpenOrienteering Mapper\n"
        "=================================================\n\n"
        f"Typ mapy: {label}\n"
        f"Měřítko: 1:{scale}\n"
        f"Ekvidistance: {interval_txt}\n"
        "Souřadnicový systém: EPSG:5514 (S-JTSK / Křovák)\n\n"
        "Doporučený postup v OOM\n"
        "-----------------------\n"
        "1. File → New, CRS EPSG:5514, měřítko podle metadata.json (scale).\n"
        "2. Template → Open… → basemap/pullautus.png (georeference z PGW).\n"
        "   Volitelně relief/pullautus_depr.png.\n"
        "3. File → Import… → karttapullautin/*.dxf (vrstevnice, srázy, knolly).\n"
        "4. File → Import… → vectors/*.shp (ZABAGED). Symboliku přiřaďte ručně.\n\n"
        "Data: ČÚZK (DMR 5G, DMP 1G, ZABAGED®), CC BY 4.0. "
        "Při šíření mapy uveďte zdroj: ČÚZK, [rok].\n"
        "Reliéf a vegetace: Karttapullautin (GPL-3.0).\n\n"
        "Tento ZIP ještě neobsahuje hotový soubor .omap (to je v2).\n"
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


def build_oom_zip(
    kp_cwd: Path,
    dest_zip: Path,
    *,
    zabaged_clean: Path | None,
    metadata: dict,
) -> Path:
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    if dest_zip.exists():
        dest_zip.unlink()

    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README_OOM.txt", oom_readme(metadata))
        zf.writestr(
            "metadata.json",
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        )
        for name in ("pullautus.png", "pullautus.pgw"):
            _write_if_exists(zf, kp_cwd / name, f"basemap/{name}")
        for name in ("pullautus_depr.png", "pullautus_depr.pgw"):
            _write_if_exists(zf, kp_cwd / name, f"relief/{name}")
        temp = kp_cwd / "temp"
        if temp.is_dir():
            for path in sorted(temp.glob("*.dxf")):
                zf.write(path, f"karttapullautin/{path.name}")
        if zabaged_clean and zabaged_clean.is_file():
            _add_shapefiles_from_zip(zf, zabaged_clean, "vectors")

    return dest_zip
