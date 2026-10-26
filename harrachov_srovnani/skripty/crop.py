"""Výřez reference s mřížkou souřadnic pixelů. crop.py out.png cx cy half [scale]"""
import sys
from PIL import Image, ImageDraw
Image.MAX_IMAGE_PIXELS = None
REF = r"C:\Users\PetrJanecek\source\repos\podkladarna\local_test\harrachov_srovnani\ref\harrachov_ref.png"
out, cx, cy, half = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
sc = float(sys.argv[5]) if len(sys.argv) > 5 else 1.0
im = Image.open(REF).convert("RGB").crop((cx - half, cy - half, cx + half, cy + half))
im = im.resize((int(im.width * sc), int(im.height * sc)))
d = ImageDraw.Draw(im)
step = 100
for v in range((cx - half) // step * step, cx + half + 1, step):
    x = (v - (cx - half)) * sc
    d.line([(x, 0), (x, 6)], fill="red"); d.text((x + 1, 6), str(v), fill="red")
for v in range((cy - half) // step * step, cy + half + 1, step):
    y = (v - (cy - half)) * sc
    d.line([(0, y), (6, y)], fill="red"); d.text((7, y + 1), str(v), fill="red")
im.save(out)
