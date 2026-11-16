"""Sprint: plocha ulic z katastru po pásech podél silnice a domu (Petr 2026-11-17, Most job 4c20a72105c9).

Parcela „ostatní komunikace“ často obsahuje i trávníky mezi paneláky. Suchý, flekatý trávník na letním ortofotu
po pixelech masku zeleně nesplní, takže se kreslil jako 501 s roztřepeným obrysem („cancoury“). Tady rozhoduje tvar:
z os zpevněných čar (OSM i ZABAGED Ulice / Silnice) vedou po ``STEP_M`` kolmé paprsky až k okraji parcely nebo
k budově, paprsek se dělí na buňky po ``CELL_M`` (šířka buňky = rozestup paprsků) a každá buňka dostane verdikt
tráva / zpevněno / nevím z podílu zelených pixelů ortofota. Zpevněno je od osy ven, dokud nepřijde souvisle tráva
(``GRASS_RUN_M``; užší trávník chůzi nezastaví – generalizace jako u mapaře), a totéž od domu / okraje parcely zpět
(chodník u domu). Šířky se podél osy vyhladí mediánem a z nich vzniknou pásy kopírující silnici a dům.

Koruna stromu (zelený pixel s výškou porostu ≥ ``TALL_M``) a stín nejsou tráva, ale „nevím“: silnice pod korunou
se nevyřízne; souvisle víc než ``UNKNOWN_RUN_M`` „nevím“ chůzi zastaví (park pod korunami). V ZABAGED udržované
zeleni a trvalém travním porostu stačí na trávu nižší podíl zeleně (``GREEN_SHARE_PRIOR``) – jen přednost, ne ořez.
"""

from __future__ import annotations

import io
import json
import math
import zipfile
from pathlib import Path

# Záloha: False = dnešní chování (parcela minus spolehlivá zeleň po pixelech).
STREET_BANDS = True

SURFACE_PNG_NAME = "povrch.png"  # třídy pixelů ortofota: 0 jiné, 1 zeleň (bez morfologie), 2 tma (stín)
STEP_M = 4.0
CELL_M = 1.0
RAY_MAX_M = 60.0
START_TOL_M = 1.5
SAMPLE_M = 0.5
GREEN_SHARE = 0.25
GREEN_SHARE_PRIOR = 0.12
KNOWN_MIN = 0.4
DARK_VALUE = 60
TALL_M = 2.0
GRASS_RUN_M = 3.0
UNKNOWN_RUN_M = 6.0
SMOOTH_STATIONS = 5
MIN_BAND_M = 0.5
TILE_M = 100.0
ZABAGED_ROAD_LAYERS = ("Ulice", "SilniceDalnice")
ZABAGED_GRASS_LAYERS = ("UdrzovanaZelen", "TrvalyTravniPorost")

SURFACE_OTHER, SURFACE_GREEN, SURFACE_DARK = 0, 1, 2


class Raster:
    """2D pole s počátkem v levém horním rohu (S-JTSK) a velikostí pixelu v metrech."""

    def __init__(self, data, x0: float, y_top: float, mpp_x: float, mpp_y: float):
        self.data, self.x0, self.y_top, self.mpp_x, self.mpp_y = data, x0, y_top, mpp_x, mpp_y

    def sample(self, xs, ys, fill):
        import numpy as np

        c = np.floor((xs - self.x0) / self.mpp_x).astype(int)
        r = np.floor((self.y_top - ys) / self.mpp_y).astype(int)
        ok = (c >= 0) & (r >= 0) & (c < self.data.shape[1]) & (r < self.data.shape[0])
        out = np.full(xs.shape, fill, dtype=self.data.dtype)
        out[ok] = self.data[r[ok], c[ok]]
        return out


def surface_classes(rgb, exg_min: float, sat_min: float):
    """uint8 třídy pixelů ortofota (``SURFACE_*``): zeleň bez morfologie, tma = nejjasnější kanál pod ``DARK_VALUE``."""
    import numpy as np

    from app.pipeline.orthophoto import green_mask

    out = np.zeros(rgb.shape[:2], dtype=np.uint8)
    out[green_mask(rgb, exg_min, sat_min)] = SURFACE_GREEN
    out[np.asarray(rgb).max(axis=-1) < DARK_VALUE] = SURFACE_DARK
    return out


def load_surface(work_dir: Path, cache_subdir: str, meta_name: str) -> Raster | None:
    import numpy as np
    from PIL import Image

    out_dir = Path(work_dir) / cache_subdir
    png, meta_path = out_dir / SURFACE_PNG_NAME, out_dir / meta_name
    if not png.is_file() or not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    xmin, ymin, xmax, ymax = meta["bounds"]
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(png) as im:
        data = np.asarray(im.convert("L"))
    if data.shape != (int(meta["height"]), int(meta["width"])):
        return None
    return Raster(data, xmin, ymax, (xmax - xmin) / data.shape[1], (ymax - ymin) / data.shape[0])


def load_chm(work_dir: Path) -> Raster | None:
    try:
        import numpy as np

        from app.pipeline.gdal_cli_raster import read_float32_geotiff
        from app.pipeline.vegetation_chm import chm_path

        src = chm_path(Path(work_dir))
        if src is None:
            return None
        arr, gt, nodata = read_float32_geotiff(src)
        arr = np.array(arr, dtype=np.float32)
        if nodata is not None:
            arr[arr == nodata] = np.nan
        return Raster(arr, gt[0], gt[3], gt[1], -gt[5])
    except Exception:  # bez výšky porostu je zelená koruna nad ulicí tráva jako dosud
        return None


def _zabaged_geoms(work_dir: Path, layers) -> list:
    """Geometrie vrstev ZABAGED ze ``zabaged_clean.zip`` (shapely); bez archivu nebo pyshp prázdný seznam."""
    archive = Path(work_dir) / "zabaged_clean.zip"
    if not archive.is_file():
        return []
    try:
        import shapefile
        from shapely.geometry import shape

        out = []
        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
            for stem in layers:
                if not {f"{stem}.shp", f"{stem}.shx", f"{stem}.dbf"} <= names:
                    continue
                reader = shapefile.Reader(
                    shp=io.BytesIO(zf.read(f"{stem}.shp")),
                    shx=io.BytesIO(zf.read(f"{stem}.shx")),
                    dbf=io.BytesIO(zf.read(f"{stem}.dbf")),
                )
                for sh in reader.iterShapes():
                    if sh.shapeType == 0:
                        continue
                    try:
                        g = shape(sh.__geo_interface__)
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if not g.is_empty:
                        out.append(g if g.is_valid else g.buffer(0))
        return out
    except Exception:  # ZABAGED je jen doplněk
        return []


def zabaged_road_lines(work_dir: Path) -> list:
    """Osy ZABAGED Ulice / Silnice: hlavní silnice v čarách přípravy OSM chybět může (Most, vedou jen chodníky)."""
    out = []
    for g in _zabaged_geoms(work_dir, ZABAGED_ROAD_LAYERS):
        out += [p for p in getattr(g, "geoms", [g]) if p.geom_type == "LineString" and len(p.coords) >= 2]
    return out


def zabaged_grass_prior(work_dir: Path):
    from shapely.ops import unary_union

    polys = [g for g in _zabaged_geoms(work_dir, ZABAGED_GRASS_LAYERS) if g.geom_type in ("Polygon", "MultiPolygon")]
    return unary_union(polys) if polys else None


def _tiles(cand):
    """Kandidát rozřezaný na dlaždice ``TILE_M`` (STRtree): průsečík paprsku jen s malými kusy, ne s celou sítí."""
    from shapely import STRtree
    from shapely.geometry import box

    xmin, ymin, xmax, ymax = cand.bounds
    pieces = []
    y = ymin
    while y < ymax:
        x = xmin
        while x < xmax:
            part = cand.intersection(box(x, y, x + TILE_M, y + TILE_M))
            if not part.is_empty:
                pieces.append(part)
            x += TILE_M
        y += TILE_M
    return pieces, STRtree(pieces)


def walk(verdicts: list, grass_run: int, unknown_run: int, length: float) -> float:
    """Délka zpevněného úseku od začátku řady verdiktů (True tráva, False zpevněno, None nevím)."""
    run = unk = 0
    for i, v in enumerate(verdicts):
        if v is None:
            unk += 1
            if unk > unknown_run:
                return (i - unk + 1) * CELL_M
            continue
        unk = 0
        run = run + 1 if v else 0
        if run >= grass_run:
            return (i - grass_run + 1) * CELL_M
    return length


def _median_smooth(values: list[float]) -> list[float]:
    import numpy as np

    a = np.asarray(values, dtype=float)
    h = SMOOTH_STATIONS // 2
    return [float(np.median(a[max(0, i - h) : i + h + 1])) for i in range(len(a))]


def _cell_verdicts(p, u, n, length: float, surface: Raster, chm: Raster | None, prior) -> list:
    """Verdikty buněk paprsku (True tráva, False zpevněno, None nevím) z pixelů ortofota a výšky porostu."""
    import numpy as np
    import shapely

    ts = np.arange(SAMPLE_M / 2, length, SAMPLE_M)
    if ts.size == 0:
        return []
    ss = np.arange(-STEP_M / 2 + SAMPLE_M / 2, STEP_M / 2, SAMPLE_M)
    T, S = np.meshgrid(ts, ss)
    xs = p[0] + n[0] * T + u[0] * S
    ys = p[1] + n[1] * T + u[1] * S
    cls = surface.sample(xs, ys, 255)
    green = cls == SURFACE_GREEN
    known = (cls != SURFACE_DARK) & (cls != 255)
    if chm is not None:
        h = np.nan_to_num(chm.sample(xs, ys, np.float32(0.0)))
        known &= ~(green & (h >= TALL_M))
    cell = np.floor(T / CELL_M).astype(int)
    ncell = int(math.ceil(length / CELL_M - 1e-9))
    out = []
    for k in range(ncell):
        m = cell == k
        tot = int(m.sum())
        kn = int((m & known).sum())
        if tot == 0 or kn < KNOWN_MIN * tot:
            out.append(None)
            continue
        share = float((m & known & green).sum()) / kn
        thr = GREEN_SHARE
        if prior is not None:
            tm = (k + 0.5) * CELL_M
            if shapely.contains_xy(prior, p[0] + n[0] * tm, p[1] + n[1] * tm):
                thr = GREEN_SHARE_PRIOR
        out.append(share >= thr)
    return out


def band_geometry(cand, lines, surface: Raster, *, chm: Raster | None = None, grass_prior=None):
    """Zpevněné pásy (shapely, S-JTSK) uvnitř ``cand`` (parcely − budovy) podél os ``lines`` (shapely LineString)."""
    import shapely
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import unary_union

    if cand is None or cand.is_empty or not lines:
        return None
    pieces, tree = _tiles(cand)
    prior = None
    if grass_prior is not None and not grass_prior.is_empty:
        prior = grass_prior.intersection(cand.buffer(1.0))
        shapely.prepare(prior)
    grass_run = max(1, int(round(GRASS_RUN_M / CELL_M)))
    unknown_run = max(1, int(round(UNKNOWN_RUN_M / CELL_M)))
    quads = []
    for ln in lines:
        if ln.length < STEP_M or not tree.query(ln, predicate="intersects").size:
            continue
        stations = []
        k = 0
        while k * STEP_M <= ln.length:
            s = k * STEP_M
            a, b = ln.interpolate(max(0.0, s - 0.5)), ln.interpolate(min(ln.length, s + 0.5))
            dx, dy = b.x - a.x, b.y - a.y
            h = math.hypot(dx, dy)
            if h > 0:
                stations.append((ln.interpolate(s), (dx / h, dy / h)))
            k += 1
        for side in (1.0, -1.0):
            rows = []
            for pt, u in stations:
                n = (-u[1] * side, u[0] * side)
                p = (pt.x, pt.y)
                ray = LineString([p, (p[0] + n[0] * RAY_MAX_M, p[1] + n[1] * RAY_MAX_M)])
                local = [pieces[i] for i in tree.query(ray, predicate="intersects")]
                length = 0.0
                if local:
                    seg = ray.intersection(unary_union(local))
                    if seg.geom_type == "MultiLineString":
                        seg = shapely.line_merge(seg)  # kusy z sousedních dlaždic do souvislých úseků
                    start = Point(p)
                    for g in getattr(seg, "geoms", [seg]):
                        if g.geom_type == "LineString" and not g.is_empty and start.distance(g) <= START_TOL_M:
                            length = max(length, max(start.distance(Point(c)) for c in g.coords))
                if length <= 0:
                    rows.append((p, u, n, 0.0, 0.0, 0.0))
                    continue
                ver = _cell_verdicts(p, u, n, length, surface, chm, prior)
                wr = walk(ver, grass_run, unknown_run, length)
                wb = walk(list(reversed(ver)), grass_run, unknown_run, length)
                rows.append((p, u, n, min(wr, length), min(wb, length), length))
            if len(rows) < 2:
                continue
            wr = _median_smooth([r[3] for r in rows])
            wb = _median_smooth([r[4] for r in rows])
            for i in range(len(rows) - 1):
                (p, _u, n, _a, _b, l1), (p2, _u2, n2, _a2, _b2, l2) = rows[i], rows[i + 1]
                if wr[i] > MIN_BAND_M and wr[i + 1] > MIN_BAND_M:
                    a1, a2 = wr[i], wr[i + 1]
                    quads.append(
                        Polygon([p, p2, (p2[0] + n2[0] * a2, p2[1] + n2[1] * a2), (p[0] + n[0] * a1, p[1] + n[1] * a1)])
                    )
                if wb[i] > MIN_BAND_M and wb[i + 1] > MIN_BAND_M and min(l1, l2) > 0:
                    b1, b2 = wb[i], wb[i + 1]
                    quads.append(
                        Polygon(
                            [
                                (p[0] + n[0] * (l1 - b1), p[1] + n[1] * (l1 - b1)),
                                (p2[0] + n2[0] * (l2 - b2), p2[1] + n2[1] * (l2 - b2)),
                                (p2[0] + n2[0] * l2, p2[1] + n2[1] * l2),
                                (p[0] + n[0] * l1, p[1] + n[1] * l1),
                            ]
                        )
                    )
    quads = [q if q.is_valid else q.buffer(0) for q in quads]
    quads = [q for q in quads if not q.is_empty]
    if not quads:
        return Polygon()  # pásy proběhly a zpevněné nic: nekreslit (None by vrátil dnešní cestu)
    return unary_union(quads).intersection(cand)
