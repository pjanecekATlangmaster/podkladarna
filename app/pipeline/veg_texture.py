"""Zeleň ze zrnitosti porostu mimo Česko (les a MTBO, 3.67.0).

Bez hustoty bodů (zahraniční výšky jsou rastry) byl les skoro celý bílý. Mapař kreslí zeleň hlavně v mezerovitém,
smíšeném a mladém porostu, kde výška porostu na pár metrech hodně kolísá. Pravidlo:

- ``A`` = dosavadní zeleň (z výšky porostu nebo hustoty bodů) ∪ mladý porost z CHM (``young_stand_to_green``),
- ``T`` = zrnitost: směrodatná odchylka CHM v okně ``WINDOW_M`` nad ``STD_M`` při průměru nad ``MEAN_M``, jen
  v lesním okolí (``FOREST_SHARE`` okna ``FOREST_WINDOW_M`` má CHM nad ``MEAN_M``; zástavba a solitéry ne),
- ``A ∪ T`` → 406, ``A ∩ T`` → 408, 410 zůstává; maska se uzavře kruhem ``CLOSE_M`` (zrnitost je vysoká hlavně
  na okrajích mezer, bez uzavření vznikají „nudle“), vyhladí většinovým filtrem ``SMOOTH_M`` (jako medián 7 px
  v Česku; tvar obrysu dělá až ``veg_smooth``), drobky pod ``MIN_AREA_M2`` jdou pryč a díry pod ``HOLE_M2`` se zalijí.

Měřeno proti mapám z Liveloxu (F1 zelené, dnes → nově): Bambësch 0,04 → 0,49, Haltern 0,26 → 0,54; hranice zelené
do 2 / 5 m od reference jako u mračna COPC úrovně 0–1 (`/mnt/project-files/evropa-data/zahranicni-build/zelen/`).
Česko a sprint beze změny.

Režim ``understory`` (``countries.yaml`` ``green: understory``, Trentino, 3.71.0): horský les přestřeloval zelenou
2,5–5× (Montevaccino 41 % proti 15 % u mapaře, Lavarone 37 % proti 7 %), protože mladý porost do 12 m pokryl půlku
nižšího lesa a zrnitost bere i běžnou drsnost vysokých smrčin. Místo ``A`` a ``T`` se bere podíl nízkého patra:
v okně ``UNDER_WINDOW_M`` aspoň ``UNDER_SHARE`` pixelů CHM ``UNDER_LOW_M``–``UNDER_HIGH_M`` (mlází a podrost v mezerách)
v lesním okolí → 406, čištění masky stejné. Montevaccino zeleň 23 %, F1 0,33 → 0,31, Lavarone 14 %, F1 0,22 → 0,26
(`/mnt/project-files/evropa-data/zahranicni-build/it/zrnitost/`).
"""

from __future__ import annotations

import numpy as np

WINDOW_M = 9.0
STD_M = 4.5
MEAN_M = 6.0
FOREST_WINDOW_M = 31.0
FOREST_SHARE = 0.6
SMOOTH_M = 7.0  # většinový filtr masky 7 × 7 m jako medián 7 px v Česku; tvar obrysu dělá až veg_smooth
MIN_AREA_M2 = 400.0
CLOSE_M = 3.0  # uzavření masky: zrnitost je vysoká hlavně na okrajích mezer, bez něj vznikají „nudle“
HOLE_M2 = 400.0  # díry menší než drobka se zalijí, plochy jsou souvislé jako u mapaře
BOTH_MIN_AREA_M2 = 200.0
UNDER_WINDOW_M = 25.0  # režim ``understory``: okno podílu nízkého patra
UNDER_LOW_M, UNDER_HIGH_M = 1.0, 8.0
UNDER_SHARE = 0.15

WHITE, LIGHT, MID, DENSE = 0, 2, 3, 4  # třídy rastru porostu (vegetation_density / vegetation_chm)


def _odd(px: float) -> int:
    return max(3, int(round(px)) | 1)


def _clean(mask, min_px: int, smooth_px: int = 1, close_px: int = 0, hole_px: int = 0):
    from scipy import ndimage as ndi

    if close_px > 0:
        y, x = np.ogrid[-close_px : close_px + 1, -close_px : close_px + 1]
        mask = ndi.binary_closing(np.pad(mask, close_px), x * x + y * y <= close_px * close_px)[close_px:-close_px, close_px:-close_px]
    if smooth_px > 1:
        mask = ndi.uniform_filter(mask.astype(np.float32), smooth_px) >= 0.5
    mask = ndi.binary_opening(mask, iterations=1)
    lab, n = ndi.label(mask)
    if n == 0:
        return mask
    sizes = np.bincount(lab.ravel())
    keep = sizes >= min_px
    keep[0] = False
    mask = keep[lab]
    if hole_px > 0:
        lab, _ = ndi.label(~mask)
        small = np.bincount(lab.ravel()) < hole_px
        small[0] = False
        mask |= small[lab]
    return mask


def texture_mask(chm, cell_m: float):
    """Zrnitý porost v lesním okolí (bool rastr tvaru ``chm``)."""
    from scipy import ndimage as ndi

    c = np.nan_to_num(np.asarray(chm, dtype=np.float32))
    k = _odd(WINDOW_M / cell_m)
    mean = ndi.uniform_filter(c, k)
    std = np.sqrt(np.maximum(ndi.uniform_filter(c * c, k) - mean * mean, 0.0))
    forest = ndi.uniform_filter((c > MEAN_M).astype(np.float32), _odd(FOREST_WINDOW_M / cell_m)) >= FOREST_SHARE
    return (std > STD_M) & (mean > MEAN_M), forest


def understory_mask(chm, cell_m: float):
    """Režim ``understory``: aspoň ``UNDER_SHARE`` okna má nízké patro ``UNDER_LOW_M``–``UNDER_HIGH_M``, v lesním okolí."""
    from scipy import ndimage as ndi

    c = np.nan_to_num(np.asarray(chm, dtype=np.float32))
    low = ((c >= UNDER_LOW_M) & (c < UNDER_HIGH_M)).astype(np.float32)
    share = ndi.uniform_filter(low, _odd(UNDER_WINDOW_M / cell_m))
    forest = ndi.uniform_filter((c > MEAN_M).astype(np.float32), _odd(FOREST_WINDOW_M / cell_m)) >= FOREST_SHARE
    return (share >= UNDER_SHARE) & forest


def _shares(out):
    return {code: float(np.count_nonzero(out == v)) / out.size * 100 for code, v in (("406", LIGHT), ("408", MID), ("410", DENSE))}


def add_texture_green(classified, chm, cell_m: float, mode: str = "laser"):
    """Třídy porostu (0 bílá, 1 401, 2–4 zeleň, 5 403) doplněné o zeleň ze zrnitosti; vrací (rastr, podíly %).

    ``mode`` = ``laser`` (výchozí) nebo ``understory`` (podíl nízkého patra, horský les v Trentinu).
    """
    from app.pipeline.vegetation_density import DEFAULT_PARAMS, young_stand_to_green

    cl = np.asarray(classified)
    if chm is None or np.asarray(chm).shape != cl.shape:
        return cl, None
    px_m2 = cell_m * cell_m
    if mode == "understory":
        green = np.isin(cl, (LIGHT, MID, DENSE))
        g = _clean(
            green | (understory_mask(chm, cell_m) & (cl == WHITE)),
            max(1, int(round(MIN_AREA_M2 / px_m2))),
            _odd(SMOOTH_M / cell_m),
            int(round(CLOSE_M / cell_m)),
            int(round(HOLE_M2 / px_m2)),
        )
        out = cl.copy()
        out[green] = WHITE
        out[g & (out == WHITE)] = LIGHT
        out[cl == MID] = MID
        out[cl == DENSE] = DENSE
        return out, _shares(out)
    green = np.isin(cl, (LIGHT, MID, DENSE))
    young = young_stand_to_green(np.zeros_like(cl), chm, cell_m, DEFAULT_PARAMS) == MID
    a = green | (young & (cl == WHITE))
    rough, forest = texture_mask(chm, cell_m)
    t = rough & ((forest & (cl == WHITE)) | a)
    sm = _odd(SMOOTH_M / cell_m)
    close = int(round(CLOSE_M / cell_m))
    hole = int(round(HOLE_M2 / px_m2))
    g = _clean(a | t, max(1, int(round(MIN_AREA_M2 / px_m2))), sm, close, hole)
    both = _clean(a & t, max(1, int(round(BOTH_MIN_AREA_M2 / px_m2))), sm, close, hole)
    out = cl.copy()
    out[green] = WHITE
    out[g & (out == WHITE)] = LIGHT
    out[both & g] = MID
    out[cl == DENSE] = DENSE
    return out, _shares(out)
