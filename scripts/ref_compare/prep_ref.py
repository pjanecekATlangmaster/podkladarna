"""prep_ref.py URL EPSG M00 M01 M02 M10 M11 M12 OUT.png – stáhne mapu z Liveloxu a zapíše OUT.pgw (EPSG:5514) a OUT.json (quad_5514).
Matice je Livelox map.projection.matrix (souřadnice EPSG -> pixel)."""
import sys, json, urllib.request
import numpy as np
from PIL import Image
from pyproj import Transformer
Image.MAX_IMAGE_PIXELS = None
url, epsg = sys.argv[1], int(sys.argv[2])
M = np.array([[float(v) for v in sys.argv[3:6]], [float(v) for v in sys.argv[6:9]], [0, 0, 1]])
out = sys.argv[9]
tmp = out + ".download"
urllib.request.urlretrieve(url, tmp)
im = Image.open(tmp); im.save(out); w, h = im.size
A = np.linalg.inv(M)
t = Transformer.from_crs(epsg, 5514, always_xy=True)
src = [(x, y) for x in (0, w / 2, w) for y in (0, h / 2, h)]
dst = [t.transform(*(A @ [x + .5, y + .5, 1])[:2]) for x, y in src]
X = np.array([[x, y, 1] for x, y in src]); sol = np.linalg.lstsq(X, np.array(dst), rcond=None)[0]
a, b, c = sol[:, 0]; d, e, f = sol[:, 1]
open(out[:-4] + ".pgw", "w").write("\n".join(str(v) for v in (a, d, b, e, c, f)))
quad = [t.transform(*(A @ [x, y, 1])[:2]) for x, y in [(0, 0), (w, 0), (w, h), (0, h)]]
json.dump({"quad_5514": {"vertices": [{"x": x, "y": y} for x, y in quad]}}, open(out[:-4] + ".json", "w"), indent=1)
print(out, w, h, "m/px", round(float(np.hypot(a, d)), 3))
