"""Herní prvky na dětských hřištích ve sprintu (531 ×): OSM body + detekce z výšky a ortofota, generalizace.

Petr 2026-11-06 (průzkum ``/mnt/project-files/herni-prvky-ortofoto/``): dětské hřiště má vždy nějaké herní prvky, OSM je
má jen u ~15 % hřišť. V obrysu ``leisure=playground`` se proto hledají:

- **konstrukce z výšky**: rastr DMP OK 0,5 m jen v obrysech hřišť (maximum bodů minus DEM jobu, ``dmpok_height``;
  bez LAZ záloha ``chm.tif``): shluk ``OBJ_MIN_M``–``OBJ_MAX_M`` nad terénem o ploše ``AREA_A_M2``, ne zelený (strom =
  zelený pixel ortofota, žádné okolí koruny), ne v hlubokém stínu, ne budka s plnou střechou;
- **vržený stín** (ortofoto 0,125 m jen v obrysech hřišť): tmavý nezelený protáhlý útvar nebo rám na světlé volné ploše
  (písek, tráva, štěrk) mimo korunu; směr stínu se odhadne z korun a budov v témž výřezu (``sun_shadow_dir``), křížek je
  na konci stínu blíž ke slunci (u paty prvku). Pravoúhlý rám (pískoviště) ne;
- **dopadové plochy**: světlá teplá (písek / štěrk) skvrna v trávě, na které něco stojí, ne pravoúhlé pískoviště.

OSM body mají přednost (vysoké konstrukce před ostatními), detekce do ``OSM_DEDUP_M`` od OSM bodu se zahodí, ploché
prvky (trampolína v zemi, pískoviště…) jen tehdy, když na hřišti nic jiného není. Generalizace: hřiště do
``SMALL_M2`` jeden křížek na nejjistějším nálezu, větší doplní další nálezy s odstupem ``GAP_MM`` a nejvýš jeden na
``AREA_PER_X_M2``. Hřiště bez nálezu dostane jeden záložní křížek ve středu největší otevřené nezelené části.
Dopravní hřiště (``name`` s „dopravní“) se přeskakuje.
"""

from __future__ import annotations

import math

# Záloha: False = herní prvky jen z OSM jako dřív (bez detekce, bez generalizace).
DETECT = True
# Záložní křížek na hřišti bez OSM bodu i nálezu.
FALLBACK = True
# Stín prvků na ortofotu 0,125 m (False = jen výška a dopadové plochy).
SHADOWS = True

# Výška nad terénem z bodů DMP OK (Barrandov 2026-11-06: prvky 1–3,5 m, shluk 0,5–40 m²).
HEIGHT_MPP = 0.5
HEIGHT_PAD_M = 3.0
OBJ_MIN_M = 1.0
OBJ_MAX_M = 3.5
AREA_A_M2 = (0.5, 40.0)
TREE_SHARE_MAX = 0.3  # podíl zelených pixelů ve shluku (strom / keř)
TALL_ADJ_M2 = 15.0  # vyšší plocha (koruna, střecha), na kterou shluk navazuje (lanová pyramida je menší)
COLOR_SAT = 0.3  # sytě barevný nezelený shluk (plast herních prvků)
# Křížek aspoň tak daleko uvnitř obrysu (plot, živý plot a sousední objekty na hraně hřiště ne).
EDGE_INSET_M = 1.0
EXG_MAX = 0.02
EXG_GREEN = 0.03
DARK_BRI = 55.0
ROOF_AREA_M2 = 8.0
ROOF_P25_M = 1.3
# Ortofoto jen v obrysech hřišť (výřez + okraj na odhad slunce z okolních korun).
FINE_MPP = 0.125
FINE_PAD_M = 8.0
FINE_MAX_M2 = 20000.0
# Stín: o ``SHADOW_DROP`` tmavší než šedé uzavření jasu ``SHADOW_CLOSE_M`` (tenké tmavé útvary), okolí světlé,
# mimo korunu (výška > ``CROWN_M``).
SHADOW_CLOSE_M = 1.0
SHADOW_DROP = 0.18
LIGHT_BG = 105.0
SHADOW_BG_M = 3.0
SUN_DARK_RATIO = 0.62
SUN_DARK_BRI = 70.0
SHADOW_EXG_MAX = 0.06
CROWN_M = 3.5
SHADOW_AREA_M2 = (0.15, 6.0)
SHADOW_ELONG = 2.5
SHADOW_LEN_M = (1.0, 7.0)
SHADOW_FRAME_FILL = 0.45
SHADOW_FRAME_HULL_M2 = 1.0
SHADOW_COMPACT_M2 = (0.8, 4.0)
SHADOW_COMPACT_SIDE_M = 0.8
# Stín stromu: konec stínu u slunce do 1 m od koruny; keř / živý plot: stín z ``SHADOW_CASTER_SHARE`` u něj.
SHADOW_CASTER_GAP_M = 1.0
SHADOW_CASTER_SHARE = 0.4
BIG_SHADOW_GAP_M = 0.75
CROWN_BRI = 85.0
# Rám pískoviště: obě strany obdélníku 1,2–6 m a skoro všechny pixely u obvodu.
SANDPIT_SIDE_M = (1.2, 6.0)
SANDPIT_EDGE_SHARE = 0.75
# Odhad slunce: posun výšky (koruny, budovy nad ``SUN_TALL_M``) do tmavé masky, kroky směru a délky.
SUN_TALL_M = 4.0
SUN_STEP_DEG = 10
SUN_MAX_M = 12.0
SUN_MIN_GAIN = 0.12
# Výchozí směr stínu (k severu, ortofota ČÚZK jsou z letní poledne) v S-JTSK (x k východu, y k severu).
DEFAULT_SHADOW_DIR = (0.0, 1.0)
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

# Soukromé hřiště (Petr 2026-11-06, Krumlov: zahrady s bazénem): bez detekce i zálohy, OSM body beze změny.
PRIVATE_ACCESS = frozenset({"private", "no", "customers"})
PRIVATE_GARDEN_SHARE = 0.8
PRIVATE_POOL_M = 10.0

KIND = "playground_equipment"
# Ploché prvky / povrch: kreslí se jen jako poslední záloha.
FLAT_TYPES = frozenset({"sandpit", "no", "trampoline", "cushion", "splash_pad", "map", "marble_run"})
TALL_TYPES = frozenset(
    {"climbingframe", "structure", "slide", "tower", "swing", "basketswing", "climbingwall", "playhouse",
     "zipwire", "rope_traverse", "climbing_slope", "seesaw", "roundabout"}
)


def is_traffic_playground(name: str | None) -> bool:
    return "dopravn" in str(name or "").lower()


def is_private_garden_tags(tags: dict) -> bool:
    """Soukromá zahrada OSM: ``leisure=garden`` s ``access=private`` nebo ``garden:type=residential``."""
    if str(tags.get("leisure") or "").lower() != "garden":
        return False
    return (
        str(tags.get("access") or "").lower() in PRIVATE_ACCESS
        or str(tags.get("garden:type") or "").lower() == "residential"
    )


def is_private_playground(props: dict) -> bool:
    """Hřiště s ``access`` private / no / customers nebo v soukromé zahradě (props z přípravy OSM)."""
    return str(props.get("access") or "").lower() in PRIVATE_ACCESS or bool(props.get("private_garden"))


def mark_private_playgrounds(features: list[dict], gardens, pools) -> int:
    """Hřišti z ≥ ``PRIVATE_GARDEN_SHARE`` v soukromé zahradě nebo s bazénem do ``PRIVATE_POOL_M`` nastaví
    ``private_garden``; ``gardens`` / ``pools`` = seznamy prstenců (``osm_area_polygons_5514``). Vrací počet."""
    from shapely.geometry import Polygon, shape
    from shapely.ops import unary_union

    def _union(rings_list):
        polys = []
        for rings in rings_list or []:
            try:
                p = Polygon(rings[0], rings[1:]).buffer(0)
            except (ValueError, TypeError, IndexError):
                continue
            if not p.is_empty:
                polys.append(p)
        return unary_union(polys) if polys else None

    garden_u, pool_u = _union(gardens), _union(pools)
    if garden_u is None and pool_u is None:
        return 0
    n = 0
    for f in features:
        props = f.get("properties") or {}
        if props.get("kind") != "playground" or (f.get("geometry") or {}).get("type") not in ("Polygon", "MultiPolygon"):
            continue
        try:
            g = shape(f["geometry"]).buffer(0)
        except (ValueError, TypeError):
            continue
        if g.is_empty or g.area <= 0:
            continue
        in_garden = garden_u is not None and g.intersection(garden_u).area >= PRIVATE_GARDEN_SHARE * g.area
        near_pool = pool_u is not None and g.distance(pool_u) <= PRIVATE_POOL_M
        if in_garden or near_pool:
            props["private_garden"] = True
            f["properties"] = props
            n += 1
    return n


def _exg_bri(rgb):
    import numpy as np

    a = rgb.astype(np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    s = r + g + b + 1e-6
    return (2 * g - r - b) / s, s / 3.0, r - b, (a.max(2) - a.min(2)) / np.maximum(a.max(2), 1.0)


def _grid(ortho, geom, pad_m: float = 3.0):
    """Výřez ortofota kolem ``geom``: (rgb, x0, y_top) nebo None."""
    h, w = ortho.data.shape[:2]
    bx0, by0, bx1, by1 = geom.buffer(pad_m).bounds
    c0, c1 = max(0, int((bx0 - ortho.x0) / ortho.mpp_x)), min(w, int(math.ceil((bx1 - ortho.x0) / ortho.mpp_x)))
    r0, r1 = max(0, int((ortho.y_top - by1) / ortho.mpp_y)), min(h, int(math.ceil((ortho.y_top - by0) / ortho.mpp_y)))
    if c1 - c0 < 3 or r1 - r0 < 3:
        return None
    return ortho.data[r0:r1, c0:c1, :3], ortho.x0 + c0 * ortho.mpp_x, ortho.y_top - r0 * ortho.mpp_y


def _sample_chm(chm, x0: float, y_top: float, mpp: float, shape):
    """Výška (nejbližší buňka) na mřížce výřezu ortofota; mimo rastr / NoData = 0."""
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


def dmpok_height(work_dir, grounds, log=None):
    """Rastr výšky nad terénem 0,5 m (``Raster``) jen v obrysech hřišť + ``HEIGHT_PAD_M``, nebo None.

    Maximum bodů DMP OK jobu (``lidar_veg_parts``) v buňkách uvnitř obrysů minus DEM jobu (``dem/dem_filled.tif``,
    nejbližší buňka); buňky bez bodu = 0. Body čte laspy po blocích s maskou obrysů v mřížce (Barrandov 5,5 km²:
    PDAL ``filters.crop`` polygonem 100–170 s, samotné čtení 40 s, laspy 17 s); bez laspy PDAL ``filters.crop``
    + ``writers.gdal``."""
    from pathlib import Path

    import numpy as np
    from shapely.ops import unary_union

    from app.download_cache import lidar_veg_parts
    from app.pipeline.gdal_cli_raster import read_float32_geotiff
    from app.pipeline.residual_verify import Raster

    work_dir = Path(work_dir)
    parts = lidar_veg_parts(work_dir / "lidar")
    dem_path = work_dir / "dem" / "dem_filled.tif"
    if not parts or not dem_path.is_file() or not grounds:
        return None
    area = unary_union([g.buffer(HEIGHT_PAD_M) for g in grounds]).simplify(0.5)
    bx0, by0, bx1, by1 = area.bounds
    s = HEIGHT_MPP
    ox, oy = math.floor(bx0 / s) * s, math.floor(by0 / s) * s
    width, height = int(math.ceil((bx1 - ox) / s)), int(math.ceil((by1 - oy) / s))
    y_top = oy + height * s
    try:
        import laspy  # noqa: F401

        zmax = _max_z_laspy(parts, area, ox, y_top, s, width, height)
        nodata = None
    except ImportError:
        zmax, nodata = _max_z_pdal(parts, area, work_dir, ox, oy, s, width, height)
    gt = (ox, s, 0.0, y_top, 0.0, -s)
    dem, dgt, dnd = read_float32_geotiff(dem_path)
    xs = gt[0] + (np.arange(zmax.shape[1]) + 0.5) * gt[1]
    ys = gt[3] + (np.arange(zmax.shape[0]) + 0.5) * gt[5]
    ci = np.clip(np.floor((xs - dgt[0]) / dgt[1]).astype(int), 0, dem.shape[1] - 1)
    ri = np.clip(np.floor((ys - dgt[3]) / dgt[5]).astype(int), 0, dem.shape[0] - 1)
    ground = dem[np.ix_(ri, ci)]
    h = zmax - ground
    bad = ~np.isfinite(h) | ~np.isfinite(zmax)
    if nodata is not None:
        bad |= zmax == nodata
    if dnd is not None:
        bad |= ground == dnd
    h[bad] = 0.0
    h[h < 0] = 0.0
    return Raster(h.astype(np.float32), gt[0], gt[3], gt[1], -gt[5])


def _max_z_laspy(parts, area, ox: float, y_top: float, s: float, width: int, height: int, chunk: int = 5_000_000):
    """Maximum Z bodů v buňkách mřížky uvnitř ``area`` (NaN = bez bodu); laspy + lazrs po blocích."""
    import laspy
    import numpy as np

    inside = _poly_mask(area, ox, y_top, s, (height, width))
    zmax = np.full((height, width), -np.inf, np.float64)
    for path in parts:
        with laspy.open(str(path)) as reader:
            hd = reader.header
            if hd.maxs[0] < ox or hd.mins[0] > ox + width * s or hd.maxs[1] < y_top - height * s or hd.mins[1] > y_top:
                continue
            for pts in reader.chunk_iterator(chunk):
                x, y = np.asarray(pts.x), np.asarray(pts.y)
                ci = np.floor((x - ox) / s).astype(np.int64)
                ri = np.floor((y_top - y) / s).astype(np.int64)
                ok = (ci >= 0) & (ci < width) & (ri >= 0) & (ri < height)
                ok[ok] = inside[ri[ok], ci[ok]]
                if ok.any():
                    np.maximum.at(zmax, (ri[ok], ci[ok]), np.asarray(pts.z)[ok])
    zmax[~np.isfinite(zmax)] = np.nan
    return zmax.astype(np.float32)


def _max_z_pdal(parts, area, work_dir, ox: float, oy: float, s: float, width: int, height: int):
    """Totéž přes PDAL (``filters.crop`` polygonem + ``writers.gdal`` max): (pole, nodata)."""
    import json
    import tempfile
    from pathlib import Path

    from app.pipeline.gdal_cli_raster import read_float32_geotiff
    from app.pipeline.prepare_lidar import find_tool, run_cmd

    dest = Path(work_dir) / "temp" / "playground_dmpok_max.tif"
    dest.parent.mkdir(parents=True, exist_ok=True)
    pipeline = {
        "pipeline": [
            *[str(p) for p in parts],
            {"type": "filters.crop", "polygon": area.wkt},
            {
                "type": "writers.gdal", "filename": str(dest), "resolution": s, "output_type": "max",
                "data_type": "float32", "gdaldriver": "GTiff", "nodata": -9999,
                "origin_x": ox, "origin_y": oy, "width": width, "height": height,
            },
        ]
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(pipeline, tmp)
        pipe_path = tmp.name
    try:
        run_cmd([find_tool("pdal"), "pipeline", pipe_path], log=None)
    finally:
        Path(pipe_path).unlink(missing_ok=True)
    zmax, _gt, nodata = read_float32_geotiff(dest)
    return zmax, nodata


def _shift(mask, dr: int, dc: int):
    import numpy as np

    out = np.zeros_like(mask)
    h, w = mask.shape
    if abs(dr) >= h or abs(dc) >= w:
        return out
    out[max(0, dr) : h + min(0, dr), max(0, dc) : w + min(0, dc)] = mask[
        max(0, -dr) : h - max(0, dr), max(0, -dc) : w - max(0, dc)
    ]
    return out


def _dark_mask(rgb, mpp: float):
    """Tenký stín na světlé ploše: o ``SHADOW_DROP`` tmavší než šedé uzavření jasu (``SHADOW_CLOSE_M`` zalije
    tmavé útvary užší než ~1 m, velké stíny korun a hrany ploch zůstanou), okolí světlejší než ``LIGHT_BG``.
    Vrací (stín, jas, ExG, okolí)."""
    from scipy import ndimage as ndi

    exg, bri, _w, _s = _exg_bri(rgb)
    n = max(3, int(round(SHADOW_CLOSE_M / mpp)) | 1)
    bg = ndi.grey_closing(bri, size=(n, n))
    return (bri < (1.0 - SHADOW_DROP) * bg) & (bg > LIGHT_BG), bri, exg, bg


def _big_dark(rgb, mpp: float):
    """Velké stíny (koruny, budovy) pro odhad slunce: tmavší než ``SUN_DARK_RATIO`` × průměr okolí 3 m."""
    from scipy import ndimage as ndi

    _exg, bri, _w, _s = _exg_bri(rgb)
    bg = ndi.uniform_filter(bri, size=max(3, int(round(SHADOW_BG_M / mpp))))
    return (bri < SUN_DARK_RATIO * bg) | (bri < SUN_DARK_BRI)


def sun_shadow_dir(rgb, nd, mpp: float):
    """Směr stínu (jednotkový vektor v S-JTSK) z korun a budov ve výřezu, nebo None (málo vysokých objektů / nejisté).

    Vysoká maska (výška > ``SUN_TALL_M``) posunutá o vektor délky do ``SUN_MAX_M`` → podíl posunutých pixelů mimo
    vysokou masku, které padnou do stínu; nejlepší směr musí být o ``SUN_MIN_GAIN`` nad mediánem směrů."""
    import numpy as np

    tall = nd > SUN_TALL_M
    if tall.sum() < 20 / (mpp * mpp):  # aspoň ~20 m² korun / střech
        return None
    dark = _big_dark(rgb, mpp)
    step = max(1, int(round(0.5 / mpp)))
    tall_s, dark_s = tall[::step, ::step], dark[::step, ::step]
    m = mpp * step
    scores = []
    for deg in range(0, 360, SUN_STEP_DEG):
        a = math.radians(deg)
        ux, uy = math.sin(a), math.cos(a)  # azimut od severu, ve směru hodinových ručiček
        best = 0.0
        for d in np.arange(1.0, SUN_MAX_M + 0.01, 1.0):
            dc, dr = int(round(ux * d / m)), int(round(-uy * d / m))
            sh = _shift(tall_s, dr, dc) & ~tall_s
            n = int(sh.sum())
            if n < 10:
                continue
            best = max(best, float((sh & dark_s).sum()) / n)
        scores.append(best)
    sc = np.asarray(scores)
    k = int(sc.argmax())
    if sc[k] - float(np.median(sc)) < SUN_MIN_GAIN:
        return None
    a = math.radians(k * SUN_STEP_DEG)
    return (math.sin(a), math.cos(a))


def _is_frame(cm, mpp: float):
    """Pravoúhlý rám (pískoviště): obdélník 1,2–6 m na obou stranách a skoro všechny pixely u jeho obvodu."""
    import numpy as np
    from shapely.geometry import MultiPoint, Point

    ys_, xs_ = np.nonzero(cm)
    if len(xs_) < 6:
        return False
    rect = MultiPoint(list(zip(xs_.tolist(), ys_.tolist()))).minimum_rotated_rectangle
    if rect.geom_type != "Polygon":
        return False
    c = list(rect.exterior.coords)
    a, b = math.dist(c[0], c[1]) * mpp, math.dist(c[1], c[2]) * mpp
    lo, hi = SANDPIT_SIDE_M
    if not (lo <= min(a, b) and max(a, b) <= hi):
        return False
    ring = rect.exterior
    tol = max(1.5, 0.4 / mpp)
    near = sum(1 for x, y in zip(xs_.tolist(), ys_.tolist()) if ring.distance(Point(x, y)) <= tol)
    return near >= SANDPIT_EDGE_SHARE * len(xs_)


def shadow_candidates(geom, rgb, x0: float, y_top: float, mpp: float, nd, shadow_dir, inner, blocked):
    """Kandidáti ``S`` ze stínu: (x, y, skóre, "S") u paty prvku (konec stínu blíž ke slunci)."""
    import numpy as np
    from scipy import ndimage as ndi
    from shapely.geometry import MultiPoint, Point

    dark, _bri, exg, _bg = _dark_mask(rgb, mpp)
    crown = nd > CROWN_M
    inside = _poly_mask(geom, x0, y_top, mpp, rgb.shape[:2])
    cand_mask = dark & (exg < SHADOW_EXG_MAX) & ~crown & inside
    # vrhače, které herní prvek nejsou: koruny (konec stínu u slunce do 1 m), zelené keře a živé ploty nad
    # ``OBJ_MIN_M`` (stín z větší části do 0,5 m od nich)
    big = ndi.binary_dilation(crown, iterations=max(1, int(round(SHADOW_CASTER_GAP_M / mpp))))
    near_caster = ndi.binary_dilation(
        crown | ((nd > OBJ_MIN_M) & (exg > EXG_GREEN)), iterations=max(1, int(round(0.5 / mpp)))
    )
    # velké stíny korun a budov: výběžky jejich okraje nejsou stín prvku
    # (koruna v ortofotu je proti výšce posunutá náklonem, proto i tmavá zeleň a pás ``BIG_SHADOW_GAP_M``)
    big_dark = _big_dark(rgb, mpp) | ((exg > EXG_GREEN) & (_bri < CROWN_BRI))
    big_shadow = ndi.binary_opening(big_dark, iterations=max(1, int(round(0.4 / mpp))))
    big_shadow = ndi.binary_dilation(big_shadow, iterations=max(1, int(round(BIG_SHADOW_GAP_M / mpp))))
    lab, _n = ndi.label(cand_mask)
    px = mpp * mpp
    sx, sy = shadow_dir
    out = []
    lines = []  # (osa, úhel, délka) protáhlých stínů – dvojice rovnoběžek = rám pískoviště
    for i, sl in enumerate(ndi.find_objects(lab), 1):
        if sl is None:
            continue
        cm = lab[sl] == i
        area = cm.sum() * px
        if not (SHADOW_AREA_M2[0] <= area <= SHADOW_AREA_M2[1]):
            continue
        ys_, xs_ = np.nonzero(cm)
        pts = list(zip(xs_.tolist(), ys_.tolist()))
        rect = MultiPoint(pts).minimum_rotated_rectangle
        if rect.geom_type != "Polygon":
            continue
        c = list(rect.exterior.coords)
        a, b = math.dist(c[0], c[1]) * mpp, math.dist(c[1], c[2]) * mpp
        long_, short = max(a, b), max(min(a, b), mpp)
        hull_m2 = max(MultiPoint(pts).convex_hull.buffer(0.5, join_style=2).area, 1.0) * px
        long_ += mpp
        short += mpp
        elong = long_ / short >= SHADOW_ELONG and SHADOW_LEN_M[0] <= long_ <= SHADOW_LEN_M[1]
        frame = hull_m2 >= SHADOW_FRAME_HULL_M2 and area / hull_m2 <= SHADOW_FRAME_FILL and long_ <= SHADOW_LEN_M[1]
        # kompaktní stín konstrukce (prolézačka „A“, pružinové houpadlo z boku)
        compact = SHADOW_COMPACT_M2[0] <= area <= SHADOW_COMPACT_M2[1] and short >= SHADOW_COMPACT_SIDE_M
        gx = (xs_ + sl[1].start + 0.5) * mpp
        gy = -(ys_ + sl[0].start + 0.5) * mpp
        line_id = None
        if long_ / short >= SHADOW_ELONG and long_ >= SANDPIT_SIDE_M[0] * 0.8:
            k0 = 0 if a >= b else 1
            ang = math.atan2(-(c[k0 + 1][1] - c[k0][1]), c[k0 + 1][0] - c[k0][0]) % math.pi
            lines.append((float(gx.mean()), float(gy.mean()), ang, long_))
            line_id = len(lines) - 1
        if not (elong or frame or compact):
            continue
        if _is_frame(cm, mpp):
            continue  # pískoviště
        if (cm & near_caster[sl]).sum() > SHADOW_CASTER_SHARE * cm.sum():
            continue  # stín / okraj živého plotu, keře
        if (cm & big_shadow[sl]).sum() > SHADOW_CASTER_SHARE * cm.sum():
            continue  # výběžek velkého stínu
        if (cm & big[sl]).sum() > SHADOW_CASTER_SHARE * cm.sum():
            continue  # pod korunou (koruna v ortofotu posunutá náklonem až o metr)
        # konec stínu u slunce: pixely s nejmenším průmětem do směru stínu (řádky obrazu rostou k jihu)
        proj = gx * sx + gy * sy
        end = proj <= proj.min() + 0.4
        er, ec = int(round(np.mean(ys_[end]))) + sl[0].start, int(round(np.mean(xs_[end]))) + sl[1].start
        if big[er, ec]:
            continue  # stín koruny / vysokého objektu
        x = x0 + (np.mean(xs_[end]) + sl[1].start + 0.5) * mpp
        y = y_top - (np.mean(ys_[end]) + sl[0].start + 0.5) * mpp
        p = Point(x, y)
        if not inner.contains(p) or blocked(p):
            continue
        out.append((x, y, 1.5 + min(long_, 4.0) / 8.0, "S", line_id))
    paired = {i for i in range(len(lines)) for j in range(len(lines)) if i != j and _sandpit_pair(lines[i], lines[j])}
    return [o[:4] for o in out if o[4] is None or o[4] not in paired]


def _sandpit_pair(l1, l2) -> bool:
    """Dvě rovnoběžné čáry stínu 1,2–6 m od sebe, které se v průmětu z většiny překrývají: protější strany rámu."""
    x1, y1, a1, n1 = l1
    x2, y2, a2, n2 = l2
    da = abs(a1 - a2) % math.pi
    if min(da, math.pi - da) > math.radians(15):
        return False
    ux, uy = math.cos(a1), math.sin(a1)
    dx, dy = x2 - x1, y2 - y1
    along, across = dx * ux + dy * uy, abs(-dx * uy + dy * ux)
    if not (SANDPIT_SIDE_M[0] <= across <= SANDPIT_SIDE_M[1]):
        return False
    overlap = min(n1 / 2, along + n2 / 2) - max(-n1 / 2, along - n2 / 2)
    return overlap >= 0.5 * min(n1, n2)


def detect_candidates(geom, ortho, chm, blockers=None, shadow_dir=None) -> list[tuple[float, float, float, str]]:
    """Kandidáti (x, y, skóre, zdroj) v hřišti: ``A`` výška, ``S`` stín, ``AS`` výška se stínem, ``AB`` výška na
    dopadové ploše, ``B`` dopadová plocha.

    ``ortho``: ortofoto hřiště (0,125 m, jinak ortofoto jobu); ``chm``: výška nad terénem (DMP OK 0,5 m nebo
    ``chm.tif``); ``blockers``: připravená geometrie budov a parkovišť kolem hřiště, už rozšířená o 0,5 m;
    ``shadow_dir``: směr stínu (jen ortofoto jemnější než 0,25 m hledá stíny)."""
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

    def _blocked(p):
        return blockers is not None and blockers.contains(p)

    cand: list[tuple[float, float, float, str]] = []
    inner = geom.buffer(-EDGE_INSET_M)
    if chm is not None:
        green = exg > EXG_GREEN
        obj = (nd >= OBJ_MIN_M) & (nd <= OBJ_MAX_M)
        obj = ndi.binary_closing(obj, iterations=max(1, int(round(0.5 / mpp)))) & (nd > 0.5)
        # okraj koruny / střechy: shluk přímo navazuje na velkou vyšší plochu (ne okolí koruny, jen dotyk)
        tall_lab, _nt = ndi.label(nd > OBJ_MAX_M)
        tall_big = np.zeros(shape, bool)
        if _nt:
            sizes = ndi.sum(np.ones(shape), tall_lab, range(1, _nt + 1)) * px
            keep = np.flatnonzero(sizes >= TALL_ADJ_M2) + 1
            tall_big = np.isin(tall_lab, keep)
        touch = ndi.binary_dilation(tall_big, iterations=max(1, int(round(0.5 / mpp))))
        lab, _n = ndi.label(obj)
        for i, sl in enumerate(ndi.find_objects(lab), 1):
            if sl is None:
                continue
            cm = lab[sl] == i
            area = cm.sum() * px
            if not (AREA_A_M2[0] <= area <= AREA_A_M2[1]):
                continue
            hv = nd[sl][cm]
            if (cm & green[sl]).sum() > TREE_SHARE_MAX * cm.sum():
                continue  # strom / keř (zelený pixel)
            colorful = float(np.median(sat[sl][cm])) > COLOR_SAT and float(np.median(exg[sl][cm])) < 0.0
            if (cm & touch[sl]).any() and not colorful:
                continue  # okraj koruny nebo střechy (barevná věž / skluzavka pod stromem zůstane)
            if exg[sl][cm].mean() > EXG_MAX or bri[sl][cm].mean() < DARK_BRI:
                continue  # zeleň / hluboký stín
            if area >= ROOF_AREA_M2 and np.percentile(hv, 25) > ROOF_P25_M:
                continue  # plná střecha – budka, altán
            full = np.zeros(shape, bool)
            full[sl] = cm
            p = _pt(full)
            if not inner.contains(p) or _blocked(p):
                continue
            cand.append((p.x, p.y, 2.0 + float(hv.max()) / 4.0, "A"))
    if SHADOWS and mpp <= 0.25:
        sdir = shadow_dir or DEFAULT_SHADOW_DIR
        for sx_, sy_, s_score, _src in shadow_candidates(geom, rgb, x0, y_top, mpp, nd, sdir, inner, _blocked):
            hit = [k for k, c in enumerate(cand) if c[3] == "A" and math.dist(c[:2], (sx_, sy_)) <= 2.5]
            if hit:
                for k in hit:
                    cand[k] = (cand[k][0], cand[k][1], cand[k][2] + 1.0, "AS")
            elif all(math.dist(c[:2], (sx_, sy_)) > 1.5 for c in cand):
                cand.append((sx_, sy_, s_score, "S"))
    sand = (bri > SAND_BRI) & (exg < EXG_MAX) & (warm > SAND_WARM) & (sat < 0.3) & inside & (nd < 0.5)
    sand = ndi.binary_opening(sand, iterations=max(1, int(round(0.5 / mpp))))
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
        step = max(1, int(round(0.5 / mpp)))
        rect = MultiPoint(list(zip(xs_[::step].tolist(), ys_[::step].tolist()))).minimum_rotated_rectangle
        if area <= SANDPIT_MAX_M2 and rect.area > 0 and fill.sum() / step / rect.area > SANDPIT_RECT_FILL:
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
        hit = [k for k, c in enumerate(cand) if math.dist(c[:2], (p.x, p.y)) <= radius and c[3] in ("A", "AS")]
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
        op = ndi.binary_opening(op, iterations=max(2, int(round(1.0 / mpp))))
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


def playground_points(
    feats: list[dict], ortho, chm, scale: int, log=None, height=None, fine_ortho=None
) -> list[dict]:
    """``feats`` (features.geojson) s herními prvky v hřištích nahrazenými generalizovanou sadou (OSM + detekce).

    ``height(grounds)`` → rastr výšky v obrysech (DMP OK) nebo None (pak ``chm``); ``fine_ortho(geom)`` → ortofoto
    hřiště 0,125 m nebo None (pak ``ortho``)."""
    import time

    from shapely.geometry import Point, shape
    from shapely.ops import unary_union
    from shapely.prepared import prep
    from shapely.strtree import STRtree

    t0 = time.time()

    def _props(f):
        return f.get("properties") or {}

    grounds, blockers = [], []
    n_private = 0
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
            if is_private_playground(p):
                n_private += 1  # soukromé: nic se nehledá, OSM body v něm zůstanou jak jsou
            elif not is_traffic_playground(p.get("name")):
                grounds.append(geom)
        else:
            blockers.append(geom)
    if not grounds:
        if log and n_private:
            log(f"Herní prvky: 0 hřišť (soukromá vynechána {n_private})")
        return feats
    # překrývající se obrysy téhož hřiště (OSM bývá zdvojené) jako jedno hřiště, ať nevzniknou dva křížky
    merged = unary_union(grounds)
    grounds = [g for g in getattr(merged, "geoms", [merged]) if g.geom_type == "Polygon" and not g.is_empty]
    eq_idx = [
        i for i, f in enumerate(feats)
        if _props(f).get("kind") == KIND and (f.get("geometry") or {}).get("type") == "Point"
    ]
    blk_tree = STRtree(blockers) if blockers else None
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
    hsrc = chm
    h_note = "chm.tif" if chm is not None else "bez výšky"
    if DETECT and height is not None:
        try:
            hr = height(grounds)
        except Exception as exc:  # bez PDAL / LAZ záloha chm.tif
            hr = None
            if log:
                log(f"Herní prvky: výška z DMP OK nedostupná ({exc}), záloha chm.tif")
        if hr is not None:
            hsrc, h_note = hr, "DMP OK 0,5 m"
    n_det = n_fb = n_osm = n_fine = n_sun = 0
    for k, geom in enumerate(grounds):
        near = [blockers[int(j)] for j in blk_tree.query(geom.buffer(1.0))] if blk_tree is not None else []
        local = unary_union(near) if near else None
        blk = prep(local.buffer(0.5)) if local is not None else None
        o_src = ortho
        sdir = None
        if DETECT and fine_ortho is not None and geom.area <= FINE_MAX_M2:
            try:
                fo = fine_ortho(geom)
            except Exception:  # síť: ortofoto jobu
                fo = None
            if fo is not None:
                o_src = fo
                n_fine += 1
                if SHADOWS:
                    got = _grid(fo, geom, FINE_PAD_M)
                    sun_h = chm if chm is not None else hsrc  # koruny i mimo obrys hřiště
                    if got is not None and sun_h is not None:
                        rgb, gx0, gy_top = got
                        nd = _sample_chm(sun_h, gx0, gy_top, float(fo.mpp_x), rgb.shape[:2])
                        sdir = sun_shadow_dir(rgb, nd, float(fo.mpp_x))
                        n_sun += sdir is not None
        cand = detect_candidates(geom, o_src, hsrc, blk, shadow_dir=sdir) if DETECT else []
        chosen = generalize(geom.area, per_ground[k], cand, scale)
        if not chosen:
            flat = [(x, y, 0.0, "osm") for x, y, t in per_ground[k] if t not in ("sandpit", "no")]
            if flat:
                chosen = flat[:1]
            elif FALLBACK:
                open_geom = geom.difference(local) if local is not None else geom
                fx, fy = fallback_point(open_geom if open_geom.area > 0.25 * geom.area else geom, o_src, hsrc)
                chosen = [(fx, fy, 0.0, "fallback")]
        for x, y, _s, src in chosen:
            det = "osm" if src == "osm" else ("fallback" if src == "fallback" else "detect")
            n_osm += det == "osm"
            n_det += det == "detect"
            n_fb += det == "fallback"
            props = {"source": det, "kind": KIND, "oom_code": "531"}
            if det == "detect":
                props["detect"] = src
            out.append(
                {"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [float(x), float(y)]}}
            )
    if log:
        log(
            f"Herní prvky: {len(grounds)} hřišť, křížky OSM {n_osm}, z výšky a ortofota {n_det}, záložní {n_fb}"
            f" (OSM bodů v hřištích {len(drop)}; soukromá vynechána {n_private}; výška {h_note}, ortofoto 0,125 m {n_fine}, slunce z výřezu {n_sun};"
            f" {time.time() - t0:.1f} s)"
        )
    return out
