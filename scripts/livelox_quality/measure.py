"""Metriky naší mapy proti referenční mapě z Liveloxu ve společné mřížce.

python measure.py <repo> <job_dir> <ref_dir> <kind> <ref_scale> <W,S,E,N> [<out_json>] [--debug <dir>]

Obě mapy se převedou do téže mřížky (UTM výřezu jobu, les 1 m, sprint 0,5 m), pixely se zařadí do tříd
podle barvy (ISOM / ISSprOM) a porovnají se podíly ploch, hustota vrstevnic a shoda zeleně a bílého lesa.
Referenční obrázek zůstává v cache; ladicí výřezy (--debug) jsou jen pro interní kontrolu, nepublikovat.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

CONTOUR_MM = 0.14  # šířka vrstevnice 101 na papíře; hlavní 0,25 mm zvedne hustotu u obou map stejně

# Třídy podle barvy (pořadí = priorita). Hodnoty HSV: h ve stupních, s a v 0–1.
CLASSES = ["white", "open", "rough_open", "green1", "green2", "green3", "olive", "water", "brown",
           "black", "grey", "paved", "course", "other"]


def classify(rgb: np.ndarray) -> np.ndarray:
    a = rgb.astype(np.float32) / 255.0
    mx, mn = a.max(-1), a.min(-1)
    v, d = mx, mx - mn
    s = np.where(mx > 0, d / np.maximum(mx, 1e-6), 0)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    h = np.zeros_like(v)
    nz = d > 1e-6
    rm = nz & (mx == r); gm = nz & (mx == g) & ~rm; bm = nz & ~rm & ~gm
    h[rm] = (60 * ((g - b)[rm] / d[rm])) % 360
    h[gm] = 60 * ((b - r)[gm] / d[gm]) + 120
    h[bm] = 60 * ((r - g)[bm] / d[bm]) + 240
    out = np.full(v.shape, CLASSES.index("other"), np.uint8)

    def put(name, m):
        out[m & (out == CLASSES.index("other"))] = CLASSES.index(name)

    put("white", (v > 0.9) & (s < 0.1))
    put("black", v < 0.28)
    put("course", (h >= 280) & (h <= 345) & (s > 0.35) & (v > 0.4))
    put("water", (h >= 170) & (h <= 240) & (s > 0.2) & (v > 0.35))
    put("grey", (s < 0.12) & (v >= 0.28) & (v <= 0.9))
    put("brown", (h >= 8) & (h < 34) & (s > 0.45) & (v >= 0.4) & (v < 0.9))
    put("open", (h >= 30) & (h < 52) & (s > 0.5) & (v > 0.85))
    put("rough_open", (h >= 30) & (h < 60) & (s >= 0.18) & (s <= 0.5) & (v > 0.88))
    put("paved", (h >= 15) & (h < 45) & (s >= 0.1) & (s < 0.45) & (v > 0.6))
    put("olive", (h >= 48) & (h < 80) & (s > 0.3) & (v >= 0.4) & (v <= 0.85))
    gr = (h >= 75) & (h <= 160) & (s > 0.12) & (v > 0.35)
    put("green1", gr & (s < 0.33))
    put("green2", gr & (s >= 0.33) & (s < 0.58))
    put("green3", gr & (s >= 0.58))
    return out


def _utm_epsg(lon: float, lat: float) -> int:
    return (32600 if lat >= 0 else 32700) + int((lon + 180) // 6) + 1


def _warp(src: Path, dst: Path, s_srs: str, epsg: int, te, res: float, extra=()):
    subprocess.run(["gdalwarp", "-q", "-overwrite", *extra, "-s_srs", s_srs, "-t_srs", f"EPSG:{epsg}",
                    "-te", *map(str, te), "-tr", str(res), str(res), "-r", "near", "-dstalpha", str(src), str(dst)],
                   check=True)
    im = np.asarray(Image.open(dst).convert("RGBA"))
    return im[..., :3], im[..., 3] > 0


def ref_with_gcps(ref_dir: Path, tmp: Path) -> Path:
    blob = json.loads((ref_dir / "blob.json").read_text())["map"]
    img = next(p for p in ref_dir.iterdir() if p.name.startswith("map."))
    w, h = Image.open(img).size
    q = [(v["longitude"], v["latitude"]) for v in blob["boundingQuadrilateral"]["vertices"]]
    pix = [(0, h), (w, h), (w, 0), (0, 0)]  # JZ, JV, SV, SZ
    vrt = tmp / "ref.vrt"
    gcps = sum((["-gcp", str(px), str(py), str(lon), str(lat)] for (px, py), (lon, lat) in zip(pix, q)), [])
    subprocess.run(["gdal_translate", "-q", "-of", "VRT", "-expand", "rgba", *gcps, "-a_srs", "EPSG:4326", str(img), str(vrt)],
                   check=False)
    if not vrt.exists() or vrt.stat().st_size == 0:  # paletový obrázek bez -expand
        subprocess.run(["gdal_translate", "-q", "-of", "VRT", *gcps, "-a_srs", "EPSG:4326", str(img), str(vrt)], check=True)
    return vrt


def render_ours(repo: Path, job: Path, tmp: Path, res: float) -> tuple[Path, Path]:
    sys.path.insert(0, str(repo))
    from app.pipeline.oom_preview import render_omap_xml_png, write_preview_pgw
    omap = next((job / "output").glob("*.omap"))
    scale = int(re.search(r'<georeferencing scale="(\d+)"', omap.read_text(encoding="utf-8")).group(1))
    png = tmp / "ours.png"
    r = render_omap_xml_png(omap, png, max_side=20000, target_dpi=int(round(25.4 / (res * 1000.0 / scale))),
                            undo_grivation=False)
    write_preview_pgw(png.with_suffix(".pgw"), *r[3:], with_grivation=True)
    # .prj z náhledu nese CRS procesu (S-JTSK); systém mapy je v .omap
    spec = re.search(r'<spec language="PROJ.4">([^<]+)</spec>', omap.read_text(encoding="utf-8")).group(1)
    return png, spec


def metrics(cls: np.ndarray, mask: np.ndarray, res: float, scale: int) -> dict:
    n = int(mask.sum())
    frac = {c: float(((cls == i) & mask).sum()) / max(n, 1) for i, c in enumerate(CLASSES)}
    land = mask & (cls != CLASSES.index("course"))
    green = np.isin(cls, [CLASSES.index(c) for c in ("green1", "green2", "green3")])
    line_m = CONTOUR_MM * scale / 1000.0
    brown_m2 = frac["brown"] * n * res * res
    area_km2 = n * res * res / 1e6
    return {
        "area_km2": round(area_km2, 3),
        "white": frac["white"], "open": frac["open"] + frac["rough_open"], "green": float((green & land).sum()) / max(n, 1),
        "green1": frac["green1"], "green2": frac["green2"], "green3": frac["green3"],
        "water": float((_area_mask(cls, ("water",), res) & mask).sum()) / max(n, 1),  # bez modrých severek
        "black": frac["black"], "olive": frac["olive"], "grey": frac["grey"],
        "paved": frac["paved"], "course": frac["course"],
        # délka vrstevnic (km na km²) z plochy hnědé děleno šířkou čáry v terénu
        "contour_km_per_km2": round(brown_m2 / line_m / 1000.0 / max(area_km2, 1e-9), 2),
    }


def align(co: np.ndarray, cr: np.ndarray, valid: np.ndarray, res: float, max_m: float = 40.0) -> tuple[int, int]:
    """Posun reference (řádky, sloupce) s největší shodou černé kresby (cesty, budovy) – georeference
    z rohů obrázku Liveloxu bývá posunutá o metry až desítky metrů."""
    k = CLASSES.index("black")
    a = ndi.gaussian_filter(((co == k) & valid).astype(np.float32), 2 / res)
    b = ndi.gaussian_filter(((cr == k) & valid).astype(np.float32), 2 / res)
    a -= a.mean(); b -= b.mean()
    corr = np.fft.irfft2(np.fft.rfft2(a) * np.conj(np.fft.rfft2(b)), s=a.shape)
    m = int(max_m / res)
    best, bv = (0, 0), -np.inf
    for dy in range(-m, m + 1):
        for dx in range(-m, m + 1):
            v = corr[dy % a.shape[0], dx % a.shape[1]]
            if v > bv:
                bv, best = v, (dy, dx)
    return best


def f1(a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> float | None:
    a, b = a & mask, b & mask
    tp = float((a & b).sum()); denom = float(a.sum() + b.sum())
    return None if denom == 0 else round(2 * tp / denom, 3)


def _area_mask(cls: np.ndarray, names: tuple[str, ...], res: float) -> np.ndarray:
    """Plošná maska třídy bez tenkých čar (otevření 3 m) – vrstevnice a cesty přes plochu se zacelí."""
    m = np.isin(cls, [CLASSES.index(c) for c in names])
    it = max(1, int(round(1.5 / res)))
    return ndi.binary_closing(ndi.binary_opening(m, iterations=it), iterations=it * 2)


def measure(repo: Path, job: Path, ref_dir: Path, kind: str, ref_scale: int, bbox, debug: Path | None = None) -> dict:
    res = 1.0 if kind == "les" else 0.5
    tmp = Path(tempfile.mkdtemp())
    png, prj = render_ours(repo, job, tmp, res)
    our_scale = int(re.search(r'<georeferencing scale="(\d+)"',
                              next((job / "output").glob("*.omap")).read_text(encoding="utf-8")).group(1))
    lon, lat = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    epsg = _utm_epsg(lon, lat)
    # rozsah naší mapy v UTM
    tmp_utm = tmp / "o_full.tif"
    subprocess.run(["gdalwarp", "-q", "-overwrite", "-s_srs", str(prj), "-t_srs", f"EPSG:{epsg}", "-tr", str(res), str(res),
                    "-r", "near", "-dstalpha", str(png), str(tmp_utm)], check=True)
    gi = json.loads(subprocess.run(["gdalinfo", "-json", str(tmp_utm)], capture_output=True, text=True, check=True).stdout)
    cc = gi["cornerCoordinates"]; te = (cc["lowerLeft"][0], cc["lowerLeft"][1], cc["upperRight"][0], cc["upperRight"][1])
    ours, om = _warp(png, tmp / "o.tif", str(prj), epsg, te, res)
    ref, rm = _warp(ref_with_gcps(ref_dir, tmp), tmp / "r.tif", "EPSG:4326", epsg, te, res, extra=("-order", "1"))
    co, cr = classify(ours), classify(ref)
    shift = align(co, cr, om & rm, res)
    ref, rm, cr = (np.roll(x, shift, axis=(0, 1)) for x in (ref, rm, cr))
    # okraj 30 m pryč (legenda, rámeček, ořez mapy), přetisk tratí z masky
    mask = ndi.binary_erosion(om & rm, iterations=int(30 / res))
    course = (cr == CLASSES.index("course")) | (co == CLASSES.index("course"))  # přetisk tratí, náš rámeček mapy
    mask &= ~ndi.binary_dilation(course, iterations=int(2 / res))
    if debug:
        debug.mkdir(parents=True, exist_ok=True)
        Image.fromarray(ours).save(debug / "ours.jpg", quality=80); Image.fromarray(ref).save(debug / "ref.jpg", quality=80)
    mo, mr = metrics(co, mask, res, our_scale), metrics(cr, mask, res, ref_scale)
    green = ("green1", "green2", "green3")
    return {
        "ours": mo, "ref": mr, "our_scale": our_scale, "ref_scale": ref_scale, "epsg": epsg,
        "shift_m": [round(shift[1] * res, 1), round(-shift[0] * res, 1)],
        "f1_green": f1(_area_mask(co, green, res), _area_mask(cr, green, res), mask),
        "f1_white": f1(_area_mask(co, ("white",), res), _area_mask(cr, ("white",), res), mask),
        "f1_open": f1(_area_mask(co, ("open", "rough_open"), res), _area_mask(cr, ("open", "rough_open"), res), mask),
    }


if __name__ == "__main__":
    a = sys.argv[1:]
    dbg = None
    if "--debug" in a:
        i = a.index("--debug"); dbg = Path(a[i + 1]); del a[i:i + 2]
    r = measure(Path(a[0]), Path(a[1]), Path(a[2]), a[3], int(a[4]), [float(x) for x in a[5].split(",")], dbg)
    s = json.dumps(r, indent=1)
    if len(a) > 6:
        Path(a[6]).write_text(s)
    print(s)
