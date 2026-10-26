"""Řídicí body lokální korelací: kolem kandidátů (křižovatky OSM cest, soutoky, budovy) se porovná
okno 160×160 m OSM kresby (cesty, budovy, vodní toky; 1 m/px) s referencí převzorkovanou počáteční
transformací a hledá se posun ±30 m. Výsledek: ref/georef_body.json (páry px ↔ S-JTSK)."""
import json, math, sys
from collections import Counter
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi
from scipy.signal import fftconvolve
sys.path.insert(0, r"C:\Users\PetrJanecek\source\repos\podkladarna-harrachov")
from app.tool_env import apply_local_gis_env; apply_local_gis_env()
from pyproj import Transformer
Image.MAX_IMAGE_PIXELS = None
B = Path(r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_srovnani")
REF = B / "ref"
if (REF / "harrachov_ref.pgw").exists():
    _w = [float(v) for v in (REF / "harrachov_ref.pgw").read_text().split()]; P = np.array([_w[0], _w[1], _w[4], _w[5]])
else:
    P = np.array(json.loads((REF / "georef_pary.json").read_text())["P"])
tr = Transformer.from_crs(4326, 5514, always_xy=True).transform
osm = json.loads((REF / "osm_overpass.json").read_text(encoding="utf-8"))["elements"]


def fwd(P, c, r):
    a, b, tx, ty = P
    return tx + a * c + b * r, ty + b * c - a * r


def inv(P, x, y):
    a, b, tx, ty = P
    dx, dy = x - tx, y - ty
    d = a * a + b * b
    return (a * dx + b * dy) / d, (b * dx - a * dy) / d


im = Image.open(REF / "harrachov_ref.png").convert("RGB")
full = np.asarray(im).astype(np.int16)
HH, WW = full.shape[:2]
blackF = (full.max(axis=2) < 100).astype(float)
cyanF = ((full[..., 0] < 120) & (full[..., 1] > 150) & (full[..., 2] > 170)).astype(float)
mask_full = np.asarray(Image.open(REF / "maska_8.png").resize((WW, HH), Image.NEAREST)) > 0

# OSM geometrie v S-JTSK
lines_hw, lines_w, polys_b = [], [], []
cnt = Counter()
for el in osm:
    t = el.get("tags", {}); g = [n for n in (el.get("geometry") or []) if n]
    if len(g) < 2:
        continue
    xy = [tr(n["lon"], n["lat"]) for n in g]
    if "highway" in t and t["highway"] not in ("steps",):
        lines_hw.append(xy)
        for k in {(round(x, 1), round(y, 1)) for x, y in xy}:
            cnt[k] += 1
    elif "waterway" in t:
        lines_w.append(xy)
    elif "building" in t:
        polys_b.append(xy)
junctions = [k for k, v in cnt.items() if v >= 3]   # uzly ve ≥3 cestách
junctions += [k for k, v in cnt.items() if v == 2]
HALF, SH, RES = 80, 30, 1.0
N = int(2 * HALF / RES)


def osm_patch(cx, cy):
    a = Image.new("L", (N, N), 0); w = Image.new("L", (N, N), 0)
    da, dw = ImageDraw.Draw(a), ImageDraw.Draw(w)
    f = lambda pts: [((x - cx + HALF) / RES, (cy + HALF - y) / RES) for x, y in pts]
    for l in lines_hw:
        da.line(f(l), fill=255, width=3)
    for p in polys_b:
        da.polygon(f(p), fill=255)
    for l in lines_w:
        dw.line(f(l), fill=255, width=2)
    return np.asarray(a, float) / 255, np.asarray(w, float) / 255


def ref_patch(cx, cy, half):
    n = int(2 * half / RES)
    gx = cx - half + (np.arange(n) + 0.5) * RES
    gy = cy + half - (np.arange(n) + 0.5) * RES
    X, Y = np.meshgrid(gx, gy)
    c, r = inv(P, X, Y)
    coords = np.array([r.ravel(), c.ravel()])
    bk = ndi.map_coordinates(blackF, coords, order=1, cval=0).reshape(n, n)
    cy_ = ndi.map_coordinates(cyanF, coords, order=1, cval=0).reshape(n, n)
    mk = ndi.map_coordinates(mask_full.astype(float), coords, order=0, cval=0).reshape(n, n)
    return bk, cy_, mk


rng = np.random.default_rng(1)
cands = junctions + [tuple(np.mean(p, axis=0)) for p in polys_b]
rng.shuffle(cands)
# doplnění: lomové body cest a toků (pokrytí jihu a západu, kde je málo křižovatek)
extra = [tuple(p) for l in lines_hw + lines_w for p in l[::5]]
rng.shuffle(extra)
cands += extra
pts = []
for cx, cy in cands:
    c, r = inv(P, cx, cy)
    if not (0 <= c < WW and 0 <= r < HH) or not mask_full[int(r), int(c)]:
        continue
    if any(math.hypot(cx - q["x"], cy - q["y"]) < 150 for q in pts):
        continue
    oa, ow = osm_patch(cx, cy)
    if oa.sum() < 50:
        continue
    bk, cyn, mk = ref_patch(cx, cy, HALF + SH)
    if mk.mean() < 0.6:
        continue
    sc = fftconvolve(bk, oa[::-1, ::-1], mode="valid") + 2 * fftconvolve(cyn, ow[::-1, ::-1], mode="valid")
    i = np.unravel_index(np.argmax(sc), sc.shape)
    peak = sc[i]
    # ostrost: druhé maximum mimo 6 m okolí
    sc2 = sc.copy(); sc2[max(0, i[0] - 6):i[0] + 7, max(0, i[1] - 6):i[1] + 7] = -1
    ratio = peak / max(sc2.max(), 1e-9)
    if ratio < 1.15 or i[0] in (0, sc.shape[0] - 1) or i[1] in (0, sc.shape[1] - 1):
        continue
    # posun: OSM okno sedí v referenci na indexu i → referenční bod OSM (cx,cy) leží v ref okně na
    # (SH + ... ) ; dx>0 = reference je posunutá na východ oproti počáteční transformaci
    dx = (i[1] - SH / RES) * RES; dy = -(i[0] - SH / RES) * RES
    # bod reference, který odpovídá OSM (cx,cy): v souřadnicích počáteční transformace (cx+dx, cy+dy)
    c, r = inv(P, cx + dx, cy + dy)
    pts.append(dict(x=cx, y=cy, c=float(c), r=float(r), dx=dx, dy=dy, ostrost=round(float(ratio), 3)))
    print(len(pts), round(cx), round(cy), dx, dy, round(ratio, 2), flush=True)
    if len(pts) >= 120:
        break
json.dump({"P0": list(P), "body": pts}, open(REF / "georef_body.json", "w"), indent=1)
