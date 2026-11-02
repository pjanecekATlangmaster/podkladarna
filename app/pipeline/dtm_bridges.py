"""Mosty a více úrovní z DTM kraje (ZPS_OPL, ``UrovenUmisteniObjektuZPS`` ≥ 1) – sprint.

Mostovka není vždy ``PlochaMostniKonstrukcePlocha``: lávky, rampy a schodiště jsou často jen
``ChodnikPlocha`` / ``SchodistePlocha`` na úrovni +1. Půdorys mostu na každé úrovni se proto skládá z mostní
konstrukce i z povrchů na téže úrovni. Spodní úrovně (povrch pod mostem) se pod půdorysem vystřihnou, mostovka
se nakreslí barvou svého povrchu (chodník 501.0, silnice 501.20) a tam, kde se pod mostem dá přejít,
se na hraně půdorysu nakreslí hrany 512.100 (trojúhelníky): nejdřív podél chodníků, jinak podél silnice.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from app.fsutil import atomic_write_text
from app.pipeline.jvf_reader import iter_records

DECK_CACHE_SUFFIX = ".deck1.json"
STRUCTURE_TYPE = "PlochaMostniKonstrukcePlocha"
# Povrchy mostovky podle barvy: chodník (501.0) a silnice (501.20). Železnice a tramvaj mostovku neplní.
LIGHT_TYPES = (
    "ChodnikPlocha",
    "CyklostezkaPlocha",
    "SchodistePlocha",
    "StavbaProZpevneniPovrchuPlocha",
)
HEAVY_TYPES = (
    "ProvozniPlochaPozemniKomunikacePlocha",
    "ManipulacniPlochaPlocha",
    "ParkovisteOdstavnaPlochaPlocha",
)
RAIL_TYPES = ("TramvajovaDrahaPlocha", "SouhrnnaPlochaZeleznicnichDrahPlocha")
DECK_TYPES = (STRUCTURE_TYPE, *LIGHT_TYPES, *HEAVY_TYPES, *RAIL_TYPES)
# Hrana 512.100 se kreslí přes spodní povrch (chodník, jinak silnice), který se pod mostem dá přejít.
# Chodník do této vzdálenosti (m) od mostu má přednost před silnicí.
SIDEWALK_NEAR_M = 5.0
# Dlouhé úseky hrany (souběžné s povrchem pod mostem) se neznačí; značí se jen přes jeho šířku.
EDGE_MAX_LEN_M = 15.0
# Průchod pod mostem: hrany vstupu a výstupu jsou od sebe aspoň tolik (m) a spojnice jejich středů svírá s hranou
# nejvýš tuto složku (|cos| ≤ ..., tj. vede napříč hranami).
THROUGH_MIN_DIST_M = 2.5
THROUGH_MAX_ALONG = 0.6
# S půdorysem mostu: spojnice středů hran míří dovnitř mostu aspoň takto (kosinus k normále, ~73°) – i šikmé křížení.
THROUGH_MIN_INWARD = 0.3
# Silnice pod mostem bývá širší než chodník.
ROAD_EDGE_MAX_LEN_M = 30.0
EDGE_MIN_LEN_M = 1.0
# Mostovky menší než tato plocha (m²) se nekreslí (střepy).
MIN_DECK_AREA_M2 = 4.0


def load_deck_polygons(opl_zip: str | Path, *, use_cache: bool = True) -> list[dict]:
    """Plochy mostů (úroveň ≥ 1): ``[{"l": úroveň, "t": typ, "p": [vnější obrys, díra, …]}, …]`` (S-JTSK)."""
    opl_zip = Path(opl_zip)
    cache = opl_zip.with_name(opl_zip.name + DECK_CACHE_SUFFIX)
    if use_cache and cache.is_file() and cache.stat().st_mtime >= opl_zip.stat().st_mtime:
        try:
            raw = json.loads(cache.read_text(encoding="utf-8"))
            return [{"l": r["l"], "t": r["t"], "p": [[(x, y) for x, y in ring] for ring in r["p"]]} for r in raw]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    out: list[dict] = []
    for rec in iter_records(opl_zip, set(DECK_TYPES)):
        if rec.geom_type != "Polygon" or not rec.coords or rec.level < 1:
            continue
        poly = [[(float(c[0]), float(c[1])) for c in ring] for ring in rec.coords if len(ring) >= 4]
        if poly:
            out.append({"l": rec.level, "t": rec.type_name, "p": poly})
    if use_cache:
        try:
            atomic_write_text(
                cache,
                json.dumps([{"l": d["l"], "t": d["t"], "p": [[list(p) for p in ring] for ring in d["p"]]} for d in out]),
                encoding="utf-8",
            )
        except OSError:
            pass
    return out


def _poly(rings):
    from shapely.geometry import Polygon

    try:
        g = Polygon(rings[0], rings[1:])
    except (ValueError, IndexError):
        return None
    g = g if g.is_valid else g.buffer(0)
    return None if g.is_empty else g


def level_decks(decks: list[dict]) -> dict[int, dict[str, object]]:
    """``{úroveň: {"light": g, "heavy": g, "all": g}}`` (shapely) – mostovka po barvách a celý půdorys.

    Mostní konstrukce bez vlastního povrchu převezme barvu povrchu, který ji z větší části pokrývá
    (jinak chodník). Povrch nad vyšší úrovní se z nižší vystřihne.
    """
    from shapely.ops import unary_union

    by_level: dict[int, dict[str, list]] = {}
    for d in decks:
        g = _poly(d["p"])
        if g is None:
            continue
        cls = "structure" if d["t"] == STRUCTURE_TYPE else "light" if d["t"] in LIGHT_TYPES else "heavy" if d["t"] in HEAVY_TYPES else "rail"
        by_level.setdefault(int(d["l"]), {"light": [], "heavy": [], "rail": [], "structure": []})[cls].append(g)
    out: dict[int, dict[str, object]] = {}
    for level, parts in by_level.items():
        light = unary_union(parts["light"]) if parts["light"] else None
        heavy = unary_union(parts["heavy"]) if parts["heavy"] else None
        light_extra, heavy_extra = [], []
        for st in parts["structure"]:
            la = light.intersection(st).area if light is not None else 0.0
            ha = heavy.intersection(st).area if heavy is not None else 0.0
            (heavy_extra if ha > la else light_extra).append(st)
        light_all = unary_union(([light] if light is not None else []) + light_extra) if (light is not None or light_extra) else None
        heavy_all = unary_union(([heavy] if heavy is not None else []) + heavy_extra) if (heavy is not None or heavy_extra) else None
        if light_all is not None and heavy_all is not None:
            light_all = light_all.difference(heavy_all)  # silnice má na mostě přednost před chodníkem (plocha se nepřekrývá)
        full = unary_union([g for g in (light_all, heavy_all, *parts["rail"]) if g is not None and not g.is_empty])
        out[level] = {"light": light_all, "heavy": heavy_all, "all": full}
    # vyšší úroveň vystřihne nižší (mostovka pod mostovkou)
    levels = sorted(out)
    for i, lv in enumerate(levels):
        above = [out[h]["all"] for h in levels[i + 1 :] if out[h]["all"] is not None]
        if not above:
            continue
        cover = unary_union(above)
        for key in ("light", "heavy"):
            if out[lv][key] is not None:
                out[lv][key] = out[lv][key].difference(cover)
    return out


def footprint(levels: dict[int, dict[str, object]]):
    """Sjednocený půdorys všech mostů (úrovně ≥ 1) nebo None."""
    from shapely.ops import unary_union

    parts = [v["all"] for v in levels.values() if v["all"] is not None and not v["all"].is_empty]
    return unary_union(parts) if parts else None


def _orient_inside(piece, deck):
    """Souřadnice čáry ``piece`` otočené tak, aby vnitřek mostu ``deck`` ležel vlevo od směru kreslení; None bez směru."""
    from shapely.geometry import Point

    coords = [(float(x), float(y)) for x, y in piece.coords]
    s_mid = piece.length / 2.0
    mid = piece.interpolate(s_mid)
    a = piece.interpolate(max(0.0, s_mid - 0.5))
    b = piece.interpolate(min(piece.length, s_mid + 0.5))
    tx, ty = b.x - a.x, b.y - a.y
    n = math.hypot(tx, ty)
    if n < 1e-9:
        return None
    nx, ny = -ty / n, tx / n  # levá normála
    if not deck.contains(Point(mid.x + nx * 0.4, mid.y + ny * 0.4)):
        coords.reverse()
    return coords


def _crossings(deck, surface, max_len_m: float):
    """Průchody povrchu ``surface`` pod mostem ``deck``: ``[(část povrchu pod mostem, [hrany přes jeho šířku])]``.

    Průchod = část povrchu uvnitř půdorysu, která hranu mostu protíná aspoň dvakrát (vstup a výstup);
    povrch, který pod most jen zasahuje (např. chodník, na který mostovka přistává), se neznačí.
    """
    out = []
    inside = deck.intersection(surface)
    comps = [inside] if inside.geom_type == "Polygon" else [g for g in getattr(inside, "geoms", []) if g.geom_type == "Polygon"]
    for comp in comps:
        if comp.area <= 0.5:
            continue
        crossing = deck.boundary.intersection(comp.buffer(0.03))  # hrana části leží na hraně mostu jen přibližně
        pieces = [crossing] if crossing.geom_type == "LineString" else [g for g in getattr(crossing, "geoms", []) if g.geom_type == "LineString"]
        pieces = [pc for pc in pieces if EDGE_MIN_LEN_M <= pc.length <= max_len_m]
        pieces = _through_pairs(pieces, deck)
        if len(pieces) >= 2:
            out.append((comp, pieces))
    return out


def _through_pairs(pieces: list, deck=None) -> list:
    """Hrany, které tvoří dvojici „vstup – výstup“: spojnice jejich středů vede napříč hranami (ne podél nich).

    Povrch, který jen sousedí s obloukem mostu (rampa, přistání), má hrany podél sebe a vypadne. S půdorysem
    ``deck`` se hrany posuzují podle normál dovnitř mostu: spojnice vede od jedné hrany dovnitř a do druhé zevnitř
    (každá svírá s normálou nejvýš ~73°), takže projde i šikmé křížení (silnice pod mostem pod ostrým úhlem).
    """
    if len(pieces) < 2:
        return []
    info = []
    for pc in pieces:
        mid = pc.interpolate(pc.length / 2.0)
        a = pc.interpolate(max(0.0, pc.length / 2.0 - 0.5))
        b = pc.interpolate(min(pc.length, pc.length / 2.0 + 0.5))
        tx, ty = b.x - a.x, b.y - a.y
        n = math.hypot(tx, ty) or 1.0
        tx, ty = tx / n, ty / n
        nx = ny = None
        if deck is not None:
            from shapely.geometry import Point

            nx, ny = -ty, tx  # levá normála; dovnitř mostu?
            if not deck.contains(Point(mid.x + nx * 0.4, mid.y + ny * 0.4)):
                nx, ny = -nx, -ny
        info.append((mid.x, mid.y, tx, ty, nx, ny))
    keep = set()
    for i in range(len(info)):
        for j in range(i + 1, len(info)):
            vx, vy = info[j][0] - info[i][0], info[j][1] - info[i][1]
            d = math.hypot(vx, vy)
            if d < THROUGH_MIN_DIST_M:
                continue
            vx, vy = vx / d, vy / d
            if deck is not None:
                ok = vx * info[i][4] + vy * info[i][5] >= THROUGH_MIN_INWARD and -(vx * info[j][4] + vy * info[j][5]) >= THROUGH_MIN_INWARD
            else:
                ok = abs(vx * info[i][2] + vy * info[i][3]) <= THROUGH_MAX_ALONG and abs(vx * info[j][2] + vy * info[j][3]) <= THROUGH_MAX_ALONG
            if ok:
                keep.update((i, j))
    return [pieces[i] for i in sorted(keep)]


LOWER_PAVED_CODE = "512.311"  # oblast průběžná v nižší úrovni (zpevněná plocha 501, 501.3)
# Průnik mostovky se spodním povrchem menší než tato plocha (m²) se nekreslí.
MIN_LOWER_AREA_M2 = 1.0


def lower_level_areas(foot, paved0, water0=None) -> dict[str, object]:
    """Části mostů, pod kterými prochází spodní zpevněná plocha: ``{"paved": geom}`` (shapely nebo None).

    Část mostu nad vodou ``water0`` se nebere (pod mostem tam nikdo neběží, mostovka se kreslí barvou svého povrchu).
    Zbytek každého souvislého mostu ``foot`` se rozdělí na souvislé kusy (na každém břehu zvlášť) a kus, který kříží
    zpevněné plochy ``paved0`` (chodníky, silnice, parkoviště) na úrovni 0, se vrací **celý** (od napojení na cestu
    po napojení na další cestu nebo po vodu). V ISSprOM se kreslí symbolem 512.311 (bílý základ a šrafa barvou
    zpevněné plochy).
    """
    from shapely.ops import unary_union

    out: dict[str, object] = {"paved": None}
    if foot is None or foot.is_empty or paved0 is None or paved0.is_empty:
        return out
    if water0 is not None and not water0.is_empty:
        foot = foot.difference(water0)
    comps = [foot] if foot.geom_type == "Polygon" else [g for g in getattr(foot, "geoms", []) if g.geom_type == "Polygon"]
    paved_parts = [
        comp for comp in comps if comp.area >= MIN_DECK_AREA_M2 and comp.intersection(paved0).area >= MIN_LOWER_AREA_M2
    ]
    if paved_parts:
        out["paved"] = unary_union(paved_parts)
    return out


def entrance_edges(foot, light0, heavy0, near_m: float = SIDEWALK_NEAR_M) -> list[list[tuple[float, float]]]:
    """Hrany 512.100 na půdorysu mostu tam, kde pod ním vede chodník (jinak silnice) na úrovni 0.

    ``foot``: půdorys mostů (shapely), ``light0`` / ``heavy0``: chodníky a silnice na úrovni 0 (z DTM, vč. části
    pod mostem). Hrany se kreslí přes šířku každého chodníku, který pod mostem prochází. Silnice se značí jen tam,
    kde u ní do ``near_m`` není chodník. Čáry jsou orientované tak, že vnitřek mostu leží vlevo od směru kreslení.
    """
    if foot is None or foot.is_empty:
        return []
    comps = [foot] if foot.geom_type == "Polygon" else [g for g in getattr(foot, "geoms", []) if g.geom_type == "Polygon"]
    edges: list[list[tuple[float, float]]] = []
    for deck in comps:
        if deck.area < MIN_DECK_AREA_M2:
            continue
        found = []
        if light0 is not None and not light0.is_empty:
            found.extend(piece for _c, pieces in _crossings(deck, light0, EDGE_MAX_LEN_M) for piece in pieces)
        if heavy0 is not None and not heavy0.is_empty:
            for comp, pieces in _crossings(deck, heavy0, ROAD_EDGE_MAX_LEN_M):
                if light0 is not None and not light0.is_empty and light0.distance(comp) <= near_m:
                    continue  # u silnice je chodník → značí se chodník
                found.extend(pieces)
        for piece in found:
            coords = _orient_inside(piece, deck)
            if coords is not None:
                edges.append(coords)
    return edges


# Průchody pod mostem podle čar cest (OSM): křížení s okrajem mostu do této vzdálenosti (m) od konce čáry je napojení.
LINE_END_CLEAR_M = 1.5
# Čára pod mostem: úsek uvnitř půdorysu nejvýš tak dlouhý (m) – delší vede po mostě / podél něj, ne pod ním napříč.
LINE_THROUGH_MAX_M = 80.0
# Nejmenší sinus úhlu čáry s okrajem mostu (souběžná čára hranu nedostane).
LINE_MIN_SIN = 0.34
# Hrana z čáry se nekreslí, když už do této vzdálenosti (m) od místa křížení leží hrana ze spodní plochy.
LINE_EDGE_DUP_M = 3.0


def line_entrance_edges(foot, lines, existing=()) -> list[list[tuple[float, float]]]:
    """Hrany 512.100 tam, kde pod mostem ``foot`` prochází čára cesty na úrovni 0, kterou plochy z DTM nezachytily.

    ``lines``: ``[(LineString, šířka kresby v m)]`` (bez čar na mostech a v tunelech). Čára musí půdorysem projít
    (vstup a výstup na okraji mostu, ne konec čáry pod mostem ani na něm). Hrana leží na okraji mostu kolem
    místa křížení v délce šířky kresby / sin úhlu (nejvýš ``ROAD_EDGE_MAX_LEN_M``); kde už je hrana z plochy
    (``existing``), se nepřidává. Čáry jsou orientované tak, že vnitřek mostu leží vlevo od směru kreslení.
    """
    from shapely.geometry import LineString, Point
    from shapely.ops import substring

    if foot is None or foot.is_empty or not lines:
        return []
    comps = [foot] if foot.geom_type == "Polygon" else [g for g in getattr(foot, "geoms", []) if g.geom_type == "Polygon"]
    taken = [LineString(e) for e in existing if len(e) >= 2]
    edges: list[list[tuple[float, float]]] = []
    for deck in comps:
        if deck.area < MIN_DECK_AREA_M2:
            continue
        ring = deck.exterior
        for line, width_m in lines:
            if not line.intersects(deck):
                continue
            inside = line.intersection(deck)
            segs = [inside] if inside.geom_type == "LineString" else [g for g in getattr(inside, "geoms", []) if g.geom_type == "LineString"]
            for seg in segs:
                if seg.length < EDGE_MIN_LEN_M or seg.length > LINE_THROUGH_MAX_M:
                    continue
                ends = [Point(seg.coords[0]), Point(seg.coords[-1])]
                if any(ring.distance(q) > 0.05 for q in ends):
                    continue  # čára končí pod mostem (nebo vede dírou v půdorysu)
                if any(line.project(q) <= LINE_END_CLEAR_M or line.length - line.project(q) <= LINE_END_CLEAR_M for q in ends):
                    continue  # čára na okraji mostu končí (napojení, schody na most)
                for q in ends:
                    if any(t.distance(q) <= LINE_EDGE_DUP_M for t in taken):
                        continue
                    s_r = ring.project(q)
                    a, b = ring.interpolate(max(0.0, s_r - 0.5)), ring.interpolate(min(ring.length, s_r + 0.5))
                    s_l = line.project(q)
                    c, d = line.interpolate(max(0.0, s_l - 0.5)), line.interpolate(min(line.length, s_l + 0.5))
                    tr = (b.x - a.x, b.y - a.y)
                    tl = (d.x - c.x, d.y - c.y)
                    nr, nl = math.hypot(*tr), math.hypot(*tl)
                    if nr < 1e-9 or nl < 1e-9:
                        continue
                    sin = abs(tr[0] * tl[1] - tr[1] * tl[0]) / (nr * nl)
                    if sin < LINE_MIN_SIN:
                        continue
                    half = min(ROAD_EDGE_MAX_LEN_M / 2.0, (width_m / 2.0 + 0.5) / sin)
                    lo, hi = s_r - half, s_r + half
                    if lo < 0.0 or hi > ring.length:  # kus přes začátek obrysu
                        piece = LineString(
                            [ring.interpolate(s % ring.length).coords[0] for s in [lo + (hi - lo) * k / 20.0 for k in range(21)]]
                        )
                    else:
                        piece = substring(ring, lo, hi)
                    if piece.geom_type != "LineString" or piece.length < EDGE_MIN_LEN_M:
                        continue
                    coords = _orient_inside(piece, deck)
                    if coords is not None:
                        edges.append(coords)
                        taken.append(piece)
    return edges
