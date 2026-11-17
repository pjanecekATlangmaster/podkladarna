"""Sprint: plocha ulic z katastru po pásech podél silnice (Petr 2026-11-17, Most job 4c20a72105c9).

Parcela „ostatní komunikace“ často obsahuje i trávníky mezi paneláky. Suchý, flekatý trávník na letním ortofotu
po pixelech masku zeleně nesplní, takže se kreslil jako 501 s roztřepeným obrysem. Tady rozhoduje tvar: z os silnic
(OSM silnice s půlkou šířky vozovky podle symbolu, ZABAGED Ulice / Silnice; chodník ani pěší zóna osou nejsou) vedou
po ``STEP_M`` kolmé paprsky do okraje parcely, nejdál ``EXTRA_MAX_M`` za okraj vozovky. Vozovka je zpevněná vždy.
Za ní se paprsek dělí na buňky po ``CELL_M`` s verdiktem tráva / zpevněno / nevím z podílu zelených pixelů ortofota;
zpevněno je od osy ven, dokud nepřijde souvisle tráva (``GRASS_RUN_M``; užší proužek chůzi nezastaví). Přesah za
vozovku se kreslí jen jako pravidelný tvar: úsek aspoň ``RUN_MIN_M`` dlouhý s konstantní šířkou (medián, většina
stanic do ``SHAPE_TOL_M`` od něj) – parkovací záliv, stání nebo chodník podél silnice. Jinak se nekreslí (Petr:
raději nic než podivný tvar). Od domu se už nechodí.

Koruna stromu (zelený pixel s výškou porostu ≥ ``TALL_M``) a stín nejsou tráva, ale „nevím“: silnice pod korunou
se nevyřízne; souvisle víc než ``UNKNOWN_RUN_M`` „nevím“ chůzi zastaví. V ZABAGED udržované zeleni a trvalém
travním porostu stačí na trávu nižší podíl zeleně (``GREEN_SHARE_PRIOR``) – jen přednost, ne ořez.
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
STEP_M = 2.0
CELL_M = 1.0
# Za okrajem vozovky jen pravidelný pás (záliv, stání, chodník) nejdál EXTRA_MAX_M (Petr 2026-11-17, Most: „5 m → 10 m“).
EXTRA_MAX_M = 10.0
EXTRA_MIN_M = 1.0
GAP_STATIONS = 1
RUN_MIN_M = 6.0
SHAPE_TOL_M = 1.0
SHAPE_SHARE = 0.7
# Osy pásů jen silnice (chodník / pěší zóna ne): půlka šířky vozovky podle symbolu přípravy OSM, ZABAGED jako výchozí.
ROAD_HALF_M = {"road_1": 2.5, "road_2": 3.0, "road_3": 3.5, "road_4": 4.5}
DEFAULT_HALF_M = 3.0
RAW_ROAD_HIGHWAYS = frozenset(
    {"living_street", "residential", "service", "unclassified", "tertiary", "secondary", "primary", "trunk"}
)
START_TOL_M = 1.5
SAMPLE_M = 0.5
GREEN_SHARE = 0.25
GREEN_SHARE_PRIOR = 0.12
KNOWN_MIN = 0.4
DARK_VALUE = 60
TALL_M = 2.0
GRASS_RUN_M = 3.0
UNKNOWN_RUN_M = 6.0
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


def road_half_width(highway: str) -> float | None:
    """Půlka šířky vozovky (m) pro osu pásů; None = není silnice (chodník, pěší zóna, cyklostezka se osami nestávají)."""
    hw = (highway or "").lower()
    if hw.startswith("road_"):
        return ROAD_HALF_M.get(hw, DEFAULT_HALF_M)
    if hw in RAW_ROAD_HIGHWAYS:
        return DEFAULT_HALF_M
    return None


def _segments(vals: list[float]) -> list[tuple[int, int]]:
    """Rozdělí úsek na kusy konstantní šířky: nový kus, když se dvě stanice po sobě odchýlí od mediánu o víc než tolerance."""
    import numpy as np

    out, start, dev = [], 0, 0
    for k in range(1, len(vals)):
        med = float(np.median(vals[start:k]))
        if abs(vals[k] - med) > SHAPE_TOL_M:
            dev += 1
            if dev >= 2:
                out.append((start, k - 2))
                start, dev = k - 1, 0
        else:
            dev = 0
    out.append((start, len(vals) - 1))
    return out


def _runs(extra: list[float]) -> list[tuple[int, int, float]]:
    """Úseky stanic (od, do včetně, šířka) s pravidelným pásem za vozovkou: záliv / stání / chodník konstantní šířky.

    Stanice s přesahem ≥ ``EXTRA_MIN_M``, mezery do ``GAP_STATIONS`` přemostěné; úsek se dělí na kusy konstantní šířky
    (``_segments``). Kus se kreslí, je-li dlouhý aspoň ``RUN_MIN_M`` a aspoň ``SHAPE_SHARE`` stanic leží do
    ``SHAPE_TOL_M`` od mediánu; šířka je medián. Šikmý nebo rozeklaný tvar se rozpadne na krátké kusy a nekreslí se.
    """
    import numpy as np

    on = [e >= EXTRA_MIN_M for e in extra]
    out = []
    i, n = 0, len(on)
    while i < n:
        if not on[i]:
            i += 1
            continue
        j, gap, k = i, 0, i + 1
        while k < n:
            if on[k]:
                j, gap = k, 0
            else:
                gap += 1
                if gap > GAP_STATIONS:
                    break
            k += 1
        vals = [float(e) for e in extra[i : j + 1]]
        for a, b in _segments(vals):
            if (b - a) * STEP_M < RUN_MIN_M:
                continue
            seg = np.asarray(vals[a : b + 1])
            med = float(np.median(seg))
            if med >= EXTRA_MIN_M and float(np.mean(np.abs(seg - med) <= SHAPE_TOL_M)) >= SHAPE_SHARE:
                out.append((i + a, i + b, med))
        i = j + 1
    return out


def band_geometry(cand, lines, surface: Raster, *, chm: Raster | None = None, grass_prior=None):
    """Plocha ulice (shapely, S-JTSK) uvnitř ``cand`` (parcely − budovy) podél os silnic.

    ``lines`` = (LineString, půlka šířky vozovky) nebo LineString (``DEFAULT_HALF_M``). Vozovka se kreslí vždy, za ní
    jen pravidelný pás (záliv, stání, chodník) do ``EXTRA_MAX_M`` od okraje vozovky (Petr 2026-11-17, Most).
    """
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
    for item in lines:
        ln, half = item if isinstance(item, tuple) else (item, DEFAULT_HALF_M)
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
        reach = half + EXTRA_MAX_M
        for side in (1.0, -1.0):
            rows = []
            for pt, u in stations:
                n = (-u[1] * side, u[0] * side)
                p = (pt.x, pt.y)
                ray = LineString([p, (p[0] + n[0] * reach, p[1] + n[1] * reach)])
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
                extra = 0.0
                if length > half + EXTRA_MIN_M:
                    ver = _cell_verdicts(p, u, n, length, surface, chm, prior)
                    road_cells = int(math.ceil(half / CELL_M))
                    ver = [False] * min(road_cells, len(ver)) + ver[road_cells:]  # vozovka je zpevněná vždy
                    extra = max(0.0, min(walk(ver, grass_run, unknown_run, length), length) - half)
                rows.append((p, n, min(half, length), extra))
            if len(rows) < 2:
                continue
            for i in range(len(rows) - 1):
                (p, n, r1, _), (p2, n2, r2, _) = rows[i], rows[i + 1]
                if r1 > MIN_BAND_M and r2 > MIN_BAND_M:
                    quads.append(
                        Polygon([p, p2, (p2[0] + n2[0] * r2, p2[1] + n2[1] * r2), (p[0] + n[0] * r1, p[1] + n[1] * r1)])
                    )
            for i0, i1, w in _runs([r[3] for r in rows]):
                a = half + w
                for i in range(i0, i1):
                    (p, n, _, _), (p2, n2, _, _) = rows[i], rows[i + 1]
                    quads.append(
                        Polygon(
                            [
                                (p[0] + n[0] * (half - 0.5), p[1] + n[1] * (half - 0.5)),
                                (p2[0] + n2[0] * (half - 0.5), p2[1] + n2[1] * (half - 0.5)),
                                (p2[0] + n2[0] * a, p2[1] + n2[1] * a),
                                (p[0] + n[0] * a, p[1] + n[1] * a),
                            ]
                        )
                    )
    quads = [q if q.is_valid else q.buffer(0) for q in quads]
    quads = [q for q in quads if not q.is_empty]
    if not quads:
        return Polygon()  # pásy proběhly a zpevněné nic: nekreslit (None by vrátil dnešní cestu)
    return unary_union(quads).intersection(cand)
