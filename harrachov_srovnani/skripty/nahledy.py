"""PNG náhledy .omap s .pgw (EPSG:5514, bez deklinace) + reference převzorkovaná do mřížky náhledu + prolnutí."""
import json, shutil, sys
from pathlib import Path
import numpy as np
from PIL import Image
from scipy import ndimage as ndi
sys.path.insert(0, r"C:\Users\PetrJanecek\source\repos\podkladarna-harrachov")
from app.tool_env import apply_local_gis_env; apply_local_gis_env()
from app.pipeline.oom_preview import render_omap_to_png
Image.MAX_IMAGE_PIXELS = None
B = Path(r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_srovnani")
REF = B / "ref"; N = B / "nahledy"; N.mkdir(exist_ok=True)
DPI = 254
names = sys.argv[1:] or [p.stem for p in sorted((B / "mapy").glob("*.omap"))]
for nm in names:
    eng = render_omap_to_png(B / "mapy" / f"{nm}.omap", N / f"{nm}.png", target_dpi=DPI, write_pgw=True,
                             undo_grivation=True, engine="pillow")
    print(nm, eng, Image.open(N / f"{nm}.png").size, flush=True)

# reference do mřížky prvního náhledu (všechny náhledy mají stejnou mřížku – ověřeno níže)
pgws = {p.stem: [float(v) for v in p.read_text().split()] for p in N.glob("Harrachov-*.pgw")}
first = sorted(pgws)[0]
for k, v in pgws.items():
    if any(abs(a - b) > 1e-6 for a, b in zip(v, pgws[first])):
        print("POZOR: jiná mřížka", k, v, pgws[first])
A, D, Bb, E, C, F = pgws[first]
size = Image.open(N / f"{first}.png").size
a, b, _, _, tx, ty = [float(v) for v in (REF / "harrachov_ref.pgw").read_text().split()]
full = np.asarray(Image.open(REF / "harrachov_ref.png").convert("RGB"))
mask = np.asarray(Image.open(REF / "maska_8.png").resize((full.shape[1], full.shape[0]), Image.NEAREST)) > 0
W, H = size
out = np.full((H, W, 4), 0, np.uint8)
for r0 in range(0, H, 1000):
    rows = np.arange(r0, min(H, r0 + 1000))
    cc, rr = np.meshgrid(np.arange(W), rows)
    X = A * cc + Bb * rr + C; Y = D * cc + E * rr + F
    dx, dy = X - tx, Y - ty; d = a * a + b * b
    c = (a * dx + b * dy) / d; r = (b * dx - a * dy) / d
    for k in range(3):
        out[rows[0]:rows[-1] + 1, :, k] = ndi.map_coordinates(full[..., k], [r, c], order=1, cval=255)
    out[rows[0]:rows[-1] + 1, :, 3] = ndi.map_coordinates(mask.astype(np.uint8) * 255, [r, c], order=0, cval=0)
ref = Image.fromarray(out, "RGBA")
ref.save(B / "referencni_mapa_v_mrizce_nahledu.png")
shutil.copy(N / f"{first}.pgw", B / "referencni_mapa_v_mrizce_nahledu.pgw")
shutil.copy(N / f"{first}.prj", B / "referencni_mapa_v_mrizce_nahledu.prj")
# prolnutí 50/50 s les-mixed (jen v mapovém poli reference)
ours_name = "Harrachov-les-mixed" if "Harrachov-les-mixed" in pgws else first
ours = Image.open(N / f"{ours_name}.png").convert("RGBA")
bg = Image.new("RGB", ours.size, "white"); bg.paste(ours, (0, 0), ours)
rbg = Image.new("RGB", ref.size, "white"); rbg.paste(ref, (0, 0), ref)
blend = Image.composite(Image.blend(rbg, bg, 0.5), bg, ref.split()[3])
blend.save(B / f"prolnuti_{ours_name.split('-', 1)[1].replace('-', '_')}_x_reference.png")
shutil.copy(N / f"{first}.pgw", B / f"prolnuti_{ours_name.split('-', 1)[1].replace('-', '_')}_x_reference.pgw")
print("reference v mřížce", size, "m/px", A)
