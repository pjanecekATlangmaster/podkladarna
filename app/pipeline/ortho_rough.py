"""Otevřený terén 401 → louka / drsný otevřený terén 403 podle ortofota.

LiDAR (hustota odrazů, CHM) louku od paseky s náletem nebo mladého porostu
neodliší: obojí má CHM ≈ 0. Ortofoto ano – louka je hladká, nálet drsný.
Pevný práh textury se ale mezi snímky nepřenáší (roční období, slunce), proto
se v každém jobu naučí malý klasifikátor ze štítků, které dává LiDAR sám:

* **louka** = 401, kde je otevřeno ve starém DMP 1G i v současném DMP OK,
* **paseka / nálet** = 401, kde DMP 1G měl les a DMP OK už ne.

Rysy jsou jen z ortofota (barva, ExG, sytost, textura 2,5 m a 11 m). Bez
štítků nebo bez ``scikit-learn`` rozhodne textura proti lesu v témže snímku
(drsnější než ``TEXTURE_FOREST_FRAC`` × medián lesa → 403). Bez ortofota se
nic nemění. Měřeno na 5 ručních mapách (Ochoza, Harrachov, Vyšší Brod E1–E3),
viz ``/mnt/project-files/ortofoto/ortofoto-pruzkum.md``.
"""

from __future__ import annotations

import io
import urllib.parse
import urllib.request
from pathlib import Path

# Hlavní vypínač (les a MTBO; sprint nevolá).
ORTHO_ROUGH_OPEN = True
ORTHO_MPP = 0.5
IMAGE_SERVER = "https://ags.cuzk.gov.cz/arcgis2/rest/services/{service}/ImageServer/exportImage"
# Štítky z LiDARu (m, průměr v okně LABEL_WINDOW_M).
LABEL_WINDOW_M = 11.0
MEADOW_MAX_M = 0.5
CLEARED_OLD_MIN_M = 8.0
CLEARED_NOW_MAX_M = 2.0
MIN_LABEL_M2 = 2_000.0
MAX_TRAIN_PER_CLASS = 40_000
ROUGH_PROBA = 0.5
TEXTURE_FOREST_FRAC = 0.75
# Úklid masky 403 (m): uzavření a otevření, ať nezůstanou pixelové fleky.
CLEAN_M = 2.0


def fetch_dmp1g(bbox_5514, shape, *, timeout: float = 180.0, log=None):
    """DMP 1G (starší LiDAR povrch) z ImageServer ČÚZK na mřížce ``shape`` = (H, W), nebo ``None``."""
    import numpy as np
    from PIL import Image

    xmin, ymin, xmax, ymax = (float(v) for v in bbox_5514)
    h, w = shape
    tile = 2000
    out = np.full((h, w), np.nan, dtype=np.float32)
    px, py = (xmax - xmin) / w, (ymax - ymin) / h
    try:
        for r0 in range(0, h, tile):
            for c0 in range(0, w, tile):
                tw, th = min(tile, w - c0), min(tile, h - r0)
                bx0, by1 = xmin + c0 * px, ymax - r0 * py
                q = urllib.parse.urlencode(
                    {
                        "bbox": f"{bx0},{by1 - th * py},{bx0 + tw * px},{by1}",
                        "bboxSR": 5514,
                        "imageSR": 5514,
                        "size": f"{tw},{th}",
                        "format": "tiff",
                        "pixelType": "F32",
                        "interpolation": "RSP_BilinearInterpolation",
                        "f": "image",
                    }
                )
                url = IMAGE_SERVER.format(service="dmp1g") + "?" + q
                with urllib.request.urlopen(url, timeout=timeout) as resp:
                    arr = np.asarray(Image.open(io.BytesIO(resp.read())), dtype=np.float32)
                out[r0 : r0 + th, c0 : c0 + tw] = arr[:th, :tw]
    except Exception as exc:  # síť / formát: rozhodne textura
        if log:
            log(f"⚠ Ortofoto 401/403: DMP 1G nestaženo ({exc})")
        return None
    return out


def _resize(arr, shape):
    import numpy as np
    from PIL import Image

    h, w = shape
    return np.asarray(Image.fromarray(np.asarray(arr, dtype=np.float32), mode="F").resize((w, h), Image.BILINEAR))


def _local_std(a, size: int):
    import numpy as np
    from scipy import ndimage as ndi

    m = ndi.uniform_filter(a, size)
    m2 = ndi.uniform_filter(a * a, size)
    return np.sqrt(np.maximum(m2 - m * m, 0.0))


def ortho_features(rgb, shape, cell_m: float, ortho_mpp: float = ORTHO_MPP):
    """Rysy pro mřížku vegetace ``shape``: (H, W, 12) float32 – barva, ExG, sytost, textura."""
    import numpy as np
    from scipy import ndimage as ndi

    from app.pipeline.orthophoto import exg, saturation

    a = np.asarray(rgb, dtype=np.float32)
    gray = a.mean(axis=-1)
    tex_px = max(3, int(round(2.5 / ortho_mpp)) | 1)
    tex = _resize(_local_std(gray, tex_px), shape)
    r, g, b = (_resize(a[..., i], shape) for i in range(3))
    e = _resize(exg(a), shape)
    s = _resize(saturation(a), shape)
    k = lambda m: max(1, int(round(m / cell_m)))  # noqa: E731
    u = lambda x, m: ndi.uniform_filter(x, k(m))  # noqa: E731
    lum = r + g + b
    return np.stack(
        [r, g, b, e, s, tex, u(tex, 5), u(tex, 11), u(e, 11), u(s, 11), u(lum, 11), _local_std(lum, k(11))],
        axis=-1,
    ).astype(np.float32)


def _clean(mask, open_mask, cell_m: float):
    from scipy import ndimage as ndi

    it = max(1, int(round(CLEAN_M / cell_m)))
    return ndi.binary_opening(ndi.binary_closing(mask, iterations=it), iterations=it) & open_mask


def rough_open_mask(classified, feats, cell_m: float, *, chm_now=None, chm_old=None, open_cls: int = 1, log=None):
    """bool maska pixelů 401, které jsou drsný otevřený terén (403)."""
    import numpy as np
    from scipy import ndimage as ndi

    open_mask = classified == open_cls
    if not open_mask.any():
        return open_mask
    k = max(1, int(round(LABEL_WINDOW_M / cell_m)))
    tex11 = feats[..., 7]
    if chm_now is not None and chm_old is not None:
        old = ndi.uniform_filter(np.nan_to_num(chm_old), k)
        now = ndi.uniform_filter(np.nan_to_num(chm_now), k)
        meadow = open_mask & (old < MEADOW_MAX_M) & (now < MEADOW_MAX_M)
        cleared = open_mask & (old > CLEARED_OLD_MIN_M) & (now < CLEARED_NOW_MAX_M)
        min_px = MIN_LABEL_M2 / (cell_m * cell_m)
        if meadow.sum() >= min_px and cleared.sum() >= min_px:
            try:
                from sklearn.ensemble import HistGradientBoostingClassifier

                rng = np.random.default_rng(0)
                im, ic = np.flatnonzero(meadow.ravel()), np.flatnonzero(cleared.ravel())
                n = min(len(im), len(ic), MAX_TRAIN_PER_CLASS)
                idx = np.concatenate([rng.choice(im, n, replace=False), rng.choice(ic, n, replace=False)])
                flat = feats.reshape(-1, feats.shape[-1])
                y = np.r_[np.zeros(n), np.ones(n)]
                clf = HistGradientBoostingClassifier(max_iter=200, max_leaf_nodes=31, random_state=0)
                clf.fit(flat[idx], y)
                proba = np.zeros(open_mask.size, dtype=np.float32)
                io_ = np.flatnonzero(open_mask.ravel())
                proba[io_] = clf.predict_proba(flat[io_])[:, 1]
                proba = ndi.uniform_filter(proba.reshape(open_mask.shape), max(1, int(round(5 / cell_m))))
                if log:
                    log(
                        "Ortofoto 401/403: model naučen ze štítků LiDARu "
                        f"(louka {meadow.sum() * cell_m**2 / 1e4:.1f} ha, paseka {cleared.sum() * cell_m**2 / 1e4:.1f} ha)"
                    )
                return _clean(proba > ROUGH_PROBA, open_mask, cell_m)
            except ImportError:
                pass
    forest = (classified != open_cls) & (feats[..., 0] > 0)
    if forest.sum() < 1000:
        return np.zeros_like(open_mask)
    thr = TEXTURE_FOREST_FRAC * float(np.median(tex11[forest]))
    if log:
        log(f"Ortofoto 401/403: práh textury {thr:.1f} (bez štítků z LiDARu)")
    return _clean(open_mask & (tex11 > thr), open_mask, cell_m)


def split_open_by_orthophoto(classified, gt, work_dir: Path, *, chm=None, rough_cls: int, open_cls: int = 1, log=None):
    """Rastr vegetace s 401 rozdělenou na 401 / ``rough_cls`` (403). Bez ortofota beze změny."""
    import numpy as np

    from app.pipeline.orthophoto import fetch_orthophoto

    if not ORTHO_ROUGH_OPEN or not np.any(classified == open_cls):
        return classified
    h, w = classified.shape
    cell_m = abs(float(gt[1]))
    bbox = (gt[0], gt[3] - h * cell_m, gt[0] + w * cell_m, gt[3])
    got = fetch_orthophoto(work_dir, bbox, ORTHO_MPP, log=log)
    if got is None:
        return classified
    rgb, _ogt = got
    feats = ortho_features(rgb, (h, w), cell_m)
    chm_old = None
    if chm is not None:
        from app.pipeline.dem_prep import DEM_DIR_NAME
        from app.pipeline.gdal_cli_raster import read_float32_geotiff

        dem_tif = Path(work_dir) / DEM_DIR_NAME / "dem_filled.tif"
        dmp1g = fetch_dmp1g(bbox, (h, w), log=log)
        if dmp1g is not None and dem_tif.is_file():
            dem, _g, _nd = read_float32_geotiff(dem_tif, log=log)
            dem = np.asarray(dem, dtype=np.float32)
            if dem.shape == (h, w):
                chm_old = np.clip(np.nan_to_num(dmp1g - dem), 0, 50)
    rough = rough_open_mask(
        classified, feats, cell_m, chm_now=chm, chm_old=chm_old, open_cls=open_cls, log=log
    )
    out = classified.copy()
    out[rough] = rough_cls
    if log:
        log(f"Ortofoto 401/403: {int(rough.sum()) * cell_m**2 / 1e4:.1f} ha 401 → 403")
    return out
