"""Otevřený terén 401 → louka / drsný otevřený terén 403 podle ortofota.

LiDAR (hustota odrazů, CHM) louku od paseky s náletem nebo mladého porostu
neodliší: obojí má CHM ≈ 0. Ortofoto ano – louka je hladká, nálet drsný.
Rozhoduje předučená logistická regrese nad rysy z ortofota (barva, ExG,
sytost, textura 2,5 m a 11 m). Rysy se v každém jobu normují vůči naší 401
v témže snímku (z-skóre), takže koeficienty platí i při jiném ročním období
a osvětlení. Učeno na ručních mapách (Ochoza, Harrachov, Vyšší Brod E1–E3,
štítky = 401 / 403 mapaře uvnitř naší 401); při vynechání území z učení
401 F1 Ochoza 0,82, Harrachov 0,75, VB E2 0,67 (dnes 0,74 / 0,58 / 0,36);
s ucelenými bloky (okno 25 m, min. 2 500 m²) 0,77 / 0,59 / 0,48 a podíl 403
na Ochoze jako u mapaře, viz
``/mnt/project-files/ortofoto/ortofoto-pruzkum.md``. Bez ortofota se nic nemění.
Starší DMP 1G se nepoužívá (ČÚZK ho přestane poskytovat).
"""

from __future__ import annotations

from pathlib import Path

# Hlavní vypínač (les a MTBO; sprint nevolá).
ORTHO_ROUGH_OPEN = True
ORTHO_MPP = 0.5
# Koeficienty pro 12 rysů ``ortho_features`` (z-skóre vůči 401 v jobu) a posun.
ROUGH_COEF = (-0.165, 1.082, -0.550, -0.038, -0.125, 0.141, -0.454, 3.058, -0.934, 0.176, -2.133, -1.539)
ROUGH_INTERCEPT = 0.246
ROUGH_PROBA = 0.5
MIN_OPEN_PX = 1000
# Úklid masky 403 (m): průměr pravděpodobnosti v okně, pak uzavření a otevření.
PROBA_WINDOW_M = 25.0
CLEAN_M = 2.0
# Kusy 403 a ostrůvky louky uvnitř 403 menší než tohle (25 mm² v 1 : 10 000) se slijí s okolím:
# 403 jen v ucelených blocích, ne přelévání s 401 (Petr 2026-10-27).
ROUGH_MIN_M2 = 2500.0


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
    import numpy as np
    from scipy import ndimage as ndi

    it = max(1, int(round(CLEAN_M / cell_m)))
    mask = ndi.binary_opening(ndi.binary_closing(mask, iterations=it), iterations=it) & open_mask
    min_px = ROUGH_MIN_M2 / (cell_m * cell_m)
    lab, _n = ndi.label(mask)
    mask &= ~(np.bincount(lab.ravel())[lab] < min_px)
    # Ostrůvek louky celý uvnitř 403 (nedotýká se lesa) → 403.
    lab, _n = ndi.label(open_mask & ~mask)
    small = np.bincount(lab.ravel()) < min_px
    small[np.unique(lab[ndi.binary_dilation(~open_mask)])] = False
    small[0] = False
    return mask | small[lab]


def rough_open_mask(classified, feats, cell_m: float, *, open_cls: int = 1, log=None):
    """bool maska pixelů 401, které jsou drsný otevřený terén (403)."""
    import numpy as np
    from scipy import ndimage as ndi

    open_mask = classified == open_cls
    if int(open_mask.sum()) < MIN_OPEN_PX:
        return np.zeros_like(open_mask)
    f = feats[open_mask].astype(np.float64)
    z = (f - f.mean(axis=0)) / (f.std(axis=0) + 1e-6)
    logit = z @ np.asarray(ROUGH_COEF) + ROUGH_INTERCEPT
    proba = np.zeros(open_mask.shape, dtype=np.float32)
    proba[open_mask] = 1.0 / (1.0 + np.exp(-logit))
    proba = ndi.uniform_filter(proba, max(1, int(round(PROBA_WINDOW_M / cell_m))))
    return _clean(proba > ROUGH_PROBA, open_mask, cell_m)


def split_open_by_orthophoto(classified, gt, work_dir: Path, *, rough_cls: int, open_cls: int = 1, log=None):
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
    rough = rough_open_mask(classified, feats, cell_m, open_cls=open_cls, log=log)
    out = classified.copy()
    out[rough] = rough_cls
    if log:
        log(
            f"Ortofoto 401/403 (předučený model, bloky ≥ {ROUGH_MIN_M2:.0f} m²): "
            f"{int(rough.sum()) * cell_m**2 / 1e4:.1f} ha 401 → 403"
        )
    return out
