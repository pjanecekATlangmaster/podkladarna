"""Maska mapového pole reference (pixely 1/8) -> ref/maska_8.png + kontrolní obrázek."""
import sys
import numpy as np
from PIL import Image
from scipy import ndimage as ndi
Image.MAX_IMAGE_PIXELS = None
B = r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_srovnani"
im = Image.open(B + r"\ref\harrachov_ref.png").convert("RGB")
F = 8
sm = np.asarray(im.resize((im.width // F, im.height // F), Image.BOX)).astype(int)
H, W, _ = sm.shape
nonwhite = (sm.min(axis=2) < 235)
nonwhite[: 160 // F + 5, :] = False          # horní pruh Livelox
nonwhite[int(H * 0.915):, :] = False          # spodní zelený pás
nonwhite[:, : int(W * 0.27)] = False          # levý panel s legendou
m = ndi.binary_closing(nonwhite, structure=np.ones((3, 3)), iterations=4)
m = ndi.binary_fill_holes(m)
m = ndi.binary_opening(m, structure=np.ones((3, 3)), iterations=6)
lab, n = ndi.label(m)
sizes = ndi.sum(m, lab, range(1, n + 1))
big = 1 + int(np.argmax(sizes))
m = lab == big
m = ndi.binary_fill_holes(m)
Image.fromarray((m * 255).astype(np.uint8)).save(B + r"\ref\maska_8.png")
vis = sm.copy().astype(np.uint8)
edge = m ^ ndi.binary_erosion(m, iterations=2)
vis[edge] = (255, 0, 0)
Image.fromarray(vis).save(sys.argv[1])
print(W, H, int(m.sum()), "px1/8 =", round(m.sum() * (F * 0.44406) ** 2 / 1e6, 2), "km2")
