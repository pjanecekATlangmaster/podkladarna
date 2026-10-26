"""Sprint: úzká a stejně široká zpevněná plocha podél OSM cesty se kreslí čarou 501.xxx, ne plochou.

Plocha chodníku / cesty z DTM, která je po celé délce úseku nejvýš tak široká jako kresba čáry
cesty + ``NARROW_TOL_MM`` mapy, je v měřítku sprintu k nerozeznání od čáry. Kreslená jako plocha
vedle čáry jen dělá zuby a dvojí kresbu (cyklostezka way/156838225 s klínem DTM chodníku vedle osy).
V takovém úseku se plocha vynechá a kreslí ho čára OSM; čára končí na okraji navazující plochy.
"""

from __future__ import annotations

import statistics
from pathlib import Path

# Tolerance šířky plochy nad šířku kresby čáry a rozptylu šířek (P90 − P10) v mm mapy (1 m v 1:4000).
NARROW_TOL_MM = 0.25
# Krok kolmých řezů podél čáry (m) a nejkratší posuzovaný úsek (m).
STEP_M = 2.0
MIN_RUN_M = 10.0
# Dosah řezu za okraj kresby čáry (m): plocha těsně vedle čáry (klín) se počítá do šířky.
REACH_M = 2.5
# Druhy čar (po ``path_draw_highway``), které ve sprintu kreslí symbol 501.xxx.
NARROW_BASES = frozenset({"sidewalk", "cycleway_paved"})
# Kousky plochy menší než tolik m² po vyříznutí pásu se zahodí.
MIN_REST_M2 = 1.0

_CACHE: dict[tuple, object] = {}


def _areas(work_dir: Path, path_source: str):
    """``(chodníky + silnice, jen chodníky)`` z DTM (shapely nebo None)."""
    from app.pipeline import dtm_import, dtm_sidewalks

    sidewalks, roads = dtm_import.classified_polygons(work_dir, path_source)
    return dtm_sidewalks.dtm_union(sidewalks + roads), dtm_sidewalks.dtm_union(sidewalks)


def _area(work_dir: Path, path_source: str):
    return _areas(work_dir, path_source)[0]


def narrow_runs(line, area, width_m: float, tol_m: float) -> list[tuple[float, float]]:
    """Úseky ``(od, do)`` v m podél ``line``, kde je plocha ``area`` úzká jako čára (viz modul)."""
    from shapely.geometry import LineString

    if line.length < MIN_RUN_M:
        return []
    reach = width_m / 2.0 + REACH_M
    n = max(1, int(line.length // STEP_M))
    stations: list[tuple[float, float]] = []  # (vzdálenost, šířka plochy v řezu)
    for k in range(n):
        d = (k + 0.5) * line.length / n
        a = line.interpolate(max(0.0, d - 0.5))
        b = line.interpolate(min(line.length, d + 0.5))
        tx, ty = b.x - a.x, b.y - a.y
        norm = (tx * tx + ty * ty) ** 0.5 or 1.0
        nx, ny = -ty / norm, tx / norm
        p = line.interpolate(d)
        cut = LineString([(p.x - nx * reach, p.y - ny * reach), (p.x + nx * reach, p.y + ny * reach)])
        stations.append((d, cut.intersection(area).length))
    half = line.length / n / 2.0
    runs: list[tuple[float, float]] = []
    run: list[tuple[float, float]] = []
    for st in stations + [(line.length + half, -1.0)]:  # zarážka uzavře poslední úsek
        if 0.05 < st[1] <= width_m + tol_m:
            run.append(st)
            continue
        if run:
            d0, d1 = run[0][0] - half, run[-1][0] + half
            widths = sorted(w for _d, w in run)
            p10 = widths[int(0.1 * (len(widths) - 1))]
            p90 = widths[int(round(0.9 * (len(widths) - 1)))]
            # Stejně široká (rozptyl do tolerance), nebo celá užší než kresba čáry (klín vedle osy).
            uniform = p90 - p10 <= tol_m or p90 <= width_m
            if d1 - d0 >= MIN_RUN_M and uniform and statistics.median(widths) <= width_m + tol_m:
                runs.append((max(0.0, d0), min(line.length, d1)))
            run = []
    return runs


def narrow_line_zone(work_dir: Path, path_source: str, preset_id: str, scale: int, *, draw: bool = False):
    """Část plochy chodníků, která se nahradí čárou 501.xxx (shapely), nebo None (jen sprint).

    ``draw``: místo toho celý pás kolem úseků čar, ve kterém se čára kreslí.
    """
    from app.pipeline import dtm_import

    if not preset_id.startswith("sprint") or dtm_import.dtm_is_disabled():
        return None
    return _zones(work_dir, path_source, preset_id, scale)[1 if draw else 0]


def _zones(work_dir: Path, path_source: str, preset_id: str, scale: int):
    from app.pipeline import dtm_import
    from app.pipeline import osm_paths as op

    stamps = tuple(
        f.stat().st_mtime_ns if f.is_file() else 0
        for f in (dtm_import.roads_path(work_dir), dtm_import.sidewalks_path(work_dir), work_dir / "osm_paths" / "paths_osm.geojson")
    )
    key = (str(work_dir), path_source, preset_id, scale, stamps)
    if key in _CACHE:
        return _CACHE[key]
    if len(_CACHE) > 32:
        _CACHE.clear()
    zone = band = None
    # Šířka se měří na chodnících i silnicích (chodník u silnice je široký), vynechávají se jen chodníky.
    area, sidewalks = _areas(work_dir, path_source)
    if area is not None and not area.is_empty and sidewalks is not None and not sidewalks.is_empty:
        from shapely.geometry import LineString
        from shapely.ops import substring, unary_union

        tol_m = op.mm_to_ground_m(NARROW_TOL_MM, scale)
        parts = []
        for feat in dtm_import._path_features(work_dir, path_source):
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or []
            hw = str((feat.get("properties") or {}).get("highway") or "path")
            if geom.get("type") != "LineString" or len(coords) < 2:
                continue
            if op.path_draw_highway(hw) not in NARROW_BASES or op.is_bridge_highway(hw) or op.is_tunnel_highway(hw):
                continue
            line = LineString([(float(c[0]), float(c[1])) for c in coords])
            if not line.intersects(area):
                continue
            width = op.drawn_symbol_width_m(hw, preset_id, scale)
            for d0, d1 in narrow_runs(line, area, width, tol_m):
                piece = substring(line, d0, d1)
                if piece.length > 0:
                    parts.append(piece.buffer(width / 2.0 + REACH_M, cap_style=2))
        if parts:
            band = unary_union(parts)
            zone = band.intersection(sidewalks.buffer(0.1))
            zone = None if zone.is_empty else zone
    _CACHE[key] = (zone, band)
    return zone, band


def minus_zone(geom, zone):
    """``geom`` bez pásu ``zone`` (obojí shapely; None = beze změny)."""
    if geom is None or zone is None or geom.is_empty:
        return geom
    rest = geom.difference(zone)
    if rest.geom_type in {"Polygon", "MultiPolygon"}:
        from shapely.ops import unary_union

        # zbytky na hraně pásu (souběžné hrany, plovoucí čárka) se nekreslí
        polys = [rest] if rest.geom_type == "Polygon" else list(rest.geoms)
        rest = unary_union([p for p in polys if p.area >= MIN_REST_M2])
    return None if rest.is_empty else rest
