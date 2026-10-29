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
# Řádky ortofota na jeden pruh při výpočtu rysů v plném rozlišení.
_STRIP_ROWS = 1024


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
    """Rysy pro mřížku vegetace ``shape``: (H, W, 12) float32 – barva, ExG, sytost, textura.

    Plné rozlišení ortofota se počítá po pruzích a do předem alokovaného pole (25 km² = 10 000² px;
    float32 kopie celého RGB by sama vzala 1,2 GB)."""
    import numpy as np
    from scipy import ndimage as ndi

    from app.pipeline.orthophoto import exg, saturation

    src = np.asarray(rgb)
    h0 = src.shape[0]
    gray = np.empty(src.shape[:2], dtype=np.float32)
    e_full = np.empty(src.shape[:2], dtype=np.float32)
    s_full = np.empty(src.shape[:2], dtype=np.float32)
    for y in range(0, h0, _STRIP_ROWS):
        a = src[y : y + _STRIP_ROWS].astype(np.float32)
        gray[y : y + _STRIP_ROWS] = a.mean(axis=-1)
        e_full[y : y + _STRIP_ROWS] = exg(a)
        s_full[y : y + _STRIP_ROWS] = saturation(a)
    tex_px = max(3, int(round(2.5 / ortho_mpp)) | 1)
    out = np.empty((*shape, 12), dtype=np.float32)
    out[..., 5] = _resize(_local_std(gray, tex_px), shape)
    del gray
    for i in range(3):
        out[..., i] = _resize(src[..., i], shape)
    out[..., 3] = _resize(e_full, shape)
    del e_full
    out[..., 4] = _resize(s_full, shape)
    del s_full
    k = lambda m: max(1, int(round(m / cell_m)))  # noqa: E731
    u = lambda x, m: ndi.uniform_filter(x, k(m))  # noqa: E731
    lum = out[..., 0] + out[..., 1] + out[..., 2]
    out[..., 6] = u(out[..., 5], 5)
    out[..., 7] = u(out[..., 5], 11)
    out[..., 8] = u(out[..., 3], 11)
    out[..., 9] = u(out[..., 4], 11)
    out[..., 10] = u(lum, 11)
    out[..., 11] = _local_std(lum, k(11))
    return out


def _clean(mask, open_mask, cell_m: float):
    import numpy as np
    from scipy import ndimage as ndi

    it = max(1, int(round(CLEAN_M / cell_m)))
    mask = ndi.binary_opening(ndi.binary_closing(mask, iterations=it), iterations=it) & open_mask
    min_px = ROUGH_MIN_M2 / (cell_m * cell_m)
    lab, _n = ndi.label(mask)
    mask &= ~(np.bincount(lab.ravel())[lab] < min_px)
    # Malý kus louky, který sousedí s 403 (i proužek mezi 403 a lesem) → 403, ať se 401
    # a 403 nepřelévají. Malá mýtina bez 403 v sousedství zůstane 401.
    lab, n = ndi.label(open_mask & ~mask)
    small = np.bincount(lab.ravel(), minlength=n + 1) < min_px
    near = np.zeros(n + 1, dtype=bool)
    near[np.unique(lab[ndi.binary_dilation(mask)])] = True
    small &= near
    small[0] = False
    return mask | small[lab]


def rough_open_mask(classified, feats, cell_m: float, *, open_cls: int = 1, exclude=None, log=None):
    """bool maska pixelů 401, které jsou drsný otevřený terén (403).

    ``exclude`` (bool maska, např. zástavba): z učení normy i z detekce vynechané –
    zahrady a dvory vypadají v ortofotu každý jinak (Petr 2026-10-27)."""
    import numpy as np
    from scipy import ndimage as ndi

    open_mask = classified == open_cls
    if exclude is not None:
        open_mask &= ~np.asarray(exclude, dtype=bool)
    if int(open_mask.sum()) < MIN_OPEN_PX:
        return np.zeros_like(open_mask)
    # Z-skóre a lineární kombinace po jednotlivých rysech (bez matice N × 12 ve float64).
    logit = np.full(int(open_mask.sum()), ROUGH_INTERCEPT, dtype=np.float64)
    for i, coef in enumerate(ROUGH_COEF):
        f = feats[..., i][open_mask].astype(np.float64)
        logit += coef * (f - f.mean()) / (f.std() + 1e-6)
    proba = np.zeros(open_mask.shape, dtype=np.float32)
    proba[open_mask] = 1.0 / (1.0 + np.exp(-logit))
    del logit
    proba = ndi.uniform_filter(proba, max(1, int(round(PROBA_WINDOW_M / cell_m))))
    return _clean(proba > ROUGH_PROBA, open_mask, cell_m)


def split_open_by_orthophoto(
    classified, gt, work_dir: Path, *, rough_cls: int, open_cls: int = 1, exclude=None, log=None
):
    """Rastr vegetace s 401 rozdělenou na 401 / ``rough_cls`` (403). Bez ortofota beze změny.

    ``exclude`` = maska zástavby: tam zůstane 401 a ortofoto se do ní nepočítá."""
    import numpy as np

    from app.pipeline.orthophoto import fetch_orthophoto

    candidates = classified == open_cls
    if exclude is not None:
        candidates &= ~np.asarray(exclude, dtype=bool)
    if not ORTHO_ROUGH_OPEN or int(candidates.sum()) < MIN_OPEN_PX:
        return classified
    h, w = classified.shape
    cell_m = abs(float(gt[1]))
    bbox = (gt[0], gt[3] - h * cell_m, gt[0] + w * cell_m, gt[3])
    got = fetch_orthophoto(work_dir, bbox, ORTHO_MPP, log=log)
    if got is None:
        return classified
    rgb, _ogt = got
    feats = ortho_features(rgb, (h, w), cell_m)
    del rgb
    rough = rough_open_mask(classified, feats, cell_m, open_cls=open_cls, exclude=exclude, log=log)
    out = classified.copy()
    out[rough] = rough_cls
    if log:
        log(
            f"Ortofoto 401/403 (předučený model, bloky ≥ {ROUGH_MIN_M2:.0f} m²): "
            f"{int(rough.sum()) * cell_m**2 / 1e4:.1f} ha 401 → 403"
        )
    return out
