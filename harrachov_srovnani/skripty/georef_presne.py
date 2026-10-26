"""Přesná georeference reference do EPSG:5514.
1) jemné hledání rotace (0,1°) korelací černé kresby s OSM cestami (2 m/px),
2) řídicí body = těžiště budov: černé plochy v referenci ↔ OSM budovy (podobná plocha, do 25 m),
3) podobnostní transformace nejmenšími čtverci, iterativní vyřazení odlehlých bodů,
4) výběr ≥ 6 řídicích bodů po celé mapě, uložení .pgw/.prj, georeference.json, ridici_body.csv, obrys.geojson.
"""
import csv, json, math, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi
from scipy.signal import fftconvolve
sys.path.insert(0, r"C:\Users\PetrJanecek\source\repos\podkladarna-harrachov")
from app.tool_env import apply_local_gis_env; apply_local_gis_env()
from pyproj import Transformer, CRS
from shapely.geometry import Polygon, mapping
Image.MAX_IMAGE_PIXELS = None
B = Path(r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_srovnani")
REF = B / "ref"
hr = json.loads((REF / "georef_hrube.json").read_text())
M_PX = hr["m_px"]; F = 8
tr = Transformer.from_crs(4326, 5514, always_xy=True).transform
osm = json.loads((REF / "osm_overpass.json").read_text(encoding="utf-8"))["elements"]

# --- 1) jemná rotace ------------------------------------------------------
R = 2.0
X0, X1, Y0, Y1 = -667000, -659000, -982000, -974000
W, H = int((X1 - X0) / R), int((Y1 - Y0) / R)
roads = Image.new("L", (W, H), 0); dr = ImageDraw.Draw(roads)
buildings = []
for el in osm:
    t = el.get("tags", {}); g = [n for n in (el.get("geometry") or []) if n]
    if len(g) < 2:
        continue
    xy = [tr(n["lon"], n["lat"]) for n in g]
    if "highway" in t:
        dr.line([((x - X0) / R, (Y1 - y) / R) for x, y in xy], fill=255, width=1)
    if "building" in t and len(xy) >= 4:
        p = Polygon(xy)
        if p.is_valid and p.area > 0:
            buildings.append((el["id"], p))
roads = ndi.gaussian_filter(np.asarray(roads, float) / 255, 2.0)
im = Image.open(REF / "harrachov_ref.png").convert("RGB")
full = np.asarray(im)
sm = np.asarray(im.resize((im.width // F, im.height // F), Image.BOX)).astype(int)
mask8 = np.asarray(Image.open(REF / "maska_8.png")) > 0
black8 = (sm.max(axis=2) < 110) & ndi.binary_erosion(mask8, iterations=3)
rr8, cc8 = np.nonzero(black8)
cc_full, rr_full = cc8 * F + (F - 1) / 2, rr8 * F + (F - 1) / 2


def score_at(deg, tx, ty, m):
    th = math.radians(deg)
    x = tx + m * (cc_full * math.cos(th) + rr_full * math.sin(th))
    y = ty + m * (cc_full * math.sin(th) - rr_full * math.cos(th))
    c = ((x - X0) / R).astype(int); r = ((Y1 - y) / R).astype(int)
    ok = (c >= 0) & (r >= 0) & (c < W) & (r < H)
    return roads[r[ok], c[ok]].mean()


best = (score_at(hr["deg"], hr["tx"], hr["ty"], M_PX), hr["deg"], hr["tx"], hr["ty"], M_PX)
# lokální hledání (rotace kolem středu mapy, posuny)
cx0, cy0 = full.shape[1] / 2, full.shape[0] / 2
for it in range(3):
    _, deg0, tx0, ty0, m0 = best
    th0 = math.radians(deg0)
    X_c = tx0 + m0 * (cx0 * math.cos(th0) + cy0 * math.sin(th0))
    Y_c = ty0 + m0 * (cx0 * math.sin(th0) - cy0 * math.cos(th0))
    step = [0.25, 0.1, 0.05][it]; tstep = [8, 4, 2][it]; mstep = [0.01, 0.004, 0.002][it]
    for deg in np.arange(deg0 - 4 * step, deg0 + 4 * step + 1e-9, step):
        for mf in (1 - 2 * mstep, 1 - mstep, 1, 1 + mstep, 1 + 2 * mstep):
            m = m0 * mf; th = math.radians(deg)
            for dx in np.arange(-5, 6) * tstep:
                for dy in np.arange(-5, 6) * tstep:
                    tx = X_c + dx - m * (cx0 * math.cos(th) + cy0 * math.sin(th))
                    ty = Y_c + dy - m * (cx0 * math.sin(th) - cy0 * math.cos(th))
                    s = score_at(deg, tx, ty, m)
                    if s > best[0]:
                        best = (s, deg, tx, ty, m)
    print("jemně", it, best)
_, deg, tx, ty, m = best
th = math.radians(deg)
P = np.array([m * math.cos(th), m * math.sin(th), tx, ty])  # a, b, tx, ty


def fwd(P, c, r):
    a, b, tx, ty = P
    return tx + a * c + b * r, ty + b * c - a * r


def inv(P, x, y):
    a, b, tx, ty = P
    dx, dy = x - tx, y - ty
    d = a * a + b * b
    return (a * dx + b * dy) / d, (b * dx - a * dy) / d


# --- 2) černé plochy (budovy) v referenci --------------------------------
mask_full = np.asarray(Image.open(REF / "maska_8.png").resize((full.shape[1], full.shape[0]), Image.NEAREST)) > 0
blk = (full.max(axis=2) < 90) & mask_full
blk = ndi.binary_opening(blk, iterations=3)          # pryč s čarami (cesty ~1–2 px široké? čáry 0,25–0,35 mm = 5–8 px)
blk = ndi.binary_opening(blk, structure=np.ones((5, 5)), iterations=2)
lab, n = ndi.label(blk)
idx = np.arange(1, n + 1)
area_px = ndi.sum(blk, lab, idx)
cms = ndi.center_of_mass(blk, lab, idx)
objs = ndi.find_objects(lab)
blobs = []
for i in range(n):
    a_m2 = area_px[i] * m * m
    if a_m2 < 60:
        continue
    sl = objs[i]
    hh = (sl[0].stop - sl[0].start) * m; ww = (sl[1].stop - sl[1].start) * m
    fill = a_m2 / max(hh * ww, 1)
    blobs.append((cms[i][1], cms[i][0], a_m2, fill))
print("černé plochy", len(blobs))


def match(P, maxd):
    pairs = []
    bx = np.array([fwd(P, c, r) for c, r, _, _ in blobs])
    for oid, p in buildings:
        if p.area < 80:
            continue
        ct = p.centroid
        d = np.hypot(bx[:, 0] - ct.x, bx[:, 1] - ct.y)
        j = int(np.argmin(d))
        if d[j] > maxd:
            continue
        a_ref = blobs[j][2]
        if not (0.5 < a_ref / p.area < 2.0):
            continue
        # jednoznačnost: v okolí žádná jiná budova OSM ani černá plocha
        near_b = sum(1 for _, q in buildings if q is not p and q.centroid.distance(ct) < 30)
        near_r = int((d < 30).sum()) - 1
        if near_b or near_r:
            continue
        pairs.append(dict(osm_id=oid, c=blobs[j][0], r=blobs[j][1], x=ct.x, y=ct.y, a_osm=p.area, a_ref=a_ref))
    return pairs


def fit(pairs):
    A = []; L = []
    for p in pairs:
        A.append([p["c"], p["r"], 1, 0]); L.append(p["x"])
        A.append([-p["r"], p["c"], 0, 1]); L.append(p["y"])
    sol, *_ = np.linalg.lstsq(np.array(A, float), np.array(L, float), rcond=None)
    return sol


for maxd, lim in ((30, 12), (15, 6), (10, 4)):
    pairs = match(P, maxd)
    for _ in range(10):
        Pn = fit(pairs)
        res = [math.hypot(*np.subtract(fwd(Pn, p["c"], p["r"]), (p["x"], p["y"]))) for p in pairs]
        worst = int(np.argmax(res))
        if res[worst] > lim and len(pairs) > 8:
            pairs.pop(worst); continue
        break
    P = Pn
    print(f"maxd {maxd}: {len(pairs)} párů, RMS {math.sqrt(np.mean(np.square(res))):.2f} m, max {max(res):.2f} m")

pairs = match(P, 8)
for _ in range(50):
    P = fit(pairs)
    res = [math.hypot(*np.subtract(fwd(P, p["c"], p["r"]), (p["x"], p["y"]))) for p in pairs]
    worst = int(np.argmax(res))
    if res[worst] > 3.5 and len(pairs) > 8:
        pairs.pop(worst); continue
    break
for p, e in zip(pairs, res):
    p["res"] = e
print("všechny budovy:", len(pairs), "RMS", round(math.sqrt(np.mean(np.square(res))), 2), "max", round(max(res), 2))
json.dump({"P": list(P), "pairs": pairs}, open(REF / "georef_pary.json", "w"), indent=1)
