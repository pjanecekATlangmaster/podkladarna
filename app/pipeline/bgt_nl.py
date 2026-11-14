"""BGT (Basisregistratie Grootschalige Topografie, Nizozemsko) jako obdoba české DTM pro kresbu.

Petr 2026-11-14: „Při zařazení země je DTM priorita“ – v Nizozemsku je to BGT (CC0, PDOK). Data se berou
z OGC API Features PDOK (``BGT_API``) jen v aktuální verzi (CQL2 ``eind_registratie IS NULL``; bez filtru
API vrací i historické verze a je několikanásobně větší). Výřez se stahuje po dlaždicích ``TILE_M``
souběžně (stránkování jedné velké dlaždice je na serveru pomalé), objekty z více dlaždic se sloučí podle
``lokaal_id``.

Výsledek se zapíše do ``work_dir/dtm`` ve stejném tvaru jako příprava DTM (``dtm_import.prepare_dtm_fences``),
takže kresba (budovy, silnice, chodníky, parkoviště, schody, mosty, ploty a jejich orientace) jde stejnou
cestou jako v Česku. Navíc ``fences.json`` nese ``areas`` (plochy pro orientaci plotů, v Česku se čtou
z balíků OPL) a ``yards`` – BGT ``erf`` (soukromý pozemek u budovy), který se kreslí jako oliva 520.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable

from app.pipeline import build_stats, dtm_fences

BGT_API = "https://api.pdok.nl/lv/bgt/ogc/v1"
RD_CRS_URI = "http://www.opengis.net/def/crs/EPSG/0/28992"
SOURCE_LABEL = "BGT (PDOK)"
STEP_LABEL = "data BGT"  # krok průběhu jobu místo „DTM kraje“
BACKGROUND_NAME = "Data BGT (PDOK)"
TILE_M = 500.0  # dlaždice stahování (≈ 1–3 s na dotaz i v centru Delftu)
WORKERS = 6
PAGE_LIMIT = 1000
HTTP_TIMEOUT_S = 90
HTTP_RETRIES = 2
CURRENT_FILTER = "eind_registratie IS NULL"
# Mřížka, na kterou se plochy BGT přichytí (m). Oblouky BGT API linearizuje u každého objektu zvlášť, takže sdílená
# hrana dvou sousedních ploch má body o milimetry vedle sebe – GEOS pak při rozdílech a sjednoceních hlásí
# „non-noded intersection“ (sprint Delft). Centimetrová mřížka je pod přesností BGT.
GRID_M = 0.01
# Zjednodušení hran ploch po uzlování (m, Douglas–Peucker): pod přesností BGT, kresba zjednodušuje až po 0,1 m.
SIMPLIFY_M = 0.02

COLLECTIONS = (
    "pand",
    "wegdeel",
    "ondersteunendwegdeel",
    "onbegroeidterreindeel",
    "begroeidterreindeel",
    "scheiding_lijn",
    "overbruggingsdeel",
)

PAVED = frozenset({"open verharding", "gesloten verharding", "half verhard"})
# wegdeel.functie → soubor DTM (CZ obdoba): jízdní pás a sjezdy = ProvozniPlocha / ManipulacniPlocha.
ROAD_FUNCTIONS = frozenset(
    {
        "rijbaan autosnelweg",
        "rijbaan autoweg",
        "rijbaan regionale weg",
        "rijbaan lokale weg",
        "OV-baan",
        "woonerf",
        "overweg",
        "inrit",
        "transitie",
    }
)
SIDEWALK_FUNCTIONS = frozenset({"voetpad", "voetgangersgebied"})  # ChodnikPlocha; fietspad jako CZ cyklostezka ne
STAIRS_FUNCTIONS = frozenset({"voetpad op trap"})
PARKING_FUNCTIONS = frozenset({"parkeervlak"})
LIGHT_DECK_FUNCTIONS = SIDEWALK_FUNCTIONS | STAIRS_FUNCTIONS | {"fietspad", "ruiterpad"}
# begroeidterreindeel.fysiek_voorkomen
GREEN_COVER = frozenset({"groenvoorziening"})
GARDEN_COVER = frozenset({"fruitteelt", "boomteelt"})
OTHER_COVER = frozenset(
    {
        "loofbos",
        "naaldbos",
        "gemengd bos",
        "houtwal",
        "grasland agrarisch",
        "grasland overig",
        "akkerland",
        "heide",
        "moeras",
        "rietland",
        "struiken",
        "kwelder",
        "duin",
    }
)
YARD_COVER = "erf"  # onbegroeidterreindeel: pozemek u budovy (soukromý) → oliva
FENCE_TYPES = frozenset({"hek"})

LogFn = Callable[[str], None]
Polygon = list[list[tuple[float, float]]]


def tile_bounds(bounds: tuple[float, float, float, float], tile_m: float = TILE_M) -> list[tuple[float, float, float, float]]:
    """Výřez rozdělený na dlaždice nejvýš ``tile_m`` (zaokrouhlené na metry)."""
    import math

    xmin, ymin, xmax, ymax = bounds
    nx = max(1, math.ceil((xmax - xmin) / tile_m))
    ny = max(1, math.ceil((ymax - ymin) / tile_m))
    out = []
    for i in range(nx):
        for j in range(ny):
            out.append(
                (
                    round(xmin + i * (xmax - xmin) / nx, 1),
                    round(ymin + j * (ymax - ymin) / ny, 1),
                    round(xmin + (i + 1) * (xmax - xmin) / nx, 1),
                    round(ymin + (j + 1) * (ymax - ymin) / ny, 1),
                )
            )
    return out


def items_url(collection: str, bounds: tuple[float, float, float, float]) -> str:
    query = {
        "bbox": ",".join(f"{v:.1f}" for v in bounds),
        "bbox-crs": RD_CRS_URI,
        "crs": RD_CRS_URI,
        "limit": PAGE_LIMIT,
        "filter": CURRENT_FILTER,
        "filter-lang": "cql2-text",
        "f": "json",
    }
    return f"{BGT_API}/collections/{collection}/items?{urllib.parse.urlencode(query)}"


def _get_json(url: str) -> tuple[dict, int]:
    from app.pipeline.fetch_openzu import USER_AGENT

    last: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read()
            return json.loads(raw), len(raw)
        except Exception as exc:  # síť / 5xx: krátce počkat a zkusit znovu
            last = exc
            if attempt < HTTP_RETRIES:
                time.sleep(2.0 * (attempt + 1))
    assert last is not None
    raise last


def fetch_tile(collection: str, bounds: tuple[float, float, float, float]) -> tuple[list[dict], int]:
    """Všechny aktuální objekty kolekce v dlaždici (se stránkováním) a stažené bajty."""
    url: str | None = items_url(collection, bounds)
    feats: list[dict] = []
    nbytes = 0
    while url:
        data, n = _get_json(url)
        nbytes += n
        feats.extend(data.get("features") or [])
        url = next((ln.get("href") for ln in data.get("links") or [] if ln.get("rel") == "next"), None)
    return feats, nbytes


def fetch_collections(
    bounds: tuple[float, float, float, float],
    collections: Iterable[str] = COLLECTIONS,
    *,
    tile_m: float = TILE_M,
    workers: int = WORKERS,
    fetch: Callable[[str, tuple], tuple[list[dict], int]] = fetch_tile,
) -> tuple[dict[str, list[dict]], int]:
    """``{kolekce: [objekt, …]}`` pro výřez (dlaždice souběžně, objekty sloučené podle ``lokaal_id``)."""
    jobs = [(c, t) for c in collections for t in tile_bounds(bounds, tile_m)]
    out: dict[str, dict[str, dict]] = {c: {} for c in collections}
    total = 0
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="bgt") as ex:
        for (col, _tile), (feats, nbytes) in zip(jobs, ex.map(lambda j: fetch(*j), jobs)):
            total += nbytes
            seen = out[col]
            for i, f in enumerate(feats):
                props = f.get("properties") or {}
                key = str(props.get("lokaal_id") or f.get("id") or f"{_tile}:{i}")
                seen.setdefault(key, f)
    return {c: list(v.values()) for c, v in out.items()}, total


def _rings(coords) -> Polygon:
    return [[(float(p[0]), float(p[1])) for p in ring] for ring in coords if len(ring) >= 4]


def feature_polygons(feat: dict) -> list[Polygon]:
    geom = feat.get("geometry") or {}
    kind = geom.get("type")
    coords = geom.get("coordinates") or []
    if kind == "Polygon":
        polys = [_rings(coords)]
    elif kind == "MultiPolygon":
        polys = [_rings(part) for part in coords]
    else:
        return []
    return [p for p in polys if p]


def feature_lines(feat: dict) -> list[list[tuple[float, float]]]:
    geom = feat.get("geometry") or {}
    kind = geom.get("type")
    coords = geom.get("coordinates") or []
    if kind == "LineString":
        parts = [coords]
    elif kind == "MultiLineString":
        parts = coords
    else:
        return []
    return [[(float(p[0]), float(p[1])) for p in part] for part in parts if len(part) >= 2]


def _level(props: dict) -> int:
    try:
        return int(props.get("relatieve_hoogteligging") or 0)
    except (TypeError, ValueError):
        return 0


def _in_bounds(poly: Polygon, bounds: tuple[float, float, float, float]) -> bool:
    xs = [p[0] for p in poly[0]]
    ys = [p[1] for p in poly[0]]
    xmin, ymin, xmax, ymax = bounds
    return not (max(xs) < xmin or min(xs) > xmax or max(ys) < ymin or min(ys) > ymax)


def classify(data: dict[str, list[dict]]) -> dict[str, list]:
    """Objekty BGT roztříděné do souborů DTM (``buildings``, ``roads``, …) + ``fences``, ``decks``, ``areas``."""
    from app.pipeline import dtm_bridges

    out: dict[str, list] = {
        k: []
        for k in (
            "buildings",
            "roads",
            "sidewalks",
            "stairs",
            "parking",
            "small_paved",
            "green",
            "other_cover",
            "garden_cover",
            "yards",
            "decks",
            "fences",
            "public",
        )
    }

    def add(key: str, feat: dict) -> None:
        out[key].extend(clean_polygons(feature_polygons(feat)))

    for f in data.get("pand") or []:
        if _level(f.get("properties") or {}) == 0:
            add("buildings", f)
    for f in data.get("wegdeel") or []:
        props = f.get("properties") or {}
        func = str(props.get("functie") or "")
        paved = str(props.get("fysiek_voorkomen") or "") in PAVED
        level = _level(props)
        if level >= 1:
            if func in ROAD_FUNCTIONS or func in PARKING_FUNCTIONS:
                deck_type = dtm_bridges.HEAVY_TYPES[0]
            elif func in LIGHT_DECK_FUNCTIONS:
                deck_type = dtm_bridges.LIGHT_TYPES[0]
            else:
                continue
            out["decks"].extend({"l": level, "t": deck_type, "p": p} for p in clean_polygons(feature_polygons(f)))
            continue
        if level < 0:
            continue
        add("public", f)
        if not paved:
            continue
        if func in ROAD_FUNCTIONS:
            add("roads", f)
        elif func in SIDEWALK_FUNCTIONS:
            add("sidewalks", f)
        elif func in STAIRS_FUNCTIONS:
            add("stairs", f)
        elif func in PARKING_FUNCTIONS:
            add("parking", f)
    for f in data.get("ondersteunendwegdeel") or []:
        props = f.get("properties") or {}
        if _level(props) != 0:
            continue
        add("public", f)
        if str(props.get("fysiek_voorkomen") or "") in GREEN_COVER:
            add("green", f)
    for f in data.get("begroeidterreindeel") or []:
        props = f.get("properties") or {}
        if _level(props) != 0:
            continue
        cover = str(props.get("fysiek_voorkomen") or "")
        if cover in GREEN_COVER:
            add("green", f)
        elif cover in GARDEN_COVER:
            add("garden_cover", f)
        elif cover in OTHER_COVER:
            add("other_cover", f)
    for f in data.get("onbegroeidterreindeel") or []:
        props = f.get("properties") or {}
        if _level(props) != 0:
            continue
        cover = str(props.get("fysiek_voorkomen") or "")
        if cover == YARD_COVER:
            add("yards", f)
        elif cover in PAVED:
            add("small_paved", f)
    for f in data.get("overbruggingsdeel") or []:
        props = f.get("properties") or {}
        level = max(1, _level(props))
        out["decks"].extend({"l": level, "t": dtm_bridges.STRUCTURE_TYPE, "p": p} for p in clean_polygons(feature_polygons(f)))
    for f in data.get("scheiding_lijn") or []:
        props = f.get("properties") or {}
        if str(props.get("type") or "") not in FENCE_TYPES:
            continue
        level = _level(props)
        for line in feature_lines(f):
            out["fences"].append(
                dtm_fences.Fence(
                    coords=tuple(line),
                    druh="",
                    level=level,
                    boundary=False,
                    zpusob="1",
                    oid=str(props.get("lokaal_id") or ""),
                )
            )
    return out


# Plochy jedné vrstvy BGT (úroveň 0) tvoří souvislé pokrytí; kreslí se z nich rozdíly a sjednocení (silnice minus
# chodníky, parkoviště minus silnice …). Pořadí = přednost, když plochu po uzlování pokrývají dvě.
COVERAGE_KEYS = (
    "buildings",
    "roads",
    "parking",
    "sidewalks",
    "stairs",
    "small_paved",
    "yards",
    "green",
    "garden_cover",
    "other_cover",
)


def node_coverage(
    sorted_data: dict[str, list], grid_m: float = GRID_M, simplify_m: float = SIMPLIFY_M
) -> dict[str, list]:
    """Plochy pokrytí (``COVERAGE_KEYS``) se společnými hranami přesně ve stejných bodech.

    Oblouky BGT jsou u každého objektu linearizované zvlášť, sousední plochy se proto na sdílené hraně o milimetry
    kříží a GEOS v kresbě padá („non-noded intersection“, sprint Delft). Hrany všech ploch se slijí (uzly na mřížce
    ``grid_m``), rozloží na stěny (polygonize) a každá stěna připadne ploše, ve které leží její vnitřní bod (při
    překryvu podle pořadí ``COVERAGE_KEYS``); stěny téže plochy se znovu sjednotí. Mezery bez vlastníka odpadnou.
    """
    import shapely
    from shapely.geometry import Polygon as ShPolygon

    items: list[tuple[int, str, int, object]] = []
    for rank, key in enumerate(COVERAGE_KEYS):
        for idx, poly in enumerate(sorted_data.get(key) or []):
            try:
                g = ShPolygon(poly[0], poly[1:])
            except (ValueError, IndexError):
                continue
            if g.is_valid and not g.is_empty:
                items.append((rank, key, idx, g))
    if not items:
        return sorted_data
    geoms = [g for *_rest, g in items]
    lines = shapely.union_all(shapely.boundary(geoms), grid_size=grid_m)
    if simplify_m > 0:
        # Linearizované oblouky mají body po centimetrech (Delft: 320 000 bodů na 3 000 chodníků, sjednocení chodníků
        # v kresbě 38 s); hrany mezi uzly se zjednoduší jednou pro obě sousední plochy a znovu se uzlují.
        lines = shapely.union_all(
            list(shapely.get_parts(shapely.simplify(shapely.line_merge(lines), simplify_m, preserve_topology=False))),
            grid_size=grid_m,
        )
    faces = list(shapely.get_parts(shapely.polygonize(list(shapely.get_parts(lines)))))
    if not faces:
        return sorted_data
    tree = shapely.STRtree(geoms)
    points = shapely.point_on_surface(faces)
    face_idx, geom_idx = tree.query(points, predicate="within")
    owner: dict[int, int] = {}
    for fi, gi in zip(face_idx.tolist(), geom_idx.tolist()):
        if fi not in owner or items[gi][0] < items[owner[fi]][0]:
            owner[fi] = gi
    pieces: dict[int, list] = {}
    for fi, gi in owner.items():
        pieces.setdefault(gi, []).append(faces[fi])
    out = {k: v for k, v in sorted_data.items() if k not in COVERAGE_KEYS}
    for key in COVERAGE_KEYS:
        out[key] = []
    for gi in sorted(pieces):
        _rank, key, _idx, _g = items[gi]
        merged = shapely.union_all(pieces[gi], grid_size=grid_m)
        for part in shapely.get_parts(merged):
            if part.geom_type == "Polygon" and part.area > 0:
                out[key].append(
                    [[(float(x), float(y)) for x, y in part.exterior.coords]]
                    + [[(float(x), float(y)) for x, y in r.coords] for r in part.interiors]
                )
    return out


POLYGON_FILES = (
    ("buildings", "budovy"),
    ("roads", "silnice (rijbaan)"),
    ("sidewalks", "chodníky (voetpad)"),
    ("stairs", "schody"),
    ("parking", "parkoviště"),
    ("small_paved", "zpevněné plochy (onbegroeid)"),
    ("green", "udržovaná zeleň"),
    ("other_cover", "jiný kryt (les, vřes, louka, pole)"),
    ("garden_cover", "sady a školky"),
)


def clean_polygons(polys: list[Polygon], grid_m: float = GRID_M) -> list[Polygon]:
    """Plochy přichycené na mřížku ``grid_m`` a platné (rozpadlé části zvlášť, bez prázdných)."""
    import shapely
    from shapely.geometry import Polygon as ShPolygon

    out: list[Polygon] = []
    for poly in polys:
        try:
            g = ShPolygon(poly[0], poly[1:])
            if not g.is_valid:
                g = shapely.make_valid(g)
            g = shapely.set_precision(g, grid_m)
        except Exception:  # poškozený objekt se vynechá
            continue
        for part in getattr(g, "geoms", [g]):
            if part.geom_type == "Polygon" and not part.is_empty and part.area > 0:
                out.append(
                    [[(float(x), float(y)) for x, y in part.exterior.coords]]
                    + [[(float(x), float(y)) for x, y in r.coords] for r in part.interiors]
                )
    return out


def _write_polygons(dest: Path, polys: list[Polygon]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not polys:
        dest.unlink(missing_ok=True)
        return
    dest.write_text(
        json.dumps({"polygons": [[[list(p) for p in ring] for ring in poly] for poly in polys]}),
        encoding="utf-8",
    )


def write_dtm_files(
    work_dir: Path, sorted_data: dict[str, list], bounds: tuple[float, float, float, float], *, log: LogFn | None = None
) -> Path:
    """Zapíše roztříděné BGT do ``work_dir/dtm`` ve tvaru přípravy DTM; vrátí cestu k ``fences.json``."""
    from app.pipeline import dtm_import

    paths = {
        "buildings": dtm_import.buildings_path,
        "roads": dtm_import.roads_path,
        "sidewalks": dtm_import.sidewalks_path,
        "stairs": dtm_import.stairs_path,
        "parking": dtm_import.parking_path,
        "small_paved": dtm_import.small_paved_path,
        "green": dtm_import.green_path,
        "other_cover": dtm_import.other_cover_path,
        "garden_cover": dtm_import.garden_cover_path,
    }
    for key, label in POLYGON_FILES:
        polys = [p for p in sorted_data.get(key) or [] if _in_bounds(p, bounds)]
        _write_polygons(paths[key](work_dir), polys)
        if log and polys:
            log(f"BGT {label}: {len(polys)} ploch")
    decks = [d for d in sorted_data.get("decks") or [] if _in_bounds(d["p"], bounds)]
    deck_path = dtm_import.decks_path(work_dir)
    if decks:
        deck_path.write_text(
            json.dumps({"decks": [{"l": d["l"], "t": d["t"], "p": [[list(p) for p in ring] for ring in d["p"]]} for d in decks]}),
            encoding="utf-8",
        )
        if log:
            log(f"BGT mosty: {len(decks)} ploch")
    else:
        deck_path.unlink(missing_ok=True)

    yards = [p for p in sorted_data.get("yards") or [] if _in_bounds(p, bounds)]
    areas = {
        "olive": yards,
        "public": [p for p in sorted_data.get("public") or [] if _in_bounds(p, bounds)],
        "building": [p for p in sorted_data.get("buildings") or [] if _in_bounds(p, bounds)],
        "open": [p for p in (sorted_data.get("green") or []) + (sorted_data.get("other_cover") or []) if _in_bounds(p, bounds)],
    }
    fences = dtm_fences.fences_in_bounds(list(sorted_data.get("fences") or []), bounds)
    merged = dtm_fences.merged_fences(fences)
    inner_dropped = 0
    try:
        merged, inner_dropped = dtm_fences.drop_fences_inside_enclosures(merged)
    except Exception as exc:  # filtr je doplněk
        if log:
            log(f"BGT: filtr plotů uvnitř oplocení přeskočen ({type(exc).__name__}: {exc})")
    fill_rings: list = []
    try:
        fill_rings = dtm_fences.private_enclosure_rings(merged, areas)
    except Exception as exc:  # výplň olivou je doplněk
        if log:
            log(f"BGT: výplň oplocení přeskočena ({type(exc).__name__}: {exc})")
    oriented = False
    try:
        merged = dtm_fences.orient_fences(merged, areas, bounds=bounds, setback_m=dtm_fences.FENCE_SETBACK_M)
        oriented = bool(areas["olive"] or areas["public"])
    except Exception as exc:  # orientace je kosmetika
        if log:
            log(f"BGT: orientace plotů přeskočena ({type(exc).__name__}: {exc})")

    def _poly_json(polys: list[Polygon]) -> list:
        return [[[list(p) for p in ring] for ring in poly] for poly in polys]

    dest = dtm_import.fences_path(work_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(
            {
                "source": "nl_bgt",
                "packages": ["BGT"],
                "source_pieces": len(fences),
                "oriented": oriented,
                "dubious_removed": 0,
                "dubious": [],
                "inner_removed": inner_dropped,
                "incomplete_opl": [],
                "units": [],
                "opl_files": [],
                "areas": {k: _poly_json(v) for k, v in areas.items()},
                "yards": _poly_json(yards),
                "fences": [{"code": code, "coords": [list(p) for p in line]} for code, line in merged],
                "fills": [[list(p) for p in ring] for ring in fill_rings],
            }
        ),
        encoding="utf-8",
    )
    if log:
        log(
            f"BGT ploty: {len(fences)} úseků → {len(merged)} napojených čar, {len(fill_rings)} oplocení k vyplnění; "
            f"soukromé pozemky (erf) jako oliva: {len(yards)}"
        )
    return dest


def prepare_bgt(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log: LogFn | None = None,
    force: bool = False,  # noqa: ARG001 – stejný podpis jako příprava DTM (BGT se necachuje mezi joby)
) -> Path | None:
    """BGT pro výřez → soubory ``work_dir/dtm`` (jako DTM); None, když BGT nic nevrátí."""
    from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514

    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    t0 = time.monotonic()
    build_stats.note_background("dtm", source="nl_bgt")
    data, nbytes = fetch_collections(bounds)
    build_stats.note_bytes("nl_bgt", nbytes, cached=False, key="bgt")
    count = sum(len(v) for v in data.values())
    build_stats.note_background("dtm", packages=len(tile_bounds(bounds)), regions=["NL"], source="nl_bgt", objects=count)
    if log:
        log(
            f"BGT: {count} aktuálních objektů z {len(tile_bounds(bounds))} dlaždic ({nbytes / 1e6:.1f} MB, "
            f"{time.monotonic() - t0:.0f} s)"
        )
    if not count:
        return None
    sorted_data = classify(data)
    try:
        sorted_data = node_coverage(sorted_data)
    except Exception as exc:  # bez uzlování se kreslí z původních ploch (GEOS může v kresbě hlásit chyby)
        if log:
            log(f"BGT: uzlování ploch přeskočeno ({type(exc).__name__}: {exc})")
    return write_dtm_files(work_dir, sorted_data, bounds, log=log)
