"""Ověření mezer v residential (sprint, volba ``sprint_residual_paved``).

Petr 2026-11-02 (rozbor ``/mnt/project-files/residential-mezery/``): pravidlo „víc než 25 % residential bez OSM →
přeskočit celé residential“ nechalo kreslit jen dobře zmapovaná sídliště (4 z 5 zkoušených míst nic, 4 % zpevněných
mezer). Místo něj se každý kus mezery ověří zvlášť:

- **zeleň z ortofota** pod ``GREEN_MAX_SHARE`` (práh ExG kalibrovaný v jobu podle OSM silnic a trávníků),
- **výška nad terénem** (``chm.tif``) nad ``TALL_M`` na méně než ``TALL_MAX_SHARE`` plochy (stromy, nezmapované budovy).

Kde má residential úplné plochy z DTM (budovy z DTM pokrývají aspoň ``DTM_COMPLETE_SHARE`` OSM budov), mezery se
nekreslí: zpevněné plochy tam kreslí DTM a zbytek bývá zeleň. Bez ortofota i CHM platí původní pravidlo 25 %.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Záloha: False = původní chování (pravidlo 25 % pro celé residential, bez ohledu na DTM).
VERIFY_PIECES = True

GREEN_MAX_SHARE = 0.25
TALL_MAX_SHARE = 0.30
TALL_M = 2.0
# Residential s úplnými plochami DTM: budovy z DTM pokrývají aspoň tento podíl OSM budov (při ≥ DTM_MIN_OSM_BUILDINGS_M2).
DTM_COMPLETE_SHARE = 0.5
DTM_MIN_OSM_BUILDINGS_M2 = 200.0

ORTHO_MPP = 0.5
EXG_DEFAULT = 0.02
SAT_MIN = 0.12
# Kalibrace: práh ExG mezi 95. percentilem pod OSM silnicemi a 25. percentilem na OSM trávnících (bez pixelů pod
# korunami). Jen snižuje: na letních snímcích ČÚZK je pevný práh stejně dobrý (5 míst, 82 % proti 83 %), nižší práh
# pomůže u vybledlé trávy; vyšší pustil zahrady.
EXG_CLAMP = (0.0, EXG_DEFAULT)
CALIB_MIN_PX = 400
ROAD_HALF_M = 1.0
GRASS_INSET_M = 2.0
CALIB_ROADS = frozenset(
    {"residential", "service", "unclassified", "tertiary", "secondary", "primary", "living_street"}
)
CALIB_GRASS_LANDUSE = frozenset({"grass", "meadow", "village_green"})
CALIB_GRASS_LEISURE = frozenset({"park", "garden"})


@dataclass
class Raster:
    data: object  # numpy pole (H×W×3 uint8 u ortofota, H×W float32 u CHM)
    x0: float
    y_top: float
    mpp_x: float
    mpp_y: float


@dataclass
class Evidence:
    ortho: Raster | None = None
    chm: Raster | None = None
    exg_min: float = EXG_DEFAULT

    @property
    def usable(self) -> bool:
        return self.ortho is not None or self.chm is not None


def _window(r: Raster, geom):
    """(r0, r1, c0, c1, maska) pixelů rastru ``r`` pokrytých plochou ``geom`` (shapely), nebo None."""
    import numpy as np
    from PIL import Image, ImageDraw

    h, w = r.data.shape[:2]
    bx0, by0, bx1, by1 = geom.bounds
    c0, c1 = max(0, int((bx0 - r.x0) / r.mpp_x)), min(w, int(math.ceil((bx1 - r.x0) / r.mpp_x)))
    r0, r1 = max(0, int((r.y_top - by1) / r.mpp_y)), min(h, int(math.ceil((r.y_top - by0) / r.mpp_y)))
    if c1 <= c0 or r1 <= r0:
        return None
    img = Image.new("1", (c1 - c0, r1 - r0), 0)
    draw = ImageDraw.Draw(img)
    ox, oy = r.x0 + c0 * r.mpp_x, r.y_top - r0 * r.mpp_y

    def px(ring):
        return [((x - ox) / r.mpp_x, (oy - y) / r.mpp_y) for x, y in ring.coords]

    for poly in getattr(geom, "geoms", [geom]):
        if poly.geom_type != "Polygon" or poly.is_empty:
            continue
        draw.polygon(px(poly.exterior), fill=1)
        for hole in poly.interiors:
            draw.polygon(px(hole), fill=0)
    mask = np.asarray(img, dtype=bool)
    if not mask.any():
        return None
    return r0, r1, c0, c1, mask


def _exg_sat(rgb):
    import numpy as np

    a = rgb.astype(np.float32) / 255.0
    mx, mn = a.max(axis=-1), a.min(axis=-1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    return 2.0 * a[..., 1] - a[..., 0] - a[..., 2], sat


def _low_mask(chm: Raster | None, ortho: Raster, r0: int, r1: int, c0: int, c1: int):
    """bool okna ortofota: pixel není pod korunou / střechou (CHM ≤ ``TALL_M``); bez CHM vše True."""
    import numpy as np

    if chm is None:
        return None
    ys = ortho.y_top - (np.arange(r0, r1) + 0.5) * ortho.mpp_y
    xs = ortho.x0 + (np.arange(c0, c1) + 0.5) * ortho.mpp_x
    rr = np.clip(((chm.y_top - ys) / chm.mpp_y).astype(int), 0, chm.data.shape[0] - 1)
    cc = np.clip(((xs - chm.x0) / chm.mpp_x).astype(int), 0, chm.data.shape[1] - 1)
    sub = chm.data[np.ix_(rr, cc)]
    return ~(sub > TALL_M)


def _sample_exg(r: Raster, geoms, chm: Raster | None = None) -> list:
    """ExG pixelů ortofota v plochách ``geoms``; s CHM bez pixelů pod korunami stromů (silnice v aleji)."""
    import numpy as np

    vals = []
    for g in geoms:
        win = _window(r, g)
        if win is None:
            continue
        r0, r1, c0, c1, mask = win
        low = _low_mask(chm, r, r0, r1, c0, c1)
        if low is not None:
            mask = mask & low
        exg, _ = _exg_sat(r.data[r0:r1, c0:c1])
        vals.append(exg[mask])
    return np.concatenate(vals) if vals else np.zeros(0, dtype=np.float32)


def calibrate_exg(r: Raster, road_geoms, grass_geoms, chm: Raster | None = None) -> tuple[float, str]:
    """Práh ExG pro tento snímek: mezi silnicemi a trávníky z OSM v místě; jinak výchozí ``EXG_DEFAULT``."""
    import numpy as np

    roads, grass = _sample_exg(r, road_geoms, chm), _sample_exg(r, grass_geoms, chm)
    if roads.size < CALIB_MIN_PX or grass.size < CALIB_MIN_PX:
        return EXG_DEFAULT, f"výchozí (málo vzorků: silnice {roads.size} px, trávník {grass.size} px)"
    p_road, p_grass = float(np.percentile(roads, 95)), float(np.percentile(grass, 25))
    if p_grass <= p_road:
        return EXG_DEFAULT, f"výchozí (silnice a trávník nerozlišené: {p_road:.3f} ≥ {p_grass:.3f})"
    thr = min(max((p_road + p_grass) / 2.0, EXG_CLAMP[0]), EXG_CLAMP[1])
    return thr, f"kalibrováno {thr:.3f} (silnice p95 {p_road:.3f}, trávník p25 {p_grass:.3f})"


def calibration_geoms(elements: list[dict]):
    """Plochy pro kalibraci z OSM dumpu: pás kolem silnic a vnitřek trávníků / parků (S-JTSK)."""
    from shapely.geometry import LineString, Polygon

    from app.pipeline.crs_5514 import wgs84_to_projected

    roads, grass = [], []
    for el in elements:
        if el.get("type") != "way":
            continue
        tags = el.get("tags") or {}
        pts = []
        for node in el.get("geometry") or []:
            if isinstance(node, dict) and node.get("lat") is not None and node.get("lon") is not None:
                pts.append(wgs84_to_projected(float(node["lat"]), float(node["lon"])))
        if len(pts) < 2:
            continue
        hw = str(tags.get("highway") or "")
        if hw in CALIB_ROADS and not tags.get("tunnel") and not tags.get("bridge") and not tags.get("covered"):
            roads.append(LineString(pts).buffer(ROAD_HALF_M, cap_style=2))
        elif (
            str(tags.get("landuse") or "") in CALIB_GRASS_LANDUSE
            or str(tags.get("leisure") or "") in CALIB_GRASS_LEISURE
        ) and len(pts) >= 4:
            try:
                g = Polygon(pts).buffer(0).buffer(-GRASS_INSET_M)
            except ValueError:
                continue
            if not g.is_empty:
                grass.append(g)
    return roads, grass


def load_evidence(work_dir: Path, bbox_wgs84, elements: list[dict], log=None) -> Evidence:
    """Ortofoto (stejná cache jako ulice z katastru) a CHM jobu; co chybí, je None."""
    ev = Evidence()
    if bbox_wgs84 is not None:
        try:
            from app.pipeline.fetch_openzu import crop_bounds_5514
            from app.pipeline.orthophoto import fetch_orthophoto
            from app.pipeline.street_parcels import street_fetch_bounds_5514

            got = fetch_orthophoto(
                work_dir,
                street_fetch_bounds_5514(bbox_wgs84),
                ORTHO_MPP,
                log=log,
                need_bounds=crop_bounds_5514(*tuple(bbox_wgs84), buffer_m=0.0),
            )
            if got is not None:
                rgb, gt = got
                ev.ortho = Raster(rgb, gt[0], gt[3], gt[1], -gt[5])
        except Exception as exc:  # mapa se kreslí i bez ortofota
            logger.warning("residual_verify: ortofoto nedostupné (%s)", exc)
    try:
        from app.pipeline.gdal_cli_raster import read_float32_geotiff
        from app.pipeline.vegetation_chm import chm_path

        src = chm_path(Path(work_dir))
        if src is not None:
            arr, gt, nodata = read_float32_geotiff(src)
            if nodata is not None:
                arr = arr.copy()
                arr[arr == nodata] = float("nan")
            ev.chm = Raster(arr, gt[0], gt[3], gt[1], -gt[5])
    except Exception as exc:  # bez CHM jen ortofoto
        logger.warning("residual_verify: CHM nedostupné (%s)", exc)
    if ev.ortho is not None:
        roads, grass = calibration_geoms(elements)
        ev.exg_min, note = calibrate_exg(ev.ortho, roads, grass, ev.chm)
        logger.info("residual_paved: práh zeleně z ortofota %s", note)
        if log:
            log(f"residual_paved: práh zeleně z ortofota {note}")
    return ev


def piece_shares(ev: Evidence, geom) -> tuple[float | None, float | None]:
    """(podíl zeleně z ortofota, podíl výšky > ``TALL_M`` z CHM) v kusu; None = podklad chybí / mimo rastr."""
    import numpy as np

    green = tall = None
    if ev.ortho is not None:
        win = _window(ev.ortho, geom)
        if win is not None:
            r0, r1, c0, c1, mask = win
            exg, sat = _exg_sat(ev.ortho.data[r0:r1, c0:c1])
            green = float(((exg > ev.exg_min) & (sat > SAT_MIN))[mask].mean())
    if ev.chm is not None:
        win = _window(ev.chm, geom)
        if win is not None:
            r0, r1, c0, c1, mask = win
            vals = ev.chm.data[r0:r1, c0:c1][mask]
            vals = vals[np.isfinite(vals)]
            if vals.size:
                tall = float((vals > TALL_M).mean())
    return green, tall


def piece_is_paved(ev: Evidence, geom) -> bool:
    """Kus mezery se kreslí, jen když v něm není zeleň ani vysoké objekty (co nejde změřit, nerozhoduje)."""
    green, tall = piece_shares(ev, geom)
    if green is not None and green >= GREEN_MAX_SHARE:
        return False
    if tall is not None and tall >= TALL_MAX_SHARE:
        return False
    return green is not None or tall is not None


def dtm_building_geoms(work_dir: Path) -> list:
    """Plochy budov z DTM jobu (prázdné bez DTM nebo při ``dtm_disabled`` / neúplné obci)."""
    from shapely.geometry import Polygon

    from app.pipeline.dtm_import import _load_polygons_file, buildings_path

    out = []
    path = buildings_path(Path(work_dir))
    if not path.is_file():
        return out
    for poly in _load_polygons_file(path):
        try:
            g = Polygon(poly[0], poly[1:]).buffer(0)
        except (ValueError, IndexError):
            continue
        if not g.is_empty:
            out.append(g)
    return out


def dtm_complete(subject, osm_buildings, dtm_buildings) -> bool:
    """Residential má úplné plochy DTM: budovy z DTM pokrývají aspoň ``DTM_COMPLETE_SHARE`` plochy OSM budov."""
    from shapely.ops import unary_union

    if not dtm_buildings:
        return False
    osm = [g for g in osm_buildings if g.intersects(subject)]
    if not osm:
        return False
    osm_u = unary_union(osm).intersection(subject)
    if osm_u.area < DTM_MIN_OSM_BUILDINGS_M2:
        return False
    dtm = [g for g in dtm_buildings if g.intersects(subject)]
    if not dtm:
        return False
    covered = osm_u.intersection(unary_union(dtm)).area
    return covered / osm_u.area >= DTM_COMPLETE_SHARE


def osm_building_geoms(elements: list[dict]) -> list:
    """Plochy OSM budov (uzavřené way) z dumpu pro residual (S-JTSK)."""
    from shapely.geometry import Polygon

    from app.pipeline.crs_5514 import wgs84_to_projected

    out = []
    for el in elements:
        if el.get("type") != "way" or "building" not in (el.get("tags") or {}):
            continue
        pts = [
            wgs84_to_projected(float(n["lat"]), float(n["lon"]))
            for n in el.get("geometry") or []
            if isinstance(n, dict) and n.get("lat") is not None and n.get("lon") is not None
        ]
        if len(pts) < 4:
            continue
        try:
            g = Polygon(pts).buffer(0)
        except ValueError:
            continue
        if not g.is_empty:
            out.append(g)
    return out


def street_parcel_wkbs(work_dir: Path) -> list[bytes]:
    """Parcely komunikací z katastru (kreslí je ulice ve sprintu, mezery se s nimi nesmí zdvojit)."""
    from shapely.geometry import shape

    from app.pipeline.street_parcels import CACHE_SUBDIR, PARCELS_NAME, SPRINT_STREET_PARCELS

    if not SPRINT_STREET_PARCELS:
        return []
    path = Path(work_dir) / CACHE_SUBDIR / PARCELS_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[bytes] = []
    for feat in data.get("features") or []:
        try:
            g = shape(feat.get("geometry") or {}).buffer(0)
        except (ValueError, TypeError, AttributeError):
            continue
        if not g.is_empty:
            out.append(g.wkb)
    return out
