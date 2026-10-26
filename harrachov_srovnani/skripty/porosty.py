"""Podklady pro návrh porostů: rastry ořezané na obrys reference + 150 m, vegetace, skály, srázy, OSM skály, ZABAGED zeleň."""
import json, shutil, subprocess, sys, zipfile
from pathlib import Path
sys.path.insert(0, r"C:\Users\PetrJanecek\source\repos\podkladarna-harrachov")
from app.tool_env import apply_local_gis_env, which_tool; apply_local_gis_env()
from PIL import Image
Image.MAX_IMAGE_PIXELS = None
B = Path(r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_srovnani")
W = Path(r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_work")
P = B / "porosty"; P.mkdir(exist_ok=True)
x0, y0, x1, y1 = json.loads((B / "ref" / "georeference.json").read_text(encoding="utf-8"))["obrys_bbox_5514"]
x0, y0, x1, y1 = x0 - 150, y0 - 150, x1 + 150, y1 + 150
tr = which_tool("gdal_translate") or "gdal_translate"
for src, dst in (("dem/chm.tif", "chm.tif"), ("dem/dem_filled.tif", "dem.tif"), ("dem/dsm_raw.tif", "dsm.tif")):
    subprocess.run([tr, "-q", "-projwin", str(x0), str(y1), str(x1), str(y0), "-a_srs", "EPSG:5514",
                    "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=3", "-co", "TILED=YES", str(W / src), str(P / dst)], check=True)
    print(dst, (P / dst).stat().st_size)
# PNG -> ořez; chm_tint je na mřížce chm.tif (gdalinfo: origin -664605.38, -976118.74, 1 m) – world file = střed pixelu
CHM_W = [1.0, 0, 0, -1.0, -664605.38 + 0.5, -976118.74 - 0.5]
for src, dst in (("vegetation/chm_tint.png", "vegetace_trida_chm_tint"), ("shade/hillshade.png", "hillshade")):
    s = W / src; im = Image.open(s)
    pgw = s.with_suffix(".pgw")
    w = [float(v) for v in pgw.read_text().split()] if pgw.exists() else CHM_W
    c0 = int((x0 - w[4]) / w[0]); c1 = int((x1 - w[4]) / w[0]); r0 = int((y1 - w[5]) / w[3]); r1 = int((y0 - w[5]) / w[3])
    c0, r0 = max(c0, 0), max(r0, 0); c1, r1 = min(c1, im.width), min(r1, im.height)
    im.crop((c0, r0, c1, r1)).save(P / f"{dst}.png")
    (P / f"{dst}.pgw").write_text("\n".join(str(v) for v in (w[0], 0, 0, w[3], w[4] + c0 * w[0], w[5] + r0 * w[3])) + "\n")
    shutil.copy(B / "ref" / "harrachov_ref.prj", P / f"{dst}.prj")
    print(dst, im.size, "->", (c1 - c0, r1 - r0), "pgw", w)
# vektory
for d, pat in (("vegetation", "vegetation.*"), ("temp", "*.dxf")):
    for f in (W / d).glob(pat):
        shutil.copy(f, P / f.name)
shutil.copytree(W / "uzitecne", P / "uzitecne", dirs_exist_ok=True)
(P / "osm").mkdir(exist_ok=True)
for f in (W / "osm_paths" / "manual").glob("OSM_*"):
    if f.stem in ("OSM_skaly", "OSM_skaly_linie", "OSM_mokrad", "OSM_voda", "OSM_residential"):
        shutil.copy(f, P / "osm" / f.name)
(P / "zabaged").mkdir(exist_ok=True)
VEG = ("TrvalyTravniPorost", "UdrzovanaZelen", "LiniovaVegetace", "VyznamnyStromLesik", "OvocnySadZahrada",
       "SkupinaBalvanu", "OsamelyBalvanSkalaSkalniSuk", "StupenSraz", "ArealUceloveZastavby", "LesniPudaSeStromy",
       "LesniPudaSKosodrevinou", "Mokrad", "Hrbitov")
with zipfile.ZipFile(W / "zabaged_clean.zip") as z:
    for n in z.namelist():
        if Path(n).stem in VEG:
            (P / "zabaged" / Path(n).name).write_bytes(z.read(n))
print("hotovo", sorted(p.name for p in (P / "zabaged").glob("*.shp")))
