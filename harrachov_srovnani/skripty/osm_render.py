"""Náhled OSM (EPSG:5514) pro ruční nalezení hrubých řídicích bodů. Argument: výstupní PNG."""
import json, sys
from pathlib import Path
sys.path.insert(0, r"C:\Users\PetrJanecek\source\repos\podkladarna-harrachov")
from app.tool_env import apply_local_gis_env; apply_local_gis_env()
from pyproj import Transformer
from PIL import Image, ImageDraw

B = Path(r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_srovnani")
tr = Transformer.from_crs(4326, 5514, always_xy=True).transform
X0, X1, Y0, Y1 = -665500, -660500, -980500, -975500  # S-JTSK
RES = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0


def px(x, y):
    return ((x - X0) / RES, (Y1 - y) / RES)


data = json.loads((B / "ref" / "osm_overpass.json").read_text(encoding="utf-8"))
W, H = int((X1 - X0) / RES), int((Y1 - Y0) / RES)
im = Image.new("RGB", (W, H), "white")
d = ImageDraw.Draw(im)
for el in data["elements"]:
    t = el.get("tags", {})
    g = el.get("geometry") or []
    pts = [px(*tr(n["lon"], n["lat"])) for n in g if n]
    if len(pts) < 2:
        continue
    if "building" in t:
        d.polygon(pts, fill="black")
    elif t.get("natural") == "water":
        d.polygon(pts, fill=(80, 160, 255))
    elif "waterway" in t:
        d.line(pts, fill=(0, 120, 255), width=2)
    elif "highway" in t:
        hw = t["highway"]
        col = (200, 60, 0) if hw in ("primary", "secondary", "tertiary", "residential", "unclassified", "service") else (60, 60, 60)
        d.line(pts, fill=col, width=3 if col[0] == 200 else 1)
    elif "aerialway" in t:
        d.line(pts, fill=(160, 0, 160), width=1)
    elif t.get("man_made") or "piste:type" in t:
        d.line(pts + [pts[0]], fill=(255, 140, 0), width=1)
# mřížka po 500 m s popisy
for x in range(X0, X1 + 1, 500):
    d.line([px(x, Y0), px(x, Y1)], fill=(220, 220, 220))
    d.text((px(x, Y1)[0] + 2, 2), str(x), fill="red")
for y in range(Y0, Y1 + 1, 500):
    d.line([px(X0, y), px(X1, y)], fill=(220, 220, 220))
    d.text((2, px(X0, y)[1] + 2), str(y), fill="red")
im.save(sys.argv[1])
print(im.size)
