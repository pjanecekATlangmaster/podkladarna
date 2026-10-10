"""Drobné samostatné plošky vegetace: vyplnit značkou, která je kolem (v pozadí).

Rastr tříd (0 bílý les, 1–4 = 401/406/408/410) → každá souvislá komponenta
(4-okolí) menší než ``min_area_m2`` se přebarví na třídu, kterou má kolem sebe
nejvíc (včetně 0 = bílý les), takže nevznikne bílá díra ve žluté ani naopak.
Přebarvená ploška splyne se sousedem stejné třídy, protože se polygonizuje až
výsledný rastr. Pokud sloučením vznikne z drobných plošek větší, další průchod
je případně zpracuje znovu.
"""

from __future__ import annotations

# Plošky pod tuto plochu (m²) se vyplní okolím. 25 m² = 0,25 mm² na papíře
# v 1 : 10 000, tedy pod hranicí čitelnosti.
VEG_MIN_AREA_M2 = 25.0
_MAX_PASSES = 4

# --- 401 v zástavbě -------------------------------------------------------
# Zástavba = shluky RÚIAN budov: dilatace o SETTLEMENT_JOIN_M spojí domy do
# bloků, eroze o SETTLEMENT_TRIM_M odřízne přesah do polí kolem.
SETTLEMENT_JOIN_M = 20.0
SETTLEMENT_TRIM_M = 12.0
# Blok menší než tohle (osamělý dům na poli) za zástavbu nepovažujeme.
SETTLEMENT_MIN_ZONE_M2 = 3000.0
# V zástavbě: díry v 401 menší než tohle (stromy, keře, auta) se zaplní,
# samostatné ploškám 401 menší než tohle (dvorky, pásky u plotů) se zahodí.
SETTLEMENT_FILL_HOLE_M2 = 300.0
SETTLEMENT_DROP_OPEN_M2 = 150.0
# Okno (m) vyhlazení hranice 401 (většinové hlasování).
SETTLEMENT_SMOOTH_M = 9.0


def _iter_polygon_rings(geojson: dict):
    """Polygony z GeoJSON FeatureCollection: ``[obrys, díra, …]`` (souřadnice x, y)."""
    for feat in geojson.get("features") or []:
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        if gtype == "Polygon":
            yield coords
        elif gtype == "MultiPolygon":
            yield from coords


def rasterize_buildings(geojson: dict, gt, shape: tuple[int, int]):
    """bool maska budov na mřížce ``gt`` (GDAL geotransform), tvar ``shape`` = (řádky, sloupce)."""
    import numpy as np
    from PIL import Image, ImageDraw

    h, w = shape
    img = Image.new("1", (w, h), 0)
    draw = ImageDraw.Draw(img)
    x0, dx, _, y0, _, dy = gt
    for rings in _iter_polygon_rings(geojson):
        for i, ring in enumerate(rings):
            pts = [((x - x0) / dx, (y - y0) / dy) for x, y, *_ in ring]
            if len(pts) >= 3:
                draw.polygon(pts, fill=1 if i == 0 else 0)
    return np.asarray(img, dtype=bool)


def settlement_zone(buildings, pixel_m: float):
    """Maska zástavby z masky budov (viz konstanty ``SETTLEMENT_*``)."""
    import numpy as np
    from scipy import ndimage

    if not buildings.any():
        return np.zeros_like(buildings, dtype=bool)
    join = max(1, int(round(SETTLEMENT_JOIN_M / pixel_m)))
    trim = max(0, int(round(SETTLEMENT_TRIM_M / pixel_m)))
    # Kruhové strukturní prvky; padding, ať eroze nepodřízne okraj rastru.
    def disk(r):
        yy, xx = np.ogrid[-r : r + 1, -r : r + 1]
        return xx * xx + yy * yy <= r * r

    pad = join + 1
    padded = np.pad(buildings, pad)
    zone = ndimage.binary_dilation(padded, structure=disk(join))
    if trim:
        zone = ndimage.binary_erosion(zone, structure=disk(trim))
    zone = zone[pad:-pad, pad:-pad]
    labels, n = ndimage.label(zone)
    if n:
        sizes = np.bincount(labels.ravel(), minlength=n + 1)
        keep = sizes * pixel_m * pixel_m >= SETTLEMENT_MIN_ZONE_M2
        keep[0] = False
        zone = keep[labels]
    return zone


def simplify_open_in_settlements(classified, buildings, pixel_m: float):
    """V zástavbě zjednoduší 401 (třída 1): zaplní díry, zahodí drobné plošky, vyhladí hranici.

    Mimo zástavbu (pole, louky) se nic nemění. ``buildings`` = bool maska budov
    na mřížce ``classified``.
    """
    import numpy as np

    arr = np.asarray(classified, dtype=np.uint8)
    zone = settlement_zone(buildings, pixel_m)
    if not zone.any():
        return arr
    try:
        from scipy import ndimage
    except ImportError:
        return arr
    px_m2 = pixel_m * pixel_m
    out = arr.copy()
    openm = arr == 1

    # 1) Díry v 401 (stromy, keře, budovy mezi dvory) – jen malé, uzavřené.
    from app.pipeline.vegetation_density import fill_small_holes

    filled = fill_small_holes(openm, max_hole_px=int(SETTLEMENT_FILL_HOLE_M2 / px_m2))
    openm = openm | (filled & zone)

    # 2) Drobné samostatné plošky 401 uvnitř zástavby pryč.
    labels, n = ndimage.label(openm)
    if n:
        sizes = np.bincount(labels.ravel(), minlength=n + 1)
        small = sizes * px_m2 < SETTLEMENT_DROP_OPEN_M2
        small[0] = False
        openm = openm & ~(small[labels] & zone)

    # 3) Vyhlazení hranice většinovým hlasováním (jen v zástavbě).
    size = max(3, int(round(SETTLEMENT_SMOOTH_M / pixel_m)) | 1)
    vote = ndimage.uniform_filter(openm.astype(np.float32), size=size, mode="nearest")
    openm = np.where(zone, vote > 0.5, openm)

    was_open = arr == 1
    out[zone & openm & ~was_open] = 1
    # Zahozená 401 → bílá (0) – nic se nekreslí, uživatel doplní ručně.
    out[zone & was_open & ~openm] = 0
    return out


def merge_small_regions(
    classified,
    pixel_area_m2: float,
    *,
    min_area_m2: float = VEG_MIN_AREA_M2,
):
    """Vrátí kopii ``classified`` bez drobných plošek (viz modul)."""
    import numpy as np

    arr = np.asarray(classified, dtype=np.uint8)
    if pixel_area_m2 <= 0 or min_area_m2 <= 0 or not arr.any():
        return arr
    try:
        from scipy import ndimage
    except ImportError:
        return arr
    min_px = int(np.ceil(min_area_m2 / pixel_area_m2))
    if min_px <= 1:
        return arr

    out = arr.copy()
    cross = ndimage.generate_binary_structure(2, 1)
    h, w = out.shape
    for _ in range(_MAX_PASSES):
        changed = False
        snapshot = out.copy()
        for cls in range(0, 5):
            labels, n = ndimage.label(snapshot == cls, structure=cross)
            if n == 0:
                continue
            sizes = np.bincount(labels.ravel(), minlength=n + 1)
            small = np.flatnonzero(sizes[1:] < min_px) + 1
            if small.size == 0:
                continue
            slices = ndimage.find_objects(labels)
            for lab in small:
                sl = slices[lab - 1]
                y0, y1 = max(sl[0].start - 1, 0), min(sl[0].stop + 1, h)
                x0, x1 = max(sl[1].start - 1, 0), min(sl[1].stop + 1, w)
                comp = labels[y0:y1, x0:x1] == lab
                ring = ndimage.binary_dilation(comp, structure=cross) & ~comp
                neigh = snapshot[y0:y1, x0:x1][ring]
                if neigh.size == 0:
                    continue  # celý rastr je jedna ploška
                counts = np.bincount(neigh, minlength=5)
                counts[cls] = 0
                out[y0:y1, x0:x1][comp] = int(counts.argmax())
                changed = True
        if not changed:
            break
    return out
