"""Podobnostní transformace z řídicích bodů (nejmenší čtverce, vyřazení odlehlých),
uloží ref/harrachov_ref.pgw/.prj, georeference.json, ridici_body.csv, obrys.geojson."""
import csv, json, math, sys
from pathlib import Path
import numpy as np
from PIL import Image
from scipy import ndimage as ndi
sys.path.insert(0, r"C:\Users\PetrJanecek\source\repos\podkladarna-harrachov")
from app.tool_env import apply_local_gis_env; apply_local_gis_env()
from pyproj import CRS, Transformer
from shapely.geometry import Polygon, box, mapping
from shapely.ops import unary_union
Image.MAX_IMAGE_PIXELS = None
REF = Path(r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_srovnani\ref")
d = json.loads((REF / "georef_body.json").read_text())
pts = d["body"]


def fwd(P, c, r):
    a, b, tx, ty = P
    return tx + a * c + b * r, ty + b * c - a * r


def fit(pp):
    A, L = [], []
    for p in pp:
        A.append([p["c"], p["r"], 1, 0]); L.append(p["x"])
        A.append([-p["r"], p["c"], 0, 1]); L.append(p["y"])
    return np.linalg.lstsq(np.array(A, float), np.array(L, float), rcond=None)[0]


def resid(P, pp):
    return [math.hypot(*np.subtract(fwd(P, p["c"], p["r"]), (p["x"], p["y"]))) for p in pp]


vyrazene = []
while True:
    P = fit(pts); res = resid(P, pts)
    w = int(np.argmax(res))
    if res[w] > 3.5 and len(pts) > 8:
        vyrazene.append(pts.pop(w)); continue
    break
a, b, tx, ty = P
m = math.hypot(a, b); rot = math.degrees(math.atan2(b, a))
rms = math.sqrt(np.mean(np.square(res)))
print(f"{len(pts)} bodů, vyřazeno {len(vyrazene)}, m/px {m:.5f}, rotace {rot:.3f}°, RMS {rms:.2f} m, max {max(res):.2f} m")
dpi_fit = 25.4 / (m / 10.0)
# world file: A D B E C F (souřadnice středu levého horního pixelu; c,r jsou indexy středů pixelů)
(REF / "harrachov_ref.pgw").write_text("\n".join(f"{v:.10f}" for v in (a, b, b, -a, tx, ty)) + "\n")
(REF / "harrachov_ref.prj").write_text(CRS.from_epsg(5514).to_wkt("WKT1_ESRI"))
to_wgs = Transformer.from_crs(5514, 4326, always_xy=True).transform
with open(REF / "ridici_body.csv", "w", newline="", encoding="utf-8") as f:
    wr = csv.writer(f, delimiter=";")
    wr.writerow(["c_px", "r_px", "x_5514", "y_5514", "lon", "lat", "x_fit", "y_fit", "residuum_m", "ostrost", "zdroj"])
    for p, e in zip(pts, res):
        xf, yf = fwd(P, p["c"], p["r"]); lon, lat = to_wgs(p["x"], p["y"])
        wr.writerow([f"{p['c']:.1f}", f"{p['r']:.1f}", f"{p['x']:.2f}", f"{p['y']:.2f}", f"{lon:.6f}", f"{lat:.6f}",
                     f"{xf:.2f}", f"{yf:.2f}", f"{e:.2f}", p["ostrost"],
                     "OSM: okno 160 m (cesty+budovy+toky) korelací s černou/modrou kresbou reference"])
# obrys mapového pole
mk = np.asarray(Image.open(REF / "maska_8.png")) > 0
F = 8
boxes = []
for rr in range(mk.shape[0]):
    row = np.concatenate([[0], mk[rr].astype(np.int8), [0]])
    dd = np.diff(row); st = np.nonzero(dd == 1)[0]; en = np.nonzero(dd == -1)[0]
    boxes += [box(s, rr, e, rr + 1) for s, e in zip(st, en)]
pix = max(getattr(unary_union(boxes), "geoms", [unary_union(boxes)]), key=lambda g: g.area)
ring = [fwd(P, cc * F - 0.5, rr * F - 0.5) for cc, rr in pix.exterior.coords]
poly = Polygon(ring).simplify(2.0).buffer(0)
fc = {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::5514"}},
      "features": [{"type": "Feature", "properties": {"nazev": "Harrachov – Šablaturova cesta, obrys mapového pole",
                                                       "plocha_km2": round(poly.area / 1e6, 3)}, "geometry": mapping(poly)}]}
(REF / "obrys.geojson").write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
minx, miny, maxx, maxy = poly.bounds
cs = [to_wgs(x, y) for x, y in poly.exterior.coords]
gj = {"meritko": 10000, "ekvidistance_m": 5, "dpi_png": 571.9826, "dpi_z_transformace": round(dpi_fit, 2),
      "m_na_px": round(m, 6), "rotace_deg": round(rot, 4),
      "rotace_popis": "úhel osy x obrázku vůči ose x (východ) S-JTSK EAST-NORTH, kladný = proti směru hodinových ručiček; sever mapy (magnetický) je od severu mřížky odkloněn o tento úhel",
      "rms_m": round(rms, 2), "max_residuum_m": round(max(res), 2), "pocet_bodu": len(pts), "vyrazeno_bodu": len(vyrazene),
      "world_file": [a, b, b, -a, tx, ty], "crs": "EPSG:5514",
      "obrys_bbox_5514": [minx, miny, maxx, maxy],
      "obrys_bbox_wgs84": [min(c[0] for c in cs), min(c[1] for c in cs), max(c[0] for c in cs), max(c[1] for c in cs)],
      "obrys_plocha_km2": round(poly.area / 1e6, 3),
      "metoda": "hrubě: korelace černé kresby/modrých toků s OSM přes rotace; přesně: lokální korelace oken 160 m kolem křižovatek OSM, LSQ podobnost, vyřazení bodů s residuem > 3,5 m"}
(REF / "georeference.json").write_text(json.dumps(gj, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(gj, ensure_ascii=False, indent=1))
