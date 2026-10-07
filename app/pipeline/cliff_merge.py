"""Spojí srázové čárky (~3 m tick) do linií, plošné skalní masy do polygonů.

Vstup (KP i DEM) jsou krátké úsečky kolmo na spád. Souosé sousední čárky se
řetězí na lomenou čáru (201/104).

U skal (``as_polygons``): plošná masa přes buffer+dissolve ticků, morfologické
otevření a vyhlazení → polygony (OOM 201.2 / 206). Samostatné / zbytkové
linie 201 se vždy zahazují (i dlouhé stěny) — na mapě zůstanou jen spojené
skalní plochy.

Zemní srázy (104): zamotané / smyčkové / krátké linie raději nekreslit
(``reject_tangled`` + min. délka ~50 m @ 10k). Kratší 104 podél delšího
(104×104) zahodit. Překryv skála×sráz → vždy sráz (104), skálu 201.2/206
zahodit. Skála přes budovu / cestu / vodu / jiné mapové objekty (kromě
zeleně/bílé/vrstevnic jako objektového překryvu) → zahodit skálu. Hustý
shluk vrstevnic (strmý svah) → zahodit jen 104 (skály ne – dense-contour
by systematicky mazal 201.2/206 na detekovaných stěnách).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

# Mezera středů sousedních KP čárek – trochu volnější než buňka 3 m, ať se
# sousední úsečky slepí do jedné dlouhé stěny místo mraku krátkých ticků.
JOIN_GAP_M = 6.0
# Max. odchylka směru čárky (nesměrové).
JOIN_ANGLE_COS = math.cos(math.radians(40))
# Spojnice středů musí jít zhruba podél stěny, ne kolmo na sousední sráz.
CHAIN_DIR_COS = 0.35
SIMPLIFY_M = 0.55
# KP umí do jedné buňky 4,4 m nasypat i 1800 čárek přes sebe. Zahodit ty, co
# se liší o < 1 m a < 8°, nic viditelného nestojí (1 m = 0,1 mm na 1:10 000).
DEDUP_POS_PER_M = 1.0
DEDUP_ANG_PER_RAD = 8.0

# Plošná skála: buffer ticků → uzavření mezer → otevření tenkých stěn →
# stažení k mase → vyhlazení. 1.25.4 (open 3.9 / shrink 2.0 / aspect 3.4 /
# šířka-gate 14) na hřebenech (Sachrův) sežralo 457 ticků → 0×201.2 — morph
# nechal protáhlé stěny skal a aspect je všechny shodil. ≥2.2.18: mírně
# měkčí open/shrink + aspect jen u úzkých pásů (<~9 m); stěna/dvojstěna
# zůstane mimo, scarp-wins / occupancy / dense-contour beze změny.
# Min-size cleanup (≥2.2.17): **zrušen** (veg + skály/kameny + path stubs).
# Jediná výjimka min-size: min. délka srázů **104** (~50 m @ 1:10k).
ROCK_BUFFER_M = 2.6
ROCK_CLOSE_M = 2.0
ROCK_OPEN_M = 3.2
ROCK_SHRINK_M = 1.5
ROCK_SMOOTH_M = 1.0
ROCK_SIMPLIFY_M = 2.0
# Jen numerický/degenerovaný úlomek po morph — ne min-size cleanup.
ROCK_DEGENERATE_AREA_M2 = 1.0
# Aspect filtr: jen úzké protáhlé zbytky stěn (ne hřebenová skalní stěna
# ~10+ m hluboká). Širší masa projde i při vyšším aspectu.
MAX_ROCK_ASPECT = 5.0
MAX_ROCK_ASPECT_WIDTH_M = 9.0
# Halo kolem plochy: čárky na okraji už nekreslit jako 201 (obrys nese plocha).
ROCK_TICK_HALO_M = 3.5
# Po řetězení: kompaktní zbytky (ne protáhlá stěna) → plocha místo mraku 201.
# (Promotion práh; ne min-size delete existujících skalních ploch.)
COMPACT_LINE_BUFFER_M = 2.4
COMPACT_MAX_ASPECT = 2.7
COMPACT_MIN_WIDTH_M = 6.5
COMPACT_MIN_AREA_M2 = 42.0

# Legacy rastr (fallback bez shapely + testy obtahu buněk).
ROCK_CELL_M = 3.0
ROCK_MIN_TICKS_PER_CELL = 2
ROCK_MIN_CORE_CELLS = 2

# Min. délka na mapě: skála ~1,2 mm, zem ~5,0 mm (Petr 2026-10-03:
# krátké 104 pryč; min ~50 m @ 1:10 000). 1:10 000 → skála 12 m, zem 50 m;
# 1:4000 → 4,8 m / 20 m. Dřív zem 3,5 mm / 35 m.
MIN_LINE_MM_ROCK = 1.2
MIN_LINE_MM_EARTH = 5.0

# Zemní sráz: po hrubém zjednodušení path/chord a zatáčky – nad tím raději nekreslit.
# (Surový řetěz DEM ticků je zubatý i u rovné stěny, proto nejdřív simplify.)
# Petr: zamotané / nejasné 104 pryč (včetně ZABAGED StupenSraz, i >70 m).
# 2.0: přísnější křivost – křivé ~45 m srázy a překryvy už neprojdou.
BANK_SHAPE_SIMPLIFY_M = 3.5
MAX_BANK_SINUOSITY = 1.45
MAX_BANK_TURN_DEG = 95.0

# 104×104: kratší sráz, který leží podél delšího (buffer), zahodit.
EARTH_OVERLAP_BUFFER_M = 4.0
EARTH_OVERLAP_COVER_FRAC = 0.55

# Střednice cesty/toku/zdi → buffer, ať plocha skály „na“ objektu koliduje.
ROCK_OCCUPANCY_LINE_BUFFER_M = 2.5

# Husté vrstevnice (strmý svah): spacing = interval / |grad|. Pod prahem
# potlačit skálu i 104. 1.25.0 (0.8× / 45 %) na Barr skoro nestřílelo – často
# jen dem_smooth; 1.25.4: nehlazený dem/ + mírně volnější práh.
DENSE_CONTOUR_SPACING_FRAC = 1.0  # spacing < 1.0×ekvidistance = „nahuštěné“
DENSE_CONTOUR_MIN_SAMPLE_FRAC = 0.30
DENSE_CONTOUR_GRADE_STEP_M = 2.0
DENSE_CONTOUR_SAMPLE_STEP_M = 4.0


_Tick = tuple[tuple[float, float], tuple[float, float]]
_Cell = tuple[int, int]


@dataclass(frozen=True)
class MergedCliffs:
    lines: list[list[tuple[float, float]]]
    polygons: list[list[tuple[float, float]]]


def filter_short_earth_banks(
    polylines: list[list[tuple[float, float]]],
    *,
    scale: int,
) -> list[list[tuple[float, float]]]:
    """Zahodí zemní srázy (104) kratší než DEM práh (~50 m @ 1:10 000).

    Stejný min-length jako ``merge_cliff_ticks(..., earth=True)`` – včetně
    ZABAGED ``StupenSraz``.
    """
    min_m = min_line_length_m(scale, earth=True)
    if min_m <= 0:
        return list(polylines)
    return [pts for pts in polylines if _polyline_length(pts) >= min_m]


def filter_tangled_earth_banks(
    polylines: list[list[tuple[float, float]]],
) -> list[list[tuple[float, float]]]:
    """Zahodí zamotané / smyčkové zemní srázy (stejná metrika jako DEM 104)."""
    return [pts for pts in polylines if polyline_is_simple_bank(pts)]


def filter_overlapping_earth_banks(
    polylines: list[list[tuple[float, float]]],
    *,
    buffer_m: float = EARTH_OVERLAP_BUFFER_M,
    cover_frac: float = EARTH_OVERLAP_COVER_FRAC,
) -> tuple[list[list[tuple[float, float]]], int]:
    """Zahodí kratší 104, které leží podél delšího srázu (104×104).

    Petr: menší srázek přes / podél většího → kreslit jen delší. Frakce délky
    kratší linie uvnitř bufferu delší ≥ ``cover_frac`` → drop.
    """
    if len(polylines) < 2:
        return list(polylines), 0
    try:
        from shapely.geometry import LineString
    except ImportError:
        return list(polylines), 0

    ranked = sorted(
        (
            (i, pts, _polyline_length(pts))
            for i, pts in enumerate(polylines)
            if len(pts) >= 2
        ),
        key=lambda t: (-t[2], t[0]),
    )
    kept_pts: list[list[tuple[float, float]]] = []
    kept_geoms: list[object] = []
    dropped = 0
    buf = max(0.5, float(buffer_m))
    frac = min(1.0, max(0.05, float(cover_frac)))
    for _i, pts, length in ranked:
        if length < 1e-3:
            dropped += 1
            continue
        try:
            line = LineString(pts)
            if line.is_empty:
                dropped += 1
                continue
        except Exception:
            kept_pts.append(pts)
            continue
        redundant = False
        for longer in kept_geoms:
            try:
                covered = line.intersection(longer.buffer(buf)).length
                if covered / length >= frac:
                    redundant = True
                    break
            except Exception:
                continue
        if redundant:
            dropped += 1
            continue
        kept_pts.append(pts)
        kept_geoms.append(line)
    return kept_pts, dropped


def filter_earth_bank_lines(
    polylines: list[list[tuple[float, float]]],
    *,
    scale: int,
) -> list[list[tuple[float, float]]]:
    """Min-délka + zamotané + 104×104 – společný filtr DEM i ZABAGED ``StupenSraz``."""
    after_shape = filter_tangled_earth_banks(
        filter_short_earth_banks(polylines, scale=scale)
    )
    kept, _n = filter_overlapping_earth_banks(after_shape)
    return kept


def min_line_length_m(scale: int, *, earth: bool = False) -> float:
    """Nejkratší sráz, který má na dané měřítko smysl kreslit."""
    mm = MIN_LINE_MM_EARTH if earth else MIN_LINE_MM_ROCK
    return mm * float(scale) / 1000.0


def merge_cliff_ticks(
    ticks: list[_Tick],
    *,
    as_polygons: bool = False,
    min_line_m: float = 0.0,
    reject_tangled: bool = False,
) -> MergedCliffs:
    """Vrátí lomené čáry a volitelně polygony. Vstup: úsečky v S-JTSK metrech."""
    remaining = _dedup_ticks(ticks)
    polygons: list[list[tuple[float, float]]] = []
    if as_polygons:
        remaining, polygons = _rock_area_polygons(remaining)
    if not remaining:
        return MergedCliffs([], polygons)
    chains = _chain_ticks(remaining)
    polylines = [_chain_polyline(remaining, chain) for chain in chains]
    polylines = [
        _simplify_polyline(pts, SIMPLIFY_M) for pts in polylines if len(pts) >= 2
    ]
    if as_polygons and polylines:
        # Husté zbytky, které footprint nechytil (řidší DEM), ale nejsou
        # protáhlá stěna → ještě jedna plocha místo shluku krátkých 201.
        polylines, extra = _promote_compact_polylines(polylines)
        polygons.extend(extra)
        # Petr: pouze spojené skalní plochy; žádná samostatná značka 201.
        polylines = []
    if reject_tangled:
        polylines = [pts for pts in polylines if polyline_is_simple_bank(pts)]
    if min_line_m > 0:
        polylines = [pts for pts in polylines if _polyline_length(pts) >= min_line_m]
    return MergedCliffs(polylines, polygons)


def _promote_compact_polylines(
    polylines: list[list[tuple[float, float]]],
) -> tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]]]:
    """Kompaktní lomené čáry → plocha; protáhlé stěny nechá liniemi."""
    try:
        from shapely.geometry import LineString
    except ImportError:
        return polylines, []

    kept: list[list[tuple[float, float]]] = []
    areas: list[list[tuple[float, float]]] = []
    for pts in polylines:
        if len(pts) < 2:
            continue
        try:
            geom = LineString(pts).buffer(
                COMPACT_LINE_BUFFER_M, join_style=1, cap_style=1
            )
            geom = geom.buffer(1.0, join_style=1).buffer(-1.0, join_style=1)
            if geom.is_empty:
                kept.append(pts)
                continue
            if geom.geom_type == "MultiPolygon":
                geom = max(geom.geoms, key=lambda g: g.area)
            if geom.geom_type != "Polygon" or geom.is_empty:
                kept.append(pts)
                continue
            width, length = _mrr_width_length(geom)
            aspect = length / max(width, 1e-6)
            if (
                geom.area >= COMPACT_MIN_AREA_M2
                and width >= COMPACT_MIN_WIDTH_M
                and aspect <= COMPACT_MAX_ASPECT
            ):
                try:
                    geom = geom.simplify(ROCK_SIMPLIFY_M, preserve_topology=True)
                except Exception:
                    pass
                if geom.is_empty or geom.geom_type != "Polygon":
                    kept.append(pts)
                    continue
                coords = [(float(x), float(y)) for x, y in geom.exterior.coords]
                if len(coords) >= 2 and coords[0] == coords[-1]:
                    coords = coords[:-1]
                if len(coords) >= 3 and _ring_is_simple(coords):
                    areas.append(coords)
                    continue
                hull = _convex_hull_ring(coords)
                if len(hull) >= 3 and _ring_area(hull) >= COMPACT_MIN_AREA_M2:
                    areas.append(hull)
                    continue
            kept.append(pts)
        except Exception:
            kept.append(pts)
    return kept, areas


def polyline_is_simple_bank(pts: list[tuple[float, float]]) -> bool:
    """True = zhruba rovný nebo mírně zatáčející sráz; False = uzel / smyčka.

    Raději nekreslit než zamotanou skupinku 104. Neříká nic o terénní pravdě.
    Hodnotí se hrubě zjednodušená linie – surový řetěz DEM ticků je zubatý i
    u rovné stěny. Petr: když se tvar nedá vyčistit / je nejasný → nekreslit.
    """
    if len(pts) < 2:
        return False
    length = _polyline_length(pts)
    if length < 1e-6:
        return False
    chord = math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1])
    if chord < 1e-3 or chord < 0.25 * length and chord < 8.0:
        return False  # uzavřená / téměř uzavřená smyčka
    if _polyline_self_intersects(pts):
        return False
    shape = _simplify_polyline(pts, BANK_SHAPE_SIMPLIFY_M)
    if len(shape) < 2:
        return False
    # Po simplify zbyl zigzag / mrak vrcholů → tvar nejde vyčistit.
    if len(shape) >= 8 and length > 0 and _polyline_length(shape) / length > 0.85:
        # Simplify skoro nic neodstranil u dlouhé linie → pořád zamotané.
        if _total_turning_deg(shape) > MAX_BANK_TURN_DEG * 0.7:
            return False
    shape_len = _polyline_length(shape)
    shape_chord = math.hypot(shape[-1][0] - shape[0][0], shape[-1][1] - shape[0][1])
    if shape_chord < 1e-3:
        return False
    if shape_len / shape_chord > MAX_BANK_SINUOSITY:
        return False
    if _total_turning_deg(shape) > MAX_BANK_TURN_DEG:
        return False
    return True


def resolve_rock_scarp_overlaps(
    rock_rings: list[list[tuple[float, float]]],
    scarp_lines: list[list[tuple[float, float]]],
) -> tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]], int]:
    """Petr: překryv skála×sráz → vždy sráz (104), skálu 201.2/206 zahodit.

    Varianta „scarp wins“ – konzistentně všude; 104 už je OK, problém jsou skály.
    """
    if not rock_rings or not scarp_lines:
        return rock_rings, scarp_lines, 0
    try:
        from shapely.geometry import LineString, Polygon
    except ImportError:
        return rock_rings, scarp_lines, 0

    rock_geoms: list[tuple[object, int]] = []
    for i, ring in enumerate(rock_rings):
        if len(ring) < 3:
            continue
        try:
            poly = Polygon(ring)
            if poly.is_empty or not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty:
                continue
            rock_geoms.append((poly, i))
        except Exception:
            continue

    scarp_geoms: list[object] = []
    for pts in scarp_lines:
        if len(pts) < 2:
            continue
        try:
            line = LineString(pts)
            if line.is_empty:
                continue
            scarp_geoms.append(line)
        except Exception:
            continue
    if not rock_geoms or not scarp_geoms:
        return rock_rings, scarp_lines, 0

    drop_rocks: set[int] = set()
    for rock_geom, ri in rock_geoms:
        for scarp_geom in scarp_geoms:
            try:
                if rock_geom.intersects(scarp_geom):
                    drop_rocks.add(ri)
                    break
            except Exception:
                continue

    kept_rocks = [r for i, r in enumerate(rock_rings) if i not in drop_rocks]
    dropped = len(rock_rings) - len(kept_rocks)
    return kept_rocks, scarp_lines, dropped


def filter_rocks_overlapping_blockers(
    rock_rings: list[list[tuple[float, float]]],
    blocker_rings: list[list[tuple[float, float]]] | None = None,
    blocker_lines: list[list[tuple[float, float]]] | None = None,
    *,
    line_buffer_m: float = ROCK_OCCUPANCY_LINE_BUFFER_M,
) -> tuple[list[list[tuple[float, float]]], int]:
    """Zahodí skálu, která geometricky koliduje s jiným mapovým objektem.

    Výjimky (zelená / bílá / vrstevnice) se sem vůbec nepředávají – volající
    je do blockerů nezařazuje. Dotyk hranou nestačí (``touches`` OK).
    """
    if not rock_rings:
        return rock_rings, 0
    polys_in = blocker_rings or []
    lines_in = blocker_lines or []
    if not polys_in and not lines_in:
        return rock_rings, 0
    try:
        from shapely.geometry import LineString, Polygon
        from shapely.ops import unary_union
    except ImportError:
        return rock_rings, 0

    parts: list[object] = []
    for ring in polys_in:
        if len(ring) < 3:
            continue
        try:
            poly = Polygon(ring)
            if poly.is_empty:
                continue
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.is_empty:
                parts.append(poly)
        except Exception:
            continue
    buf = max(0.0, float(line_buffer_m))
    for pts in lines_in:
        if len(pts) < 2:
            continue
        try:
            line = LineString(pts)
            if line.is_empty:
                continue
            parts.append(line.buffer(buf) if buf > 0 else line)
        except Exception:
            continue
    if not parts:
        return rock_rings, 0
    try:
        mask = unary_union(parts)
    except Exception:
        return rock_rings, 0

    kept: list[list[tuple[float, float]]] = []
    dropped = 0
    for ring in rock_rings:
        if len(ring) < 3:
            dropped += 1
            continue
        try:
            geom = Polygon(ring)
            if not geom.is_valid:
                geom = geom.buffer(0)
            if geom.is_empty:
                dropped += 1
                continue
            if geom.intersects(mask) and not geom.touches(mask):
                dropped += 1
                continue
        except Exception:
            pass
        kept.append(ring)
    return kept, dropped


def _local_grade(elev_at, x: float, y: float, step_m: float) -> float | None:
    z0 = elev_at(x, y)
    zx = elev_at(x + step_m, y)
    zy = elev_at(x, y + step_m)
    if None in (z0, zx, zy) or step_m <= 0:
        return None
    return math.hypot((zx - z0) / step_m, (zy - z0) / step_m)


def _contour_spacing_m(grade: float | None, interval_m: float) -> float | None:
    if grade is None or grade < 1e-9 or interval_m <= 0:
        return None
    return float(interval_m) / float(grade)


def _sample_spacings_along_line(
    pts: list[tuple[float, float]],
    elev_at,
    *,
    interval_m: float,
    grade_step_m: float,
    sample_step_m: float,
) -> list[float]:
    out: list[float] = []
    if elev_at is None or len(pts) < 2:
        return out
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg < 1e-6:
            continue
        n = max(1, int(seg / max(sample_step_m, 1e-6)))
        for k in range(n + 1):
            t = k / n
            g = _local_grade(
                elev_at, x0 + t * (x1 - x0), y0 + t * (y1 - y0), grade_step_m
            )
            sp = _contour_spacing_m(g, interval_m)
            if sp is not None:
                out.append(sp)
    return out


def _sample_spacings_in_ring(
    ring: list[tuple[float, float]],
    elev_at,
    *,
    interval_m: float,
    grade_step_m: float,
    sample_step_m: float,
) -> list[float]:
    out: list[float] = []
    if elev_at is None or len(ring) < 3:
        return out
    # Rim line first – strmá hrana skály je na obrysu, ne uvnitř plošiny.
    out.extend(
        _sample_spacings_along_line(
            list(ring) + [ring[0]],
            elev_at,
            interval_m=interval_m,
            grade_step_m=grade_step_m,
            sample_step_m=sample_step_m,
        )
    )
    # Sparse interior: 3×3 relative to bbox center + rim vertices subsample.
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    candidates: list[tuple[float, float]] = [(cx, cy)]
    step = max(sample_step_m, 1.0)
    for fx in (0.25, 0.5, 0.75):
        for fy in (0.25, 0.5, 0.75):
            candidates.append((minx + fx * (maxx - minx), miny + fy * (maxy - miny)))
    stride = max(1, len(ring) // 12)
    candidates.extend(ring[::stride])
    for x, y in candidates:
        if x < minx - step or x > maxx + step or y < miny - step or y > maxy + step:
            continue
        g = _local_grade(elev_at, x, y, grade_step_m)
        sp = _contour_spacing_m(g, interval_m)
        if sp is not None:
            out.append(sp)
    return out


def _dense_contour_hit(
    spacings: list[float],
    *,
    interval_m: float,
    spacing_frac: float = DENSE_CONTOUR_SPACING_FRAC,
    min_sample_frac: float = DENSE_CONTOUR_MIN_SAMPLE_FRAC,
) -> bool:
    # Krátký sráz má málo vzorků – i 1–2 husté stačí (jinak 104 v shlucích přežije).
    if len(spacings) < 1 or interval_m <= 0:
        return False
    limit = float(spacing_frac) * float(interval_m)
    if limit <= 0:
        return False
    dense_n = sum(1 for s in spacings if s < limit)
    if len(spacings) < 3:
        return dense_n == len(spacings)
    return (dense_n / len(spacings)) >= float(min_sample_frac)


def geometry_has_dense_contours(
    *,
    elev_at,
    interval_m: float,
    line: list[tuple[float, float]] | None = None,
    ring: list[tuple[float, float]] | None = None,
    spacing_frac: float = DENSE_CONTOUR_SPACING_FRAC,
    min_sample_frac: float = DENSE_CONTOUR_MIN_SAMPLE_FRAC,
    grade_step_m: float = DENSE_CONTOUR_GRADE_STEP_M,
    sample_step_m: float = DENSE_CONTOUR_SAMPLE_STEP_M,
) -> bool:
    """True = lokálně nahuštěné vrstevnice (strmý souvislý svah) → potlačit objekt."""
    if elev_at is None or interval_m <= 0:
        return False
    if line is not None:
        spacings = _sample_spacings_along_line(
            line,
            elev_at,
            interval_m=interval_m,
            grade_step_m=grade_step_m,
            sample_step_m=sample_step_m,
        )
    elif ring is not None:
        spacings = _sample_spacings_in_ring(
            ring,
            elev_at,
            interval_m=interval_m,
            grade_step_m=grade_step_m,
            sample_step_m=sample_step_m,
        )
    else:
        return False
    return _dense_contour_hit(
        spacings,
        interval_m=interval_m,
        spacing_frac=spacing_frac,
        min_sample_frac=min_sample_frac,
    )


def filter_by_dense_contours(
    lines: list[list[tuple[float, float]]],
    polygons: list[list[tuple[float, float]]],
    elev_at,
    *,
    interval_m: float,
) -> tuple[
    list[list[tuple[float, float]]],
    list[list[tuple[float, float]]],
    int,
]:
    """Petr: hustý shluk vrstevnic → zahodit skálu i sráz (104); vrstevnice stačí."""
    if elev_at is None or interval_m <= 0:
        return lines, polygons, 0
    dropped = 0
    kept_lines: list[list[tuple[float, float]]] = []
    for pts in lines:
        if geometry_has_dense_contours(elev_at=elev_at, interval_m=interval_m, line=pts):
            dropped += 1
            continue
        kept_lines.append(pts)
    kept_polys: list[list[tuple[float, float]]] = []
    for ring in polygons:
        if geometry_has_dense_contours(elev_at=elev_at, interval_m=interval_m, ring=ring):
            dropped += 1
            continue
        kept_polys.append(ring)
    return kept_lines, kept_polys, dropped


def filter_cliffs_crossing_buildings(
    lines: list[list[tuple[float, float]]],
    polygons: list[list[tuple[float, float]]],
    building_rings: list[list[tuple[float, float]]],
) -> tuple[
    list[list[tuple[float, float]]],
    list[list[tuple[float, float]]],
    int,
]:
    """Petr: skála/sráz přes budovu = chyba → zahodit objekt."""
    if not building_rings or (not lines and not polygons):
        return lines, polygons, 0
    try:
        from shapely.geometry import LineString, Polygon
        from shapely.ops import unary_union
    except ImportError:
        return lines, polygons, 0

    buildings = []
    for ring in building_rings:
        if len(ring) < 3:
            continue
        try:
            poly = Polygon(ring)
            if poly.is_empty:
                continue
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.is_empty:
                buildings.append(poly)
        except Exception:
            continue
    if not buildings:
        return lines, polygons, 0
    try:
        mask = unary_union(buildings)
    except Exception:
        return lines, polygons, 0

    dropped = 0
    kept_lines: list[list[tuple[float, float]]] = []
    for pts in lines:
        if len(pts) < 2:
            dropped += 1
            continue
        try:
            geom = LineString(pts)
            if geom.intersects(mask) and not geom.touches(mask):
                dropped += 1
                continue
        except Exception:
            pass
        kept_lines.append(pts)

    kept_polys: list[list[tuple[float, float]]] = []
    for ring in polygons:
        if len(ring) < 3:
            dropped += 1
            continue
        try:
            geom = Polygon(ring)
            if not geom.is_valid:
                geom = geom.buffer(0)
            if geom.is_empty:
                dropped += 1
                continue
            if geom.intersects(mask) and not geom.touches(mask):
                dropped += 1
                continue
        except Exception:
            pass
        kept_polys.append(ring)

    return kept_lines, kept_polys, dropped

def _total_turning_deg(pts: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(1, len(pts) - 1):
        ax, ay = pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]
        bx, by = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
        na, nb = math.hypot(ax, ay), math.hypot(bx, by)
        if na < 1e-9 or nb < 1e-9:
            continue
        cos = max(-1.0, min(1.0, (ax * bx + ay * by) / (na * nb)))
        total += math.degrees(math.acos(cos))
    return total


def _polyline_self_intersects(pts: list[tuple[float, float]]) -> bool:
    """Proper self-crossing of an open polyline (shared vertices OK)."""
    n = len(pts)
    if n < 4:
        return False
    for i in range(n - 1):
        a, b = pts[i], pts[i + 1]
        for j in range(i + 2, n - 1):
            if i == 0 and j == n - 2:
                continue
            if _segments_intersect(a, b, pts[j], pts[j + 1]):
                return True
    return False


def _polyline_length(pts: list[tuple[float, float]]) -> float:
    return sum(
        math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
        for i in range(1, len(pts))
    )


def _dedup_ticks(ticks: list[_Tick]) -> list[_Tick]:
    seen: set[tuple[int, int, int]] = set()
    out: list[_Tick] = []
    for a, b in ticks:
        if math.hypot(b[0] - a[0], b[1] - a[1]) < 0.25:
            continue
        mx = 0.5 * (a[0] + b[0])
        my = 0.5 * (a[1] + b[1])
        ang = math.atan2(b[1] - a[1], b[0] - a[0]) % math.pi
        key = (
            round(mx * DEDUP_POS_PER_M),
            round(my * DEDUP_POS_PER_M),
            round(ang * DEDUP_ANG_PER_RAD),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append((a, b))
    return out


def _mid(tick: _Tick) -> tuple[float, float]:
    (ax, ay), (bx, by) = tick
    return (0.5 * (ax + bx), 0.5 * (ay + by))


def _tangent(tick: _Tick) -> tuple[float, float]:
    (ax, ay), (bx, by) = tick
    dx, dy = bx - ax, by - ay
    n = math.hypot(dx, dy)
    if n < 1e-9:
        return (0.0, 0.0)
    return (dx / n, dy / n)


def _chain_ticks(ticks: list[_Tick]) -> list[list[int]]:
    n = len(ticks)
    cell = JOIN_GAP_M
    gap_sq = JOIN_GAP_M * JOIN_GAP_M
    mids = [_mid(t) for t in ticks]
    tans = [_tangent(t) for t in ticks]
    keys = [(int(x // cell), int(y // cell)) for x, y in mids]
    # Spotřebovaná čárka z mřížky rovnou zmizí – ve skalním poli má buňka stovky
    # čárek a procházet pořád dokola ty už použité stálo většinu času.
    grid: dict[_Cell, set[int]] = defaultdict(set)
    for i, key in enumerate(keys):
        grid[key].add(i)
    used = bytearray(n)

    def best_next(i: int) -> int | None:
        mx, my = mids[i]
        tx, ty = tans[i]
        gi, gj = keys[i]
        best_j: int | None = None
        best_d = gap_sq
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                bucket = grid.get((gi + di, gj + dj))
                if not bucket:
                    continue
                for j in bucket:
                    jx, jy = mids[j]
                    dx, dy = jx - mx, jy - my
                    d_sq = dx * dx + dy * dy
                    if d_sq < 0.0225 or d_sq > best_d:
                        continue
                    ux, uy = tans[j]
                    if abs(tx * ux + ty * uy) < JOIN_ANGLE_COS:
                        continue
                    if abs(tx * dx + ty * dy) < CHAIN_DIR_COS * math.sqrt(d_sq):
                        continue
                    best_d = d_sq
                    best_j = j
        return best_j

    def take(i: int) -> None:
        used[i] = 1
        grid[keys[i]].discard(i)

    chains: list[list[int]] = []
    for start in range(n):
        if used[start]:
            continue
        take(start)
        chain = [start]

        def grow() -> None:
            while True:
                nxt = best_next(chain[-1])
                if nxt is None:
                    return
                take(nxt)
                chain.append(nxt)

        grow()
        chain.reverse()
        grow()
        chains.append(chain)
    return chains


def _chain_polyline(ticks: list[_Tick], chain: list[int]) -> list[tuple[float, float]]:
    if len(chain) == 1:
        a, b = ticks[chain[0]]
        return [a, b]
    mids = [_mid(ticks[i]) for i in chain]
    first = ticks[chain[0]]
    second = mids[1]
    start = (
        first[0]
        if math.hypot(first[0][0] - second[0], first[0][1] - second[1])
        >= math.hypot(first[1][0] - second[0], first[1][1] - second[1])
        else first[1]
    )
    last = ticks[chain[-1]]
    prev = mids[-2]
    end = (
        last[0]
        if math.hypot(last[0][0] - prev[0], last[0][1] - prev[1])
        >= math.hypot(last[1][0] - prev[0], last[1][1] - prev[1])
        else last[1]
    )
    pts = [start]
    for mid in mids:
        if math.hypot(mid[0] - pts[-1][0], mid[1] - pts[-1][1]) >= 0.2:
            pts.append(mid)
    if math.hypot(end[0] - pts[-1][0], end[1] - pts[-1][1]) >= 0.2:
        pts.append(end)
    return pts


def _point_seg_dist(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom < 1e-18:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _simplify_polyline(
    pts: list[tuple[float, float]], tol: float
) -> list[tuple[float, float]]:
    if len(pts) <= 2 or tol <= 0:
        return pts
    max_d = -1.0
    idx = 0
    ax, ay = pts[0]
    bx, by = pts[-1]
    for i in range(1, len(pts) - 1):
        d = _point_seg_dist(pts[i][0], pts[i][1], ax, ay, bx, by)
        if d > max_d:
            max_d = d
            idx = i
    if max_d > tol:
        left = _simplify_polyline(pts[: idx + 1], tol)
        right = _simplify_polyline(pts[idx:], tol)
        return left[:-1] + right
    return [pts[0], pts[-1]]


def _neighbors4(cell: _Cell) -> tuple[_Cell, _Cell, _Cell, _Cell]:
    i, j = cell
    return ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1))


def _rock_area_polygons(
    ticks: list[_Tick],
) -> tuple[list[_Tick], list[list[tuple[float, float]]]]:
    """Plošná skalní masa → plynulý polygon; stěny nechá liniím.

    Footprint = buffer+dissolve ticků (ne řetězení konců). Morfologické otevření
    sežere tenké stěny; MRR šířka/aspect odfiltruje zbytky pásů.
    """
    polygons = _rock_footprint_rings(ticks)
    if not polygons:
        # Bez shapely / prázdný výsledek: starý rastr jader (KP hustá pole).
        return _rock_area_polygons_cells(ticks)
    remaining = _ticks_outside_rings(ticks, polygons, halo_m=ROCK_TICK_HALO_M)
    return remaining, polygons


def shapely_available() -> bool:
    """True = morph skalních ploch (buffer/dissolve) může běžet."""
    try:
        import shapely  # noqa: F401
    except ImportError:
        return False
    return True


def _rock_footprint_rings(ticks: list[_Tick]) -> list[list[tuple[float, float]]]:
    """Vyhlazený footprint z bufferovaných ticků. [] když shapely chybí."""
    if not ticks:
        return []
    try:
        from shapely import make_valid
        from shapely.geometry import LineString
        from shapely.ops import unary_union
    except ImportError:
        # Docker image dřív neměl shapely v requirements.txt → 1859 ticků
        # a cell-fallback 0 ploch (DEM má ~1 tick/buňku). Volající loguje.
        return []

    segs = [LineString([a, b]) for a, b in ticks]
    try:
        geom = unary_union(segs).buffer(ROCK_BUFFER_M, join_style=1, cap_style=1)
        geom = geom.buffer(ROCK_CLOSE_M, join_style=1).buffer(-ROCK_CLOSE_M, join_style=1)
        # Tenké stěny (1D) zmizí; kompaktní masa zůstane.
        geom = geom.buffer(-ROCK_OPEN_M, join_style=1).buffer(ROCK_OPEN_M, join_style=1)
        if ROCK_SHRINK_M > 0:
            geom = geom.buffer(-ROCK_SHRINK_M, join_style=1)
        if ROCK_SMOOTH_M > 0 and not geom.is_empty:
            geom = geom.buffer(ROCK_SMOOTH_M, join_style=1).buffer(
                -ROCK_SMOOTH_M, join_style=1
            )
        geom = make_valid(geom)
    except Exception:
        return []

    if geom is None or geom.is_empty:
        return []

    parts = []
    if geom.geom_type == "Polygon":
        parts = [geom]
    elif geom.geom_type == "MultiPolygon":
        parts = list(geom.geoms)
    else:
        try:
            parts = [g for g in geom.geoms if g.geom_type == "Polygon"]
        except Exception:
            return []

    # Skály mimo min-size cleanup: po morph nechat plochu (i <62 m² / <8 m šířky).
    # Aspect dál zahodí protáhlé zbytky stěn; vegetace má vlastní filtr.
    rings: list[list[tuple[float, float]]] = []
    for poly in parts:
        if poly.is_empty or poly.area < ROCK_DEGENERATE_AREA_M2:
            continue
        try:
            poly = poly.simplify(ROCK_SIMPLIFY_M, preserve_topology=True)
        except Exception:
            pass
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        if poly.area < ROCK_DEGENERATE_AREA_M2:
            continue
        width, length = _mrr_width_length(poly)
        if (
            length / max(width, 1e-6) > MAX_ROCK_ASPECT
            and width < MAX_ROCK_ASPECT_WIDTH_M
        ):
            continue
        coords = [(float(x), float(y)) for x, y in poly.exterior.coords]
        if len(coords) >= 2 and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) >= 3 and _ring_is_simple(coords):
            rings.append(coords)
        elif len(coords) >= 3:
            hull = _convex_hull_ring(coords)
            if len(hull) >= 3 and _ring_area(hull) >= ROCK_DEGENERATE_AREA_M2:
                rings.append(hull)
    return rings


def _mrr_width_length(poly) -> tuple[float, float]:
    """Šířka a délka minimum rotated rectangle (shapely Polygon)."""
    try:
        mrr = poly.minimum_rotated_rectangle
        coords = list(mrr.exterior.coords)
    except Exception:
        minx, miny, maxx, maxy = poly.bounds
        w, h = maxx - minx, maxy - miny
        return min(w, h), max(w, h)
    sides = [
        math.hypot(coords[i][0] - coords[i + 1][0], coords[i][1] - coords[i + 1][1])
        for i in range(4)
    ]
    return min(sides), max(sides)


def _ticks_outside_rings(
    ticks: list[_Tick],
    rings: list[list[tuple[float, float]]],
    *,
    halo_m: float,
) -> list[_Tick]:
    """Zahodí čárky uvnitř / na okraji plochy (halo), ať nezůstane rám 201."""
    try:
        from shapely.geometry import Point, Polygon
        from shapely.ops import unary_union
    except ImportError:
        return ticks
    polys = []
    for ring in rings:
        if len(ring) < 3:
            continue
        try:
            p = Polygon(ring)
            if halo_m > 0:
                p = p.buffer(halo_m)
            if not p.is_empty:
                polys.append(p)
        except Exception:
            continue
    if not polys:
        return ticks
    mask = unary_union(polys)
    out: list[_Tick] = []
    for tick in ticks:
        mx, my = _mid(tick)
        try:
            if mask.contains(Point(mx, my)):
                continue
        except Exception:
            pass
        out.append(tick)
    return out


def _rock_area_polygons_cells(
    ticks: list[_Tick],
) -> tuple[list[_Tick], list[list[tuple[float, float]]]]:
    """Fallback: plošné jádro v rastru 3 m (bez shapely / řídký footprint)."""
    cell = ROCK_CELL_M
    buckets: dict[_Cell, list[int]] = defaultdict(list)
    for i, tick in enumerate(ticks):
        x, y = _mid(tick)
        buckets[(int(math.floor(x / cell)), int(math.floor(y / cell)))].append(i)
    rocky = {
        key for key, ids in buckets.items() if len(ids) >= ROCK_MIN_TICKS_PER_CELL
    }
    core = {c for c in rocky if all(nb in rocky for nb in _neighbors4(c))}
    if len(core) < ROCK_MIN_CORE_CELLS:
        return ticks, []

    polygons: list[list[tuple[float, float]]] = []
    used_cells: set[_Cell] = set()
    for comp in _cell_components(core):
        if len(comp) < ROCK_MIN_CORE_CELLS:
            continue
        area_cells = set(comp)
        for c in comp:
            area_cells.update(nb for nb in _neighbors4(c) if nb in rocky)
        kept: list[list[tuple[float, float]]] = []
        for ring in _rock_outer_rings(area_cells, cell):
            if len(ring) >= 3 and _ring_area(ring) >= ROCK_DEGENERATE_AREA_M2:
                kept.append(ring)
        if kept:
            polygons.extend(kept)
            used_cells |= area_cells

    if not used_cells:
        return ticks, []
    for i, j in tuple(used_cells):
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                nb = (i + di, j + dj)
                if nb in rocky:
                    used_cells.add(nb)
    remaining = [
        tick
        for tick in ticks
        if (
            int(math.floor(_mid(tick)[0] / cell)),
            int(math.floor(_mid(tick)[1] / cell)),
        )
        not in used_cells
    ]
    return remaining, polygons


def _cell_components(cells: set[_Cell]) -> list[list[_Cell]]:
    seen: set[_Cell] = set()
    out: list[list[_Cell]] = []
    for seed in cells:
        if seed in seen:
            continue
        seen.add(seed)
        stack = [seed]
        comp: list[_Cell] = []
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in _neighbors4(cur):
                if nb in cells and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        out.append(comp)
    return out


def _rock_outer_rings(
    cells: set[_Cell], cell_m: float
) -> list[list[tuple[float, float]]]:
    """Vnější obrys buněk (fallback). Preferuje union čtverců přes shapely."""
    rings = _rings_from_cell_union(cells, cell_m)
    if rings is not None:
        return rings
    out: list[list[tuple[float, float]]] = []
    for ring in _trace_cell_rings(cells, cell_m):
        if _signed_ring_area(ring) <= 0:
            continue
        simple = _simplify_ring_safe(ring, ROCK_SIMPLIFY_M)
        if len(simple) >= 3 and _ring_is_simple(simple):
            out.append(simple)
    return out


def _rings_from_cell_union(
    cells: set[_Cell], cell_m: float
) -> list[list[tuple[float, float]]] | None:
    """Union čtverců buněk → vnější prstence. None = shapely není k dispozici."""
    if not cells:
        return []
    try:
        from shapely import make_valid
        from shapely.geometry import box
        from shapely.ops import unary_union
    except ImportError:
        return None

    polys = [
        box(i * cell_m, j * cell_m, (i + 1) * cell_m, (j + 1) * cell_m)
        for i, j in cells
    ]
    geom = unary_union(polys)
    pad = cell_m * 0.45
    try:
        geom = geom.buffer(pad, join_style=1).buffer(-pad, join_style=1)
    except Exception:
        pass
    try:
        geom = make_valid(geom)
    except Exception:
        pass
    if geom is None or geom.is_empty:
        return []

    parts = []
    if geom.geom_type == "Polygon":
        parts = [geom]
    elif geom.geom_type == "MultiPolygon":
        parts = list(geom.geoms)
    else:
        try:
            parts = [g for g in geom.geoms if g.geom_type == "Polygon"]
        except Exception:
            return []

    # Stejně jako footprint: skály bez min-size area cut (jen degenerát).
    rings: list[list[tuple[float, float]]] = []
    for poly in parts:
        if poly.is_empty or poly.area < ROCK_DEGENERATE_AREA_M2:
            continue
        try:
            poly = poly.simplify(ROCK_SIMPLIFY_M, preserve_topology=True)
        except Exception:
            pass
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        if poly.area < ROCK_DEGENERATE_AREA_M2:
            continue
        coords = [(float(x), float(y)) for x, y in poly.exterior.coords]
        if len(coords) >= 2 and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) >= 3 and _ring_is_simple(coords):
            rings.append(coords)
        elif len(coords) >= 3:
            hull = _convex_hull_ring(coords)
            if len(hull) >= 3 and _ring_area(hull) >= ROCK_DEGENERATE_AREA_M2:
                rings.append(hull)
    return rings


def _simplify_ring_safe(
    ring: list[tuple[float, float]], tol: float
) -> list[tuple[float, float]]:
    """Zjednoduší prstenec; při self-intersect vrátí původní nebo konvexní obálku."""
    simple = _simplify_ring(ring, tol)
    if _ring_is_simple(simple):
        return simple
    if _ring_is_simple(ring):
        return ring
    hull = _convex_hull_ring(ring)
    return hull if len(hull) >= 3 else ring


def _ring_is_simple(pts: list[tuple[float, float]]) -> bool:
    """True, když se nekříží nesousední hrany (včetně uzavření prstence)."""
    n = len(pts)
    if n < 3:
        return False
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for j in range(i + 1, n):
            if (j + 1) % n == i or (i + 1) % n == j:
                continue
            c, d = pts[j], pts[(j + 1) % n]
            if _segments_intersect(a, b, c, d):
                return False
    return True


def _segments_intersect(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    """Proper or improper intersection of open segments ab and cd (shared vertex OK)."""

    def orient(
        p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]
    ) -> int:
        v = (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])
        if abs(v) < 1e-12:
            return 0
        return 1 if v > 0 else 2

    def on_seg(
        p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]
    ) -> bool:
        return (
            min(p[0], r[0]) - 1e-9 <= q[0] <= max(p[0], r[0]) + 1e-9
            and min(p[1], r[1]) - 1e-9 <= q[1] <= max(p[1], r[1]) + 1e-9
        )

    # Shared endpoint of adjacent ring edges is not a crossing.
    if a == c or a == d or b == c or b == d:
        return False
    o1, o2 = orient(a, b, c), orient(a, b, d)
    o3, o4 = orient(c, d, a), orient(c, d, b)
    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and on_seg(a, c, b):
        return True
    if o2 == 0 and on_seg(a, d, b):
        return True
    if o3 == 0 and on_seg(c, a, d):
        return True
    if o4 == 0 and on_seg(c, b, d):
        return True
    return False


def _convex_hull_ring(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Monotone chain – nouzový jednoduchý vnější prstenec."""
    uniq = sorted(set(pts))
    if len(uniq) <= 2:
        return list(uniq)

    def cross(
        o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
    ) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in uniq:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(uniq):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _trace_cell_rings(
    cells: set[_Cell], cell_m: float
) -> list[list[tuple[float, float]]]:
    """Obtáhne obrys po hranách buněk. Vnější prstenec CCW, díra CW."""
    edges: dict[_Cell, list[_Cell]] = defaultdict(list)
    for i, j in cells:
        # Buňka (i, j) = čtverec [i, i+1] × [j, j+1] v rozích rastru.
        if (i, j - 1) not in cells:
            edges[(i, j)].append((i + 1, j))
        if (i + 1, j) not in cells:
            edges[(i + 1, j)].append((i + 1, j + 1))
        if (i, j + 1) not in cells:
            edges[(i + 1, j + 1)].append((i, j + 1))
        if (i - 1, j) not in cells:
            edges[(i, j + 1)].append((i, j))

    rings: list[list[tuple[float, float]]] = []
    while edges:
        start = next(iter(edges))
        ring_cells = [start]
        cur = start
        incoming: _Cell | None = None
        while True:
            outgoing = edges.get(cur)
            if not outgoing:
                break
            nxt = _pick_next(outgoing, cur, incoming)
            if not outgoing:
                del edges[cur]
            incoming = (nxt[0] - cur[0], nxt[1] - cur[1])
            if nxt == start:
                break
            ring_cells.append(nxt)
            cur = nxt
        if len(ring_cells) >= 4:
            rings.append([(x * cell_m, y * cell_m) for x, y in ring_cells])
    return rings


def _pick_next(outgoing: list[_Cell], cur: _Cell, incoming: _Cell | None) -> _Cell:
    """V rohu, kde se dva laloky dotýkají, zahne doleva – laloky zůstanou oddělené."""
    if incoming is None or len(outgoing) == 1:
        return outgoing.pop()
    ix, iy = incoming

    def rank(nxt: _Cell) -> int:
        dx, dy = nxt[0] - cur[0], nxt[1] - cur[1]
        cross = ix * dy - iy * dx
        if cross > 0:
            return 0
        if ix * dx + iy * dy > 0:
            return 1
        return 2 if cross < 0 else 3

    best = min(range(len(outgoing)), key=lambda k: rank(outgoing[k]))
    return outgoing.pop(best)


def _simplify_ring(
    ring: list[tuple[float, float]], tol: float
) -> list[tuple[float, float]]:
    """Douglas–Peucker na uzavřeném prstenci – rozdělí ho na dvě půlky."""
    if len(ring) <= 4 or tol <= 0:
        return ring
    ax, ay = ring[0]
    far = max(
        range(1, len(ring)),
        key=lambda i: (ring[i][0] - ax) ** 2 + (ring[i][1] - ay) ** 2,
    )
    first = _simplify_polyline(ring[: far + 1], tol)
    second = _simplify_polyline(ring[far:] + [ring[0]], tol)
    out = first[:-1] + second[:-1]
    return out if len(out) >= 3 else ring


def _signed_ring_area(pts: list[tuple[float, float]]) -> float:
    if len(pts) < 3:
        return 0.0
    s = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return s * 0.5


def _ring_area(pts: list[tuple[float, float]]) -> float:
    return abs(_signed_ring_area(pts))


