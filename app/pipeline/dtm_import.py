"""DTM kraje v pipeline: ploty z ZPS_KR (sprint) – příprava na pozadí a objekty do .omap.

1. ``start_dtm_prefetch`` (vlákno hned na začátku jobu): najde balíky pro výřez
   (``dtm_sources``), stáhne je do cache, načte ploty (``dtm_fences``), napojí je
   a uloží do ``work/dtm/fences.json``.
2. ``build_dtm_fence_parts`` (sestavení mapy): objekty ploty 518 / živý plot 416.
3. ``subtract_dtm_from_osm_fences``: OSM ploty se ořežou tam, kde už je zaměřený
   plot z DTM (ZABAGED / OSM jsou méně přesné; priorita DTM).

Chyba (výpadek DMVS, oblast bez DTM) nikdy neshodí job – jen se zaloguje a mapa
zůstane z OSM.
"""

from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from app.pipeline import dtm_fences, dtm_sources
from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514
from app.pipeline.geom_clip import Bounds, clip_polyline
from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import OomObjectPart
from app.pipeline.oom_symbol_map import symbol_index_for_code

FENCES_NAME = "fences.json"
# OSM plot do této vzdálenosti (m) od plotu z DTM se bere jako duplicitní.
OSM_DEDUP_BUFFER_M = 3.0
# Zbytek OSM plotu po ořezu kratší než toto (m) se zahodí (střepy).
OSM_MIN_REMAINDER_M = 2.0


def fences_path(work_dir: Path) -> Path:
    return work_dir / "dtm" / FENCES_NAME


class DtmPrefetch:
    """Příprava dat DTM na pozadí; ``result()`` počká a vrátí cestu k ``fences.json`` (nebo None)."""

    def __init__(self, future: Future, executor: ThreadPoolExecutor) -> None:
        self.future = future
        self._executor = executor

    def result(self) -> Path | None:
        try:
            return self.future.result()
        finally:
            self._executor.shutdown(wait=False)


def prepare_dtm_fences(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log=None,
    force: bool = False,
) -> Path | None:
    """Ploty z DTM pro výřez → ``work_dir/dtm/fences.json``; None, když data nejsou."""
    west, south, east, north = bbox_wgs84
    packages = dtm_sources.find_packages_for_bbox(
        bbox_wgs84, sets=(dtm_sources.SET_KR, dtm_sources.SET_OPL), force=force, log=log
    )
    if not packages:
        if log:
            log("DTM: pro výřez nejsou datové sady (ploty zůstanou z OSM)")
        return None
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    # Stažení více balíků souběžně (I/O); parsování a cache JSON pak postupně.
    files = dtm_sources.download_packages(packages, log=log)
    kr_packages = [p for p in packages if p.set_name == dtm_sources.SET_KR]
    if not kr_packages:
        return None
    all_fences: list[dtm_fences.Fence] = []
    for pkg in kr_packages:
        all_fences.extend(
            dtm_fences.fences_in_bounds(dtm_fences.load_fences(files[pkg.filename]), bounds)
        )
    oriented = False
    dubious = 0
    areas: dict[str, list] = {"olive": [], "public": [], "building": [], "open": []}
    try:
        for pkg in (p for p in packages if p.set_name == dtm_sources.SET_OPL):
            part = dtm_fences.load_orientation_areas(files[pkg.filename])
            for key in areas:
                areas[key].extend(part.get(key, []))
    except Exception as exc:  # plochy jsou jen doplněk, ploty se kreslí i bez nich
        if log:
            log(f"DTM: plochy z OPL nelze načíst ({type(exc).__name__}: {exc})")
    if areas["olive"] or areas["building"]:
        # Podezřelé „ploty“ (stará migrace s neznámým původem, jen hranice jiného objektu).
        all_fences, removed = dtm_fences.filter_dubious_fences(all_fences, areas)
        dubious = len(removed)
        if removed and log:
            ids = ", ".join(f.oid for f in removed[:3] if f.oid)
            log(f"DTM ploty: vynecháno {dubious} podezřelých úseků (neznámý původ, bez soukromé hranice; např. {ids})")
    merged = dtm_fences.merged_fences(all_fences)
    try:
        if areas["olive"] or areas["public"]:
            merged = dtm_fences.orient_fences(merged, areas, bounds=bounds)
            oriented = True
    except Exception as exc:  # orientace je jen kosmetika
        if log:
            log(f"DTM: orientace plotů přeskočena ({type(exc).__name__}: {exc})")
    dest = fences_path(work_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(
            {
                "packages": [p.filename for p in packages],
                "source_pieces": len(all_fences),
                "oriented": oriented,
                "dubious_removed": dubious,
                "fences": [{"code": code, "coords": [list(p) for p in line]} for code, line in merged],
            }
        ),
        encoding="utf-8",
    )
    if log:
        log(f"DTM ploty: {len(all_fences)} úseků → {len(merged)} napojených čar ({len(packages)} balíků)")
    return dest


def start_dtm_prefetch(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log=None,
    force: bool = False,
) -> DtmPrefetch:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dtm-dl")

    def _run() -> Path | None:
        try:
            return prepare_dtm_fences(work_dir, bbox_wgs84, log=log, force=force)
        except Exception as exc:  # výpadek DMVS / poškozený balík nesmí shodit job
            if log:
                log(f"DTM: ploty přeskočeny ({type(exc).__name__}: {exc})")
            return None

    if log:
        log("Start přípravy DTM (ploty) na pozadí.")
    return DtmPrefetch(executor.submit(_run), executor)


def load_fence_lines(work_dir: Path) -> list[tuple[str, list[tuple[float, float]]]]:
    path = fences_path(work_dir)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for f in data.get("fences") or []:
        coords = [(float(x), float(y)) for x, y in f.get("coords") or []]
        if len(coords) >= 2:
            out.append((str(f.get("code") or dtm_fences.CODE_FENCE), coords))
    return out


def build_dtm_fence_parts(
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    clip_bounds: Bounds | None = None,
) -> list[OomObjectPart]:
    """Objekty plotů z DTM (jen sprint)."""
    from app.pipeline.osm_paths import _path_object

    if not preset_id.startswith("sprint"):
        return []
    lines = load_fence_lines(work_dir)
    if not lines:
        return []
    symbol_cache: dict[str, int | None] = {}
    objects: list[str] = []
    for code, line in lines:
        if code not in symbol_cache:
            symbol_cache[code] = symbol_index_for_code(preset_id, scale, code)
        idx = symbol_cache[code]
        if idx is None:
            continue
        for piece in clip_polyline(line, clip_bounds) if clip_bounds else [line]:
            mapped = [
                projected_to_map_coord(
                    x, y, ref_x=ref_x, ref_y=ref_y, scale=scale, grivation_deg=grivation_deg
                )
                for x, y in piece
            ]
            obj = _path_object(idx, mapped)
            if obj:
                objects.append(obj)
    if not objects:
        return []
    return [
        OomObjectPart(name="DTM – ploty", objects_xml="\n".join(objects), count=len(objects))
    ]


def dtm_fence_buffer(work_dir: Path, buffer_m: float = OSM_DEDUP_BUFFER_M):
    """Sjednocený pás kolem plotů z DTM (Shapely), nebo None."""
    lines = load_fence_lines(work_dir)
    if not lines:
        return None
    try:
        from shapely.geometry import LineString
        from shapely.ops import unary_union
    except ImportError:
        return None
    return unary_union([LineString(line).buffer(buffer_m) for _code, line in lines])


def subtract_dtm_from_osm_fences(feats: list[dict], buffer) -> list[dict]:
    """OSM ploty (``kind == "fence"``) bez částí, které leží u plotu z DTM."""
    if buffer is None:
        return feats
    from shapely.geometry import LineString

    out: list[dict] = []
    for feat in feats:
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        if props.get("kind") != "fence" or geom.get("type") != "LineString":
            out.append(feat)
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            out.append(feat)
            continue
        line = LineString(coords)
        rest = line.difference(buffer)
        if rest.is_empty:
            continue
        if rest.equals(line):
            out.append(feat)
            continue
        pieces = list(rest.geoms) if hasattr(rest, "geoms") else [rest]
        for piece in pieces:
            if piece.geom_type != "LineString" or piece.length < OSM_MIN_REMAINDER_M:
                continue
            out.append({**feat, "geometry": {"type": "LineString", "coordinates": [list(c) for c in piece.coords]}})
    return out
