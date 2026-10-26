"""Kontrolní obrázek: reference převzorkovaná do S-JTSK + OSM cesty (červeně), toky (modře), budovy (fialově), řídicí body.
kontrola_georef.py out.png [res] [x0 y0 x1 y1]"""
import csv, json, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi
sys.path.insert(0, r"C:\Users\PetrJanecek\source\repos\podkladarna-harrachov")
from app.tool_env import apply_local_gis_env; apply_local_gis_env()
from pyproj import Transformer
Image.MAX_IMAGE_PIXELS = None
REF = Path(r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_srovnani\ref")
a, b, b2, e, tx, ty = [float(v) for v in (REF / "harrachov_ref.pgw").read_text().split()]
g = json.loads((REF / "georeference.json").read_text(encoding="utf-8"))
res = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
x0, y0, x1, y1 = [float(v) for v in sys.argv[3:7]] if len(sys.argv) > 6 else g["obrys_bbox_5514"]
full = np.asarray(Image.open(REF / "harrachov_ref.png").convert("RGB"))
W, H = int((x1 - x0) / res), int((y1 - y0) / res)
X, Y = np.meshgrid(x0 + (np.arange(W) + 0.5) * res, y1 - (np.arange(H) + 0.5) * res)
dx, dy = X - tx, Y - ty; d = a * a + b * b
c = (a * dx + b * dy) / d; r = (b * dx - a * dy) / d
out = np.stack([ndi.map_coordinates(full[..., k], [r, c], order=1, cval=255) for k in range(3)], axis=-1).astype(np.uint8)
im = Image.fromarray(out); im = Image.blend(im, Image.new("RGB", im.size, "white"), 0.35)
dr = ImageDraw.Draw(im)
tr = Transformer.from_crs(4326, 5514, always_xy=True).transform
f = lambda pts: [((x - x0) / res, (y1 - y) / res) for x, y in pts]
for el in json.loads((REF / "osm_overpass.json").read_text(encoding="utf-8"))["elements"]:
    t = el.get("tags", {}); gg = [n for n in (el.get("geometry") or []) if n]
    if len(gg) < 2:
        continue
    pts = f([tr(n["lon"], n["lat"]) for n in gg])
    if "highway" in t:
        dr.line(pts, fill=(255, 0, 0), width=1)
    elif "waterway" in t:
        dr.line(pts, fill=(0, 0, 255), width=1)
    elif "building" in t:
        dr.polygon(pts, outline=(200, 0, 200))
for row in csv.DictReader(open(REF / "ridici_body.csv", encoding="utf-8"), delimiter=";"):
    px, py = f([(float(row["x_5514"]), float(row["y_5514"]))])[0]
    dr.ellipse([px - 6, py - 6, px + 6, py + 6], outline=(0, 160, 0), width=2)
ob = json.loads((REF / "obrys.geojson").read_text(encoding="utf-8"))["features"][0]["geometry"]["coordinates"][0]
dr.line(f(ob), fill=(0, 200, 0), width=2)
im.save(sys.argv[1]); print(im.size)
