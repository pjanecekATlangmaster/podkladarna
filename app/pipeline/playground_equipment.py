"""Herní prvky na dětských hřištích ve sprintu (531 ×): OSM body + detekce z CHM a ortofota, generalizace.

Petr 2026-11-06 (průzkum ``/mnt/project-files/herni-prvky-ortofoto/``): dětské hřiště má vždy nějaké herní prvky, OSM je
má jen u ~15 % hřišť. V obrysu ``leisure=playground`` se proto hledají:

- **konstrukce z výšky** (``chm.tif`` z DMP OK): objekt s vrcholem ``TALL_MIN_M``–``TALL_MAX_M`` nad terénem a plochou
  ``AREA_A_M2``, ne zelený (ExG z ortofota), ne v hlubokém stínu, ne okraj zelené koruny, ne budka s plnou střechou;
- **dopadové plochy**: světlá teplá (písek / štěrk) skvrna v trávě, na které něco stojí (stín nebo výška), ne pravoúhlé
  pískoviště.

OSM body mají přednost (vysoké konstrukce před ostatními), detekce do ``OSM_DEDUP_M`` od OSM bodu se zahodí, ploché
prvky (trampolína v zemi, pískoviště…) jen tehdy, když na hřišti nic jiného není. Generalizace: hřiště do
``SMALL_M2`` jeden křížek, větší křížky aspoň ``GAP_MM`` od sebe a nejvýš jeden na ``AREA_PER_X_M2``. Hřiště bez nálezu
dostane jeden záložní křížek ve středu největší otevřené nezelené části (Petr 2026-11-06: hřiště má prvky vždy).
Dopravní hřiště (``name`` s „dopravní“) se přeskakuje. Měření (18 + 17 hřišť): přesnost detekce ~0,85, záloha prvek
trefí jen u čtvrtiny hřišť.
"""

from __future__ import annotations

import math

# Záloha: False = herní prvky jen z OSM jako dřív (bez detekce, bez generalizace).
DETECT = True
# Záložní křížek na hřišti bez OSM bodu i nálezu.
FALLBACK = True

TALL_MIN_M = 1.5
TALL_MAX_M = 8.0  # lanové pyramidy 6–7 m
OBJ_MIN_M = 0.8  # práh objektu nad terénem
AREA_A_M2 = (1.0, 30.0)
TREE_M = 4.0  # zelená koruna nad touto výškou
TREE_SHARE_MAX = 0.3
# Okolí koruny: CHM jobu je maximum DMP OK v 1 m, koruna v něm přesahuje zelenou v ortofotu.
TREE_NEAR_M = 2.0
# Křížek aspoň tak daleko uvnitř obrysu (plot, živý plot a sousední objekty na hraně hřiště ne).
EDGE_INSET_M = 1.0
EXG_MAX = 0.02
DARK_BRI = 55.0
ROOF_AREA_M2 = 8.0
ROOF_P25_M = 1.3
AREA_B_M2 = (4.0, 150.0)
SAND_BRI = 135.0
SAND_WARM = 12.0
GRASS_RING_SHARE = 0.5
SANDPIT_MAX_M2 = 30.0
SANDPIT_RECT_FILL = 0.88
OSM_DEDUP_M = 4.0
SMALL_M2 = 500.0
GAP_MM = 4.0
AREA_PER_X_M2 = 300.0

KIND = "playground_equipment"
# Ploché prvky / povrch: kreslí se jen jako poslední záloha.
FLAT_TYPES = frozenset({"sandpit", "no", "trampoline", "cushion", "splash_pad", "map", "marble_run"})
TALL_TYPES = frozenset(
    {"climbingframe", "structure", "slide", "tower", "swing", "basketswing", "climbingwall", "playhouse",
     "zipwire", "rope_traverse", "climbing_slope", "seesaw", "roundabout"}
)


def is_traffic_playground(name: str | None) -> bool:
    return "dopravn" in str(name or "").lower()


def _exg_bri(rgb):
    import numpy as np

    a = rgb.astype(np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    s = r + g + b + 1e-6
    return (2 * g - r - b) / s, s / 3.0, r - b, (a.max(2) - a.min(2)) / np.maximum(a.max(2), 1.0)


def _grid(ortho, geom, pad_m: float = 3.0):
    """Výřez ortofota kolem ``geom``: (rgb, x0, y_top, mpp) nebo None."""
    h, w = ortho.data.shape[:2]
    bx0, by0, bx1, by1 = geom.buffer(pad_m).bounds
    c0, c1 = max(0, int((bx0 - ortho.x0) / ortho.mpp_x)), min(w, int(math.ceil((bx1 - ortho.x0) / ortho.mpp_x)))
    r0, r1 = max(0, int((ortho.y_top - by1) / ortho.mpp_y)), min(h, int(math.ceil((ortho.y_top - by0) / ortho.mpp_y)))
    if c1 - c0 < 3 or r1 - r0 < 3:
        return None
    return ortho.data[r0:r1, c0:c1, :3], ortho.x0 + c0 * ortho.mpp_x, ortho.y_top - r0 * ortho.mpp_y


def _sample_chm(chm, x0: float, y_top: float, mpp: float, shape):
    """CHM (nejbližší buňka) na mřížce výřezu ortofota; mimo rastr / NoData = 0."""
    import numpy as np

    hh, ww = shape
    xs = x0 + (np.arange(ww) + 0.5) * mpp
    ys = y_top - (np.arange(hh) + 0.5) * mpp
    ci = np.floor((xs - chm.x0) / chm.mpp_x).astype(int)
    ri = np.floor((chm.y_top - ys) / chm.mpp_y).astype(int)
    h, w = chm.data.shape[:2]
    okc, okr = (ci >= 0) & (ci < w), (ri >= 0) & (ri < h)
    out = np.zeros(shape, np.float32)
    sub = chm.data[np.clip(ri, 0, h - 1)][:, np.clip(ci, 0, w - 1)]
    out[np.ix_(okr, okc)] = sub[np.ix_(okr, okc)]
    out[~np.isfinite(out)] = 0.0
    return out


def _poly_mask(geom, x0: float, y_top: float, mpp: float, shape):
    import numpy as np
    from PIL import Image, ImageDraw

    img = Image.new("1", (shape[1], shape[0]), 0)
    draw = ImageDraw.Draw(img)
    polys = getattr(geom, "geoms", [geom])
    for p in polys:
        if p.is_empty or p.geom_type != "Polygon":
            continue
        draw.polygon([((x - x0) / mpp, (y_top - y) / mpp) for x, y in p.exterior.coords], fill=1)
        for ring in p.interiors:
            draw.polygon([((x - x0) / mpp, (y_top - y) / mpp) for x, y in ring.coords], fill=0)
    return np.asarray(img, dtype=bool)


def detect_candidates(geom, ortho, chm, blockers=None) -> list[tuple[float, float, float, str]]:
    """Kandidáti (x, y, skóre, zdroj ``A`` výška / ``AB`` výška na dopadové ploše / ``B`` dopadová plocha) v hřišti."""
    import numpy as np
    from scipy import ndimage as ndi
    from shapely.geometry import MultiPoint, Point

    if ortho is None:
        return []
    got = _grid(ortho, geom)
    if got is None:
        return []
    rgb, x0, y_top = got
    mpp = float(ortho.mpp_x)
    px = mpp * mpp
    shape = rgb.shape[:2]
    exg, bri, warm, sat = _exg_bri(rgb)
    inside = _poly_mask(geom, x0, y_top, mpp, shape)
    nd = _sample_chm(chm, x0, y_top, mpp, shape) if chm is not None else np.zeros(shape, np.float32)

    def _pt(cm):
        cy, cx = ndi.center_of_mass(cm)
        return Point(x0 + (cx + 0.5) * mpp, y_top - (cy + 0.5) * mpp)

    def _blocked(p, buf=0.5):
        return blockers is not None and blockers.buffer(buf).contains(p)

    cand: list[tuple[float, float, float, str]] = []
    inner = geom.buffer(-EDGE_INSET_M)
    if chm is not None:
        tree = (nd > TREE_M) & (exg > 0.03)
        tree_near = ndi.binary_dilation(tree, iterations=max(1, round(TREE_NEAR_M / mpp)))
        lab, _n = ndi.label((nd > OBJ_MIN_M) & (nd < TALL_MAX_M) & ~tree)
        for i, sl in enumerate(ndi.find_objects(lab), 1):
            if sl is None:
                continue
            cm = lab[sl] == i
            area = cm.sum() * px
            if not (AREA_A_M2[0] <= area <= AREA_A_M2[1]):
                continue
            hv = nd[sl][cm]
            if (cm & tree_near[sl]).sum() > TREE_SHARE_MAX * cm.sum():
                continue  # okraj koruny
            if exg[sl][cm].mean() > EXG_MAX or bri[sl][cm].mean() < DARK_BRI:
                continue  # zeleň / hluboký stín
            if area >= ROOF_AREA_M2 and np.percentile(hv, 25) > ROOF_P25_M:
                continue  # plná střecha – budka, altán
            if hv.max() < TALL_MIN_M:
                continue
            full = np.zeros(shape, bool)
            full[sl] = cm
            p = _pt(full)
            if not inner.contains(p) or _blocked(p):
                continue
            cand.append((p.x, p.y, 2.0 + float(hv.max()) / 4.0, "A"))
    sand = (bri > SAND_BRI) & (exg < EXG_MAX) & (warm > SAND_WARM) & (sat < 0.3) & inside & (nd < 0.5)
    sand = ndi.binary_opening(sand, iterations=1)
    lab2, _n2 = ndi.label(sand)
    ring_it = max(1, round(1.5 / mpp))
    for i, sl in enumerate(ndi.find_objects(lab2), 1):
        if sl is None:
            continue
        cm = lab2 == i
        area = cm.sum() * px
        if not (AREA_B_M2[0] <= area <= AREA_B_M2[1]):
            continue
        fill = ndi.binary_fill_holes(cm)
        ys_, xs_ = np.nonzero(fill)
        rect = MultiPoint(list(zip(xs_.tolist(), ys_.tolist()))).minimum_rotated_rectangle
        if area <= SANDPIT_MAX_M2 and rect.area > 0 and fill.sum() / rect.area > SANDPIT_RECT_FILL:
            continue  # pravoúhlé pískoviště
        med = float(np.median(bri[fill]))
        if (fill & (bri < 0.72 * med)).sum() < 1 and (fill & (nd > 0.5)).sum() < 1:
            continue  # na dopadové ploše musí něco stát
        ring = ndi.binary_dilation(cm, iterations=ring_it) & ~cm
        if ring.sum() == 0 or (exg[ring] > 0.03).mean() < GRASS_RING_SHARE:
            continue
        p = _pt(cm)
        if not inner.contains(p) or _blocked(p):
            continue
        radius = math.sqrt(area / math.pi) + 1.0
        hit = [k for k, c in enumerate(cand) if math.dist(c[:2], (p.x, p.y)) <= radius and c[3] == "A"]
        if hit:
            for k in hit:
                cand[k] = (cand[k][0], cand[k][1], cand[k][2] + 1.0, "AB")
        else:
            cand.append((p.x, p.y, 1.0, "B"))
    return cand


def fallback_point(geom, ortho, chm) -> tuple[float, float]:
    """Střed největší otevřené nezelené části hřiště (nejdál od okraje), jinak reprezentativní bod obrysu."""
    import numpy as np
    from scipy import ndimage as ndi

    got = _grid(ortho, geom) if ortho is not None else None
    if got is not None:
        rgb, x0, y_top = got
        mpp = float(ortho.mpp_x)
        shape = rgb.shape[:2]
        exg, bri, _w, _s = _exg_bri(rgb)
        nd = _sample_chm(chm, x0, y_top, mpp, shape) if chm is not None else np.zeros(shape, np.float32)
        op = _poly_mask(geom, x0, y_top, mpp, shape) & (nd < 0.5) & (exg < 0.0) & (bri > 90)
        op = ndi.binary_opening(op, iterations=2)
        lab, n = ndi.label(op)
        if n:
            big = int(np.argmax(ndi.sum(op, lab, range(1, n + 1)))) + 1
            dt = ndi.distance_transform_edt(lab == big)
            cy, cx = np.unravel_index(int(dt.argmax()), dt.shape)
            return x0 + (cx + 0.5) * mpp, y_top - (cy + 0.5) * mpp
    rp = geom.representative_point()
    return rp.x, rp.y


def _thin(pts, gap_m: float):
    kept = []
    for p in pts:
        if all(math.dist(p[:2], q[:2]) >= gap_m for q in kept):
            kept.append(p)
    return kept


def generalize(area_m2: float, osm_pts, cand, scale: int):
    """Výběr křížků jednoho hřiště: OSM (ne ploché) > kandidáti podle skóre; malé hřiště 1, jinak odstup a strop."""
    ranked = [(x, y, 10.0 if t in TALL_TYPES else 5.0, "osm") for x, y, t in osm_pts if t not in FLAT_TYPES]
    ranked += [c for c in cand if all(math.dist(c[:2], q[:2]) > OSM_DEDUP_M for q in ranked)]
    ranked.sort(key=lambda c: -c[2])
    if area_m2 <= SMALL_M2:
        return ranked[:1]
    gap = GAP_MM * max(int(scale), 1) / 1000.0
    return _thin(ranked, gap)[: max(1, math.ceil(area_m2 / AREA_PER_X_M2))]


def playground_points(feats: list[dict], ortho, chm, scale: int, log=None) -> list[dict]:
    """``feats`` (features.geojson) s herními prvky v hřištích nahrazenými generalizovanou sadou (OSM + detekce)."""
    from shapely.geometry import Point, shape
    from shapely.ops import unary_union
    from shapely.strtree import STRtree

    def _props(f):
        return f.get("properties") or {}

    grounds, blockers = [], []
    for f in feats:
        p, g = _props(f), f.get("geometry") or {}
        if g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        kind = p.get("kind")
        if kind not in ("playground", "building", "parking"):
            continue
        try:
            geom = shape(g)
            geom = geom if geom.is_valid else geom.buffer(0)
        except (ValueError, TypeError):
            continue
        if geom.is_empty:
            continue
        if kind == "playground":
            if not is_traffic_playground(p.get("name")):
                grounds.append(geom)
        else:
            blockers.append(geom)
    if not grounds:
        return feats
    eq_idx = [
        i for i, f in enumerate(feats)
        if _props(f).get("kind") == KIND and (f.get("geometry") or {}).get("type") == "Point"
    ]
    blk = unary_union(blockers) if blockers else None
    tree = STRtree(grounds)
    per_ground: dict[int, list] = {k: [] for k in range(len(grounds))}
    drop: set[int] = set()
    for i in eq_idx:
        x, y = feats[i]["geometry"]["coordinates"][:2]
        pt = Point(x, y)
        for k in tree.query(pt.buffer(2.0)):
            if grounds[int(k)].buffer(2.0).contains(pt):
                per_ground[int(k)].append((x, y, str(_props(feats[i]).get("playground") or "").lower()))
                drop.add(i)
                break
    out = [f for i, f in enumerate(feats) if i not in drop]
    n_det = n_fb = n_osm = 0
    for k, geom in enumerate(grounds):
        cand = detect_candidates(geom, ortho, chm, blk) if DETECT else []
        chosen = generalize(geom.area, per_ground[k], cand, scale)
        if not chosen:
            flat = [(x, y, 0.0, "osm") for x, y, t in per_ground[k] if t not in ("sandpit", "no")]
            if flat:
                chosen = flat[:1]
            elif FALLBACK:
                fx, fy = fallback_point(geom, ortho, chm)
                chosen = [(fx, fy, 0.0, "fallback")]
        for x, y, _s, src in chosen:
            src = "osm" if src == "osm" else ("fallback" if src == "fallback" else "detect")
            n_osm += src == "osm"
            n_det += src == "detect"
            n_fb += src == "fallback"
            out.append(
                {
                    "type": "Feature",
                    "properties": {"source": src, "kind": KIND, "oom_code": "531"},
                    "geometry": {"type": "Point", "coordinates": [float(x), float(y)]},
                }
            )
    if log:
        log(
            f"Herní prvky: {len(grounds)} hřišť, křížky OSM {n_osm}, z výšky a ortofota {n_det}, záložní {n_fb}"
            f" (OSM bodů v hřištích {len(drop)})"
        )
    return out
