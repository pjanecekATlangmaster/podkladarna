"""ZABAGED „Silnice neevidovaná“ (vrstva ``SilniceNeevidovana``) jen tam, kde ji potvrdí druhý zdroj.

Vrstva obsahuje i nové účelové komunikace, které v terénu často nejsou (Lochkov: FID 531893833434325,
531898212287701, 531897708971221). Úsek čáry se proto kreslí, jen když leží do ``CONFIRM_M`` od

1. plochy silnice z DTM (``work/dtm/roads.json``) nebo osy pozemní komunikace z DTM sady DI (``work/dtm/road_axes.json``),
2. jinak když ortofoto v pásu ±``ORTHO_STRIP_M`` od osy vypadá jako zpevněný povrch (nezelený, šedý, ne tmavý stín).

Vzorkování po ``SAMPLE_M``, souvislé potvrzené úseky aspoň ``MIN_RUN_M``, mezery do ``GAP_M`` se zacelí.
Čistá logika (``confirm_line``) pracuje jen s predikáty nad souřadnicemi, takže jde testovat bez sítě.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

LAYER = "SilniceNeevidovana"
AXES_NAME = "road_axes.json"

SAMPLE_M = 2.0  # krok vzorkování osy
CONFIRM_M = 6.0  # vzdálenost od plochy / osy DTM, do které je úsek potvrzený
MIN_RUN_M = 20.0  # nejkratší nakreslený souvislý úsek (počet vzorků: ceil)
GAP_M = 15.0  # kratší mezery mezi potvrzenými úseky se zacelí
ORTHO_STRIP_M = 1.5  # ortofoto: pás ±tolik m od osy
ORTHO_STEP_M = 0.5  # krok napříč pásem
ORTHO_STRIP_SHARE = 0.7  # podíl „zpevněných“ pixelů v pásu, aby vzorek vypadal jako zpevněný pás
ORTHO_WINDOW_M = 20.0  # okno, ve kterém se podíl vzorků počítá
ORTHO_SHARE = 0.6  # aspoň tolik vzorků v okně vypadá zpevněně
ORTHO_MAX_SAT = 0.2  # sytost: asfalt a dlažba jsou šedé
ORTHO_MIN_VALUE = 50  # nejtmavší povolený pixel (hluboký stín není důkaz)
ORTHO_BUFFER_M = 30.0  # okolí čáry stahované z ortofota

Point = tuple[float, float]
Predicate = Callable[[np.ndarray, np.ndarray], np.ndarray]  # (x, y) -> bool pole
OrthoFn = Callable[[np.ndarray, np.ndarray], np.ndarray]  # (body Nx2, tečny Nx2) -> bool pole


def zone_predicate(geometry, distance_m: float = CONFIRM_M) -> Predicate | None:
    """Predikát „bod je do ``distance_m`` od geometrie“; None pro prázdnou geometrii."""
    import shapely

    if geometry is None or geometry.is_empty:
        return None
    zone = geometry.buffer(distance_m)
    shapely.prepare(zone)
    return lambda xs, ys: shapely.contains_xy(zone, xs, ys)


def _close_gaps(flags: np.ndarray, max_gap: int) -> np.ndarray:
    """False úseky kratší než ``max_gap`` vzorků mezi dvěma True se přepnou na True."""
    out = flags.copy()
    n = len(out)
    i = 0
    while i < n:
        if out[i]:
            i += 1
            continue
        j = i
        while j < n and not out[j]:
            j += 1
        if i > 0 and j < n and (j - i) <= max_gap:
            out[i:j] = True
        i = j
    return out


def _runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """Souvislé True úseky jako ``(první, poslední)`` index včetně."""
    out: list[tuple[int, int]] = []
    i, n = 0, len(flags)
    while i < n:
        if not flags[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and flags[j + 1]:
            j += 1
        out.append((i, j))
        i = j + 1
    return out


def confirm_line(
    pts: list[Point],
    in_dtm: Predicate | None,
    ortho_paved: OrthoFn | None = None,
) -> tuple[list[list[Point]], dict[str, float]]:
    """Potvrzené části čáry ``pts`` (S-JTSK) a statistika v metrech ``{"dtm", "ortho", "dropped"}``.

    ``in_dtm``: bod do ``CONFIRM_M`` od plochy silnice / osy z DTM (None = DTM v místě není).
    ``ortho_paved``: pro vzorky, které DTM nepotvrdilo, říká, zda je v pásu kolem osy zpevněný povrch
    (None = bez ortofota). Vzorek potvrzený oknem ortofota (``ORTHO_SHARE`` vzorků v ``ORTHO_WINDOW_M``) se bere stejně."""
    from shapely.geometry import LineString
    from shapely.ops import substring

    stats = {"dtm": 0.0, "ortho": 0.0, "dropped": 0.0}
    if len(pts) < 2:
        return [], stats
    line = LineString(pts)
    length = float(line.length)
    if length <= 0:
        return [], stats
    dists = np.append(np.arange(0.0, length, SAMPLE_M), length)
    if len(dists) < 2:
        return [], stats
    xy = np.array([line.interpolate(float(d)).coords[0][:2] for d in dists], dtype=float)
    max_gap = int(GAP_M // SAMPLE_M)
    min_run = max(1, int(np.ceil(MIN_RUN_M / SAMPLE_M)) + 1)

    dtm = np.zeros(len(dists), dtype=bool)
    if in_dtm is not None:
        dtm = np.asarray(in_dtm(xy[:, 0], xy[:, 1]), dtype=bool)
    dtm = _close_gaps(dtm, max_gap)

    ortho = np.zeros(len(dists), dtype=bool)
    if ortho_paved is not None and not dtm.all():
        tang = np.gradient(xy, axis=0)
        norm = np.linalg.norm(tang, axis=1)
        norm[norm == 0] = 1.0
        looks = np.asarray(ortho_paved(xy, tang / norm[:, None]), dtype=float)
        win = max(1, int(round(ORTHO_WINDOW_M / SAMPLE_M)) + 1)
        # Podíl v okně kolem vzorku; u okrajů a čar kratších než okno jen ze skutečných vzorků
        # (np.convolve „same“ vrací u čáry kratší než okno delší pole – pád jobu Zvoles 3.30.0).
        n = len(looks)
        idx = np.arange(n)
        lo = np.clip(idx - win // 2, 0, n)
        hi = np.clip(idx + (win - 1) // 2 + 1, 0, n)
        csum = np.concatenate([[0.0], np.cumsum(looks)])
        ortho = (csum[hi] - csum[lo]) / np.maximum(hi - lo, 1) >= ORTHO_SHARE

    confirmed = _close_gaps(dtm | ortho, max_gap)
    keep = np.zeros(len(dists), dtype=bool)
    pieces: list[list[Point]] = []
    for a, b in _runs(confirmed):
        if b - a + 1 < min_run:
            continue
        keep[a : b + 1] = True
        piece = substring(line, float(dists[a]), float(dists[b]))
        if piece.geom_type == "LineString" and len(piece.coords) >= 2:
            pieces.append([(float(c[0]), float(c[1])) for c in piece.coords])
    step = length / max(1, len(dists) - 1)
    stats["dtm"] = float((keep & dtm).sum() * step)
    stats["ortho"] = float((keep & ~dtm).sum() * step)
    stats["dropped"] = float((~keep).sum() * step)
    return pieces, stats


def ortho_paved_fn(rgb: np.ndarray, gt) -> OrthoFn:
    """Predikát nad ortofotem: v pásu ±``ORTHO_STRIP_M`` od vzorku je aspoň ``ORTHO_STRIP_SHARE`` pixelů
    nezelených, málo sytých a ne tmavých. ``gt`` je GDAL geotransform."""
    from app.pipeline.orthophoto import green_mask, saturation

    offsets = np.arange(-ORTHO_STRIP_M, ORTHO_STRIP_M + 1e-9, ORTHO_STEP_M)
    height, width = rgb.shape[:2]

    def _fn(points: np.ndarray, tangents: np.ndarray) -> np.ndarray:
        normals = np.stack([-tangents[:, 1], tangents[:, 0]], axis=1)
        px = points[:, None, 0] + normals[:, None, 0] * offsets[None, :]
        py = points[:, None, 1] + normals[:, None, 1] * offsets[None, :]
        col = np.floor((px - gt[0]) / gt[1]).astype(int)
        row = np.floor((py - gt[3]) / gt[5]).astype(int)
        inside = (col >= 0) & (col < width) & (row >= 0) & (row < height)
        col = np.clip(col, 0, width - 1)
        row = np.clip(row, 0, height - 1)
        sample = rgb[row, col]  # N x K x 3
        flat = sample.reshape(-1, 3)
        ok = (~green_mask(flat)) & (saturation(flat) < ORTHO_MAX_SAT) & (flat.max(axis=-1) >= ORTHO_MIN_VALUE)
        ok = ok.reshape(inside.shape) & inside
        return ok.mean(axis=1) >= ORTHO_STRIP_SHARE

    return _fn


def load_dtm_zone_predicate(work_dir: Path) -> Predicate | None:
    """Predikát „do ``CONFIRM_M`` od plochy silnice (``dtm/roads.json``) nebo osy komunikace (``dtm/road_axes.json``)“."""
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union

    from app.pipeline import dtm_import

    geoms = []
    try:
        for poly in dtm_import._load_road_polygons(work_dir):
            if poly and len(poly[0]) >= 3:
                g = Polygon(poly[0], poly[1:]).buffer(0)
                if not g.is_empty:
                    geoms.append(g)
    except Exception as exc:  # plochy jsou jen jeden z důkazů
        logger.warning("SilniceNeevidovana: plochy silnic z DTM nelze načíst (%s)", exc)
    try:
        data = json.loads((dtm_import.dtm_dir(work_dir) / AXES_NAME).read_text(encoding="utf-8"))
        for coords in data.get("lines") or []:
            if len(coords) >= 2:
                geoms.append(LineString([(float(x), float(y)) for x, y in coords]))
    except (OSError, ValueError, TypeError):
        pass  # starší joby bez osy
    return zone_predicate(unary_union(geoms)) if geoms else None


class UnregisteredRoadConfirmer:
    """Potvrzení čar vrstvy ``SilniceNeevidovana`` pro job: DTM zóna se načte jednou, ortofoto líně a jen při potřebě."""

    def __init__(self, work_dir: Path, log=None, bounds=None) -> None:
        self.work_dir = Path(work_dir)
        self.log = log
        # Výřez mapy (S-JTSK); ortofoto se načte jednou pro celý výřez, ne po čarách.
        self.bounds = tuple(float(v) for v in bounds) if bounds is not None else None
        self._in_dtm: Predicate | None = None
        self._dtm_loaded = False
        self._ortho: tuple[tuple[float, float, float, float], OrthoFn] | None = None  # (obálka, funkce)
        self._ortho_tried = False
        self.totals = {"dtm": 0.0, "ortho": 0.0, "dropped": 0.0}

    def _dtm(self) -> Predicate | None:
        if not self._dtm_loaded:
            self._in_dtm = load_dtm_zone_predicate(self.work_dir)
            self._dtm_loaded = True
        return self._in_dtm

    def _shared_cache_bounds(self) -> tuple[float, float, float, float] | None:
        """Obálka předstaženého ortofota jobu (``work/orthophoto``), nebo None."""
        from app.pipeline.orthophoto import CACHE_SUBDIR, ORTHO_META_NAME

        try:
            meta = json.loads((self.work_dir / CACHE_SUBDIR / ORTHO_META_NAME).read_text(encoding="utf-8"))
            b = [float(v) for v in meta["bounds"]]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return (b[0], b[1], b[2], b[3]) if len(b) == 4 else None

    def _ortho_fn(self, pts: list[Point]) -> OrthoFn | None:
        """Ortofoto jednou pro celý výřez mapy (bez výřezu: obálka první čáry + ``ORTHO_BUFFER_M``).

        Z předstaženého ortofota jobu se jen vyřízne; jinak se stáhne do vlastní složky (pid procesu),
        sdílenou cache nepřepíše a souběžné skládání les / MTBO si nečte rozepsaný soubor."""
        if self._ortho is not None:
            env = self._ortho[0]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            if env[0] <= min(xs) and env[1] <= min(ys) and env[2] >= max(xs) and env[3] >= max(ys):
                return self._ortho[1]
            if self.bounds is not None:
                return self._ortho[1]  # čára přesahuje výřez mapy: mimo něj vzorky nepotvrdí (``inside``)
        if self._ortho_tried and self.bounds is not None:
            return None
        self._ortho_tried = True
        try:
            import os

            from app.pipeline.orthophoto import fetch_orthophoto
            from app.pipeline.street_parcels import ORTHO_MPP

            if self.bounds is not None:
                need = self.bounds
            else:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                need = (min(xs) - ORTHO_BUFFER_M, min(ys) - ORTHO_BUFFER_M, max(xs) + ORTHO_BUFFER_M, max(ys) + ORTHO_BUFFER_M)
            shared = self._shared_cache_bounds()
            subdir = f"orthophoto_unreg_{os.getpid()}"
            if shared is not None:
                # Les / MTBO: předstažené ortofoto výřezu jobu; vektorový výřez mapy bývá větší (okraj kolem
                # mapy), mimo předstažení se ortofotem nepotvrzuje a nic se nestahuje ani nepřepisuje.
                cut = (max(need[0], shared[0]), max(need[1], shared[1]), min(need[2], shared[2]), min(need[3], shared[3]))
                if cut[0] < cut[2] and cut[1] < cut[3]:
                    need, subdir = cut, None
            got = fetch_orthophoto(self.work_dir, need, ORTHO_MPP, log=self.log, need_bounds=need, subdir=subdir)
            if got is None:
                return None
            rgb, gt = got
            fn = ortho_paved_fn(rgb, gt)
            h, w = rgb.shape[:2]
            env = (gt[0], gt[3] + gt[5] * h, gt[0] + gt[1] * w, gt[3])
            self._ortho = (env, fn)
            return fn
        except Exception as exc:  # bez ortofota rozhoduje jen DTM
            logger.warning("SilniceNeevidovana: ortofoto nelze použít (%s)", exc)
            return None

    def confirm(self, line_parts: list[list[Point]]) -> list[list[Point]]:
        out: list[list[Point]] = []
        in_dtm = self._dtm()
        for pts in line_parts:
            if len(pts) < 2:
                continue
            try:
                pieces, st = confirm_line(pts, in_dtm, self._lazy_ortho(pts, in_dtm))
            except Exception as exc:  # nepotvrzená čára se nekreslí, mapa kvůli ní nespadne
                logger.warning("SilniceNeevidovana: čáru nelze ověřit, vynechána (%s)", exc)
                continue
            out.extend(pieces)
            for k, v in st.items():
                self.totals[k] += v
        return out

    def _lazy_ortho(self, pts: list[Point], in_dtm: Predicate | None) -> OrthoFn | None:
        """Ortofoto jen když DTM nepotvrdí celou čáru (jinak by se stahovalo zbytečně)."""
        if in_dtm is not None:
            from shapely.geometry import LineString

            line = LineString(pts)
            n = max(2, int(line.length // SAMPLE_M) + 1)
            xy = np.array([line.interpolate(i / (n - 1), normalized=True).coords[0][:2] for i in range(n)])
            if bool(np.all(in_dtm(xy[:, 0], xy[:, 1]))):
                return None
        return self._ortho_fn(pts)

    def summary(self) -> str:
        t = self.totals
        return (
            f"ZABAGED Silnice neevidovaná: {t['dtm'] + t['ortho']:.0f} m nakresleno, {t['dropped']:.0f} m bez potvrzení "
            f"(DTM {t['dtm']:.0f} m, ortofoto {t['ortho']:.0f} m)"
        )
