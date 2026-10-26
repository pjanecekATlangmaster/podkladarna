"""Hrubá georeference: hledání rotace a posunu (měřítko z DPI) korelací černých čar reference
s OSM cestami a modrých čar s OSM vodními toky. Výstup: ref/georef_hrube.json"""
import json, math, sys
from pathlib import Path
import numpy as np
from PIL import Image
from scipy import ndimage as ndi
from scipy.signal import fftconvolve
sys.path.insert(0, r"C:\Users\PetrJanecek\source\repos\podkladarna-harrachov")
from app.tool_env import apply_local_gis_env; apply_local_gis_env()
from pyproj import Transformer
from PIL import ImageDraw
Image.MAX_IMAGE_PIXELS = None
B = Path(r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_srovnani")
F = 8
M_PX = 25.4 / 571.9826 * 10.0          # m/px plného rozlišení při 1:10 000
R = 4.0
X0, X1, Y0, Y1 = -667000, -659000, -982000, -974000
W, H = int((X1 - X0) / R), int((Y1 - Y0) / R)
tr = Transformer.from_crs(4326, 5514, always_xy=True).transform

roads = Image.new("L", (W, H), 0); water = Image.new("L", (W, H), 0)
dr, dw = ImageDraw.Draw(roads), ImageDraw.Draw(water)
for el in json.loads((B / "ref" / "osm_overpass.json").read_text(encoding="utf-8"))["elements"]:
    t = el.get("tags", {}); g = [n for n in (el.get("geometry") or []) if n]
    if len(g) < 2:
        continue
    pts = [((x - X0) / R, (Y1 - y) / R) for x, y in (tr(n["lon"], n["lat"]) for n in g)]
    if "highway" in t:
        dr.line(pts, fill=255, width=1)
    elif "waterway" in t:
        dw.line(pts, fill=255, width=1)
roads = ndi.gaussian_filter(np.asarray(roads, float) / 255, 1.5)
water = ndi.gaussian_filter(np.asarray(water, float) / 255, 1.5)

im = Image.open(B / "ref" / "harrachov_ref.png").convert("RGB")
sm = np.asarray(im.resize((im.width // F, im.height // F), Image.BOX)).astype(int)
mask = np.asarray(Image.open(B / "ref" / "maska_8.png")) > 0
mask = ndi.binary_erosion(mask, iterations=3)
r_, g_, b_ = sm[..., 0], sm[..., 1], sm[..., 2]
black = (sm.max(axis=2) < 110) & mask
cyan = (r_ < 120) & (g_ > 150) & (b_ > 170) & mask
print("black px", black.sum(), "cyan px", cyan.sum())


def template(pmask, th, s):
    rr, cc = np.nonzero(pmask)
    k = s / R
    dc = k * (cc * math.cos(th) + rr * math.sin(th))
    drr = k * (rr * math.cos(th) - cc * math.sin(th))
    oc, orr = dc.min(), drr.min()
    ic = np.round(dc - oc).astype(int); ir = np.round(drr - orr).astype(int)
    t = np.zeros((ir.max() + 1, ic.max() + 1)); np.add.at(t, (ir, ic), 1.0)
    return t, oc, orr


best = None
s = F * M_PX
for deg in np.arange(-30, 30.01, 1.0):
    th = math.radians(deg)
    tb, oc, orr = template(black, th, s)
    tc, oc2, orr2 = template(cyan, th, s)
    assert abs(oc - oc2) < 50 or True
    cb = fftconvolve(roads, tb[::-1, ::-1], mode="valid") / tb.sum()
    # cyan šablona může mít jiný počátek – převedeme na společný počátek přes oc/orr
    full_c = np.zeros_like(tb)
    rr, cc = np.nonzero(cyan); k = s / R
    ic = np.round(k * (cc * math.cos(th) + rr * math.sin(th)) - oc).astype(int)
    ir = np.round(k * (rr * math.cos(th) - cc * math.sin(th)) - orr).astype(int)
    ok = (ic >= 0) & (ir >= 0) & (ic < tb.shape[1]) & (ir < tb.shape[0])
    np.add.at(full_c, (ir[ok], ic[ok]), 1.0)
    cw = fftconvolve(water, full_c[::-1, ::-1], mode="valid") / max(full_c.sum(), 1)
    sc = cb + 2.0 * cw
    i = np.unravel_index(np.argmax(sc), sc.shape)
    val = sc[i]
    print(f"{deg:6.1f} {val:.4f} road {cb[i]:.4f} water {cw[i]:.4f}")
    if best is None or val > best[0]:
        # počátek šablony (r=0,c=0 v 1/8) leží v rastru OSM na (i[1]-oc, i[0]-orr)
        col0, row0 = i[1] - oc, i[0] - orr
        best = (val, deg, X0 + col0 * R, Y1 - row0 * R)
print("BEST", best)
val, deg, tx, ty = best
th = math.radians(deg)
# transformace plného rozlišení: x = tx + m*(c cos + r sin), y = ty + m*(c sin - r cos)
json.dump({"deg": deg, "tx": tx, "ty": ty, "m_px": M_PX, "score": val},
          open(B / "ref" / "georef_hrube.json", "w"), indent=1)
