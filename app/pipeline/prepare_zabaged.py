from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import yaml

from app.pipeline.crs_5514 import write_prj
from app.settings import CONFIG_DIR
from app.tool_env import gis_subprocess_env, which_tool


def _feature_count(ogrinfo: str, shp_vsi: str) -> int:
    r = subprocess.run(
        [ogrinfo, "-al", "-so", shp_vsi],
        capture_output=True,
        text=True,
        check=False,
        env=gis_subprocess_env(ogrinfo),
    )
    m = re.search(r"Feature Count:\s*(\d+)", r.stdout or "")
    return int(m.group(1)) if m else -1


def clean_zabaged(
    src_zip: Path,
    out_zip: Path,
    log: callable | None = None,
) -> Path:
    if not src_zip.exists():
        raise FileNotFoundError(f"ZABAGED ZIP nenalezen: {src_zip}")

    layers_path = CONFIG_DIR / "zabaged_layers.yaml"
    layers = yaml.safe_load(layers_path.read_text(encoding="utf-8"))["layers"]
    ogrinfo = which_tool("ogrinfo")
    if not ogrinfo:
        raise RuntimeError(
            "ogrinfo není v PATH. Na Windows použijte OSGeo4W, "
            "nebo docker compose -f docker-compose.dev.yml up"
        )

    stage = out_zip.parent / "_zabaged_stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    kept = 0
    with zipfile.ZipFile(src_zip) as zf:
        names = zf.namelist()
        for layer in layers:
            shp_members = [n for n in names if Path(n).name == f"{layer}.shp"]
            if not shp_members:
                if log:
                    log(f"  skip (neni v ZIP): {layer}")
                continue
            vsi = f"/vsizip/{src_zip.as_posix()}/{shp_members[0]}"
            fc = _feature_count(ogrinfo, vsi)
            if fc <= 0:
                if log:
                    log(f"  skip (prazdne): {layer}")
                continue
            for n in names:
                bn = Path(n).name
                if bn == f"{layer}.shp.xml" or bn.startswith(f"{layer}."):
                    if bn.startswith(f"{layer}_"):
                        continue
                    (stage / bn).write_bytes(zf.read(n))
            if log:
                log(f"  OK {layer}: {fc} prvku")
            kept += 1

    if kept == 0:
        shutil.rmtree(stage)
        raise RuntimeError("ZABAGED ZIP neobsahuje zadne pouzitelne vrstvy")

    # Starsi cache i cizi ZIPy nesou ESRI .prj bez TOWGS84 – v OOM by vektory
    # sedely vedle georeferencovaneho PNG.
    for shp in stage.glob("*.shp"):
        write_prj(shp)

    if out_zip.exists():
        out_zip.unlink()
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for f in stage.iterdir():
            if f.is_file():
                z.write(f, f.name)
    shutil.rmtree(stage)
    if log:
        log(f"ZIP {out_zip.name}: {kept} vrstev, {out_zip.stat().st_size / 1e3:.0f} kB")
    return out_zip
