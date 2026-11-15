"""AV (amtliche Vermessung, Švýcarsko) jako obdoba české DTM pro kresbu.

Petr 2026-11-14/15: obdoba DTM je při zařazení země priorita, „zejména pro sprint … dává přesná data ohledně
chodníků a silnic“. Ve Švýcarsku je to úřední vyměřování AV (model DM.01-AV-CH): pokryv půdy (Bodenbedeckung,
vrstva ``LCSF``) s budovami, silnicemi a cestami, chodníky (Trottoir), ostrůvky a zahradami, a jednotlivé objekty
(Einzelobjekte, ``SOSF``) se schody a mosty. Ploty AV nevede (kreslí je OSM).

Data jsou ze sdílené WFS kantonů geodienste.ch (``AV_WFS``, GeoJSON v EPSG:2056, bez klíče). Ve 21 kantonech volně,
v JU, LU, NE, NW, OW a VD jen se smlouvou – služba tam vrací prázdnou odpověď a kreslí se jen z OSM.
Licence kantonů: většinou „Freie Nutzung, Quellenangabe ist Pflicht“ (citace v ``configs/countries.yaml``).

Výsledek se zapíše do ``work_dir/dtm`` stejně jako BGT (``bgt_nl.write_dtm_files``): uzlování ploch
(``bgt_nl.node_coverage``) a soubory přípravy DTM, takže kresba jde stejnou cestou jako v Česku.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable

from app.pipeline import build_stats
from app.pipeline.bgt_nl import (
    Polygon,
    clean_polygons,
    feature_polygons,
    node_coverage,
    tile_bounds,
    write_dtm_files,
)

AV_WFS = "https://geodienste.ch/db/av_0/deu"
AV_CRS = "EPSG:2056"
SOURCE_LABEL = "AV (geodienste.ch)"
LAYERS = ("LCSF", "SOSF")
TILE_M = 1000.0  # 1 km² ≈ 2–4 MB GeoJSON v centru Curychu, ~1 s
WORKERS = 4
HTTP_TIMEOUT_S = 120
HTTP_RETRIES = 2
MAX_RESPONSE_BYTES = 300_000_000  # dlaždice 1 km² má jednotky MB; víc je chyba služby

# Bodenbedeckung (LCSF.Art) → soubor DTM (CZ obdoba).
BUILDING_ART = frozenset({"Gebaeude"})
ROAD_ART = frozenset({"Strasse_Weg"})  # silnice i cesty (AV je nerozlišuje, povrch nevede)
SIDEWALK_ART = frozenset({"Trottoir"})
SMALL_PAVED_ART = frozenset({"Verkehrsinsel", "uebrige_befestigte"})  # ostrůvky, nádvoří, parkoviště, náměstí
GARDEN_ART = "Gartenanlage"  # zahrady i parky: u budov → oliva, velké a bez budov → zeleň (``split_gardens``)
GREEN_ART = frozenset({"uebrige_humusierte"})
GARDEN_COVER_ART = frozenset({"Reben", "uebrige_Intensivkultur"})
NATURE_ART = frozenset(
    {
        "Acker_Wiese_Weide",
        "geschlossener_Wald",
        "uebrige_bestockte",
        "Wytweide_dicht",
        "Wytweide_offen",
        "Hoch_Flachmoor",
        "Schilfguertel",
    }
)
PUBLIC_ART = ROAD_ART | SIDEWALK_ART | frozenset({"Verkehrsinsel"})
# Einzelobjekte (SOSF.Art)
STAIRS_ART = frozenset({"wichtige_Treppe"})
BRIDGE_ART = frozenset({"Bruecke_Passerelle"})

# Zahrada (Gartenanlage) je soukromý pozemek (oliva 520), když se dotýká budovy a na jednu sousední budovu připadá
# nejvýš ``GARDEN_PER_BUILDING_M2`` (Curych 4 km²: medián zahrady 281 m², řadové domy 8 956 m² / 48 budov);
# jinak park nebo zeleň sídliště (udržovaná zeleň, ne oliva).
GARDEN_TOUCH_M = 0.5
GARDEN_PER_BUILDING_M2 = 2000.0
# Silnice a cesty AV v přírodě: AV nevede povrch, lesní a polní cesty by ve sprintu vyšly jako zpevněná plocha 501.
# Část plochy do ``RURAL_NEAR_M`` od přírodního krytu (les, louka, pole) a dál než ``SETTLEMENT_M`` od zástavby
# (budovy, zahrady, ostatní zpevněné) se nebere – kreslí ji čára z OSM podle povrchu.
RURAL_NEAR_M = 3.0
SETTLEMENT_M = 15.0

LogFn = Callable[[str], None]


def items_url(layer: str, bounds: tuple[float, float, float, float]) -> str:
    query = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": f"ms:{layer}",
        "BBOX": ",".join(f"{v:.1f}" for v in bounds) + ",urn:ogc:def:crs:EPSG::2056",
        "OUTPUTFORMAT": "geojson",
    }
    return f"{AV_WFS}?{urllib.parse.urlencode(query)}"


def _get_json(url: str) -> tuple[dict, int]:
    from app.pipeline.fetch_openzu import USER_AGENT

    last: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"AV: odpověď větší než {MAX_RESPONSE_BYTES // 1_000_000} MB")
            return json.loads(raw), len(raw)
        except Exception as exc:  # síť / 5xx: krátce počkat a zkusit znovu
            last = exc
            if attempt < HTTP_RETRIES:
                time.sleep(2.0 * (attempt + 1))
    assert last is not None
    raise last


def fetch_tile(layer: str, bounds: tuple[float, float, float, float]) -> tuple[list[dict], int]:
    data, nbytes = _get_json(items_url(layer, bounds))
    return list(data.get("features") or []), nbytes


def _feature_key(feat: dict) -> str:
    """AV ve WFS nemá identifikátor; objekt z více dlaždic má stejnou geometrii i atributy."""
    raw = json.dumps([feat.get("properties"), feat.get("geometry")], sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def fetch_layers(
    bounds: tuple[float, float, float, float],
    layers: Iterable[str] = LAYERS,
    *,
    tile_m: float = TILE_M,
    workers: int = WORKERS,
    fetch: Callable[[str, tuple], tuple[list[dict], int]] = fetch_tile,
) -> tuple[dict[str, list[dict]], int]:
    """``{vrstva: [objekt, …]}`` pro výřez (dlaždice souběžně, duplicity z přesahu dlaždic sloučené)."""
    layers = tuple(layers)
    jobs = [(layer, t) for layer in layers for t in tile_bounds(bounds, tile_m)]
    out: dict[str, dict[str, dict]] = {layer: {} for layer in layers}
    total = 0
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="av") as ex:
        for (layer, _tile), (feats, nbytes) in zip(jobs, ex.map(lambda j: fetch(*j), jobs)):
            total += nbytes
            for f in feats:
                out[layer].setdefault(_feature_key(f), f)
    return {layer: list(v.values()) for layer, v in out.items()}, total


def _shapes(polys: list[Polygon]) -> list:
    from shapely.geometry import Polygon as ShPolygon

    out = []
    for p in polys:
        try:
            g = ShPolygon(p[0], p[1:])
        except (ValueError, IndexError):
            continue
        if g.is_valid and not g.is_empty:
            out.append(g)
    return out


def _to_polys(geom) -> list[Polygon]:
    import shapely

    out: list[Polygon] = []
    for part in shapely.get_parts(geom):
        if part.geom_type == "Polygon" and not part.is_empty and part.area > 0:
            out.append(
                [[(float(x), float(y)) for x, y in part.exterior.coords]]
                + [[(float(x), float(y)) for x, y in r.coords] for r in part.interiors]
            )
    return out


def split_gardens(gardens: list[Polygon], buildings: list[Polygon]) -> tuple[list[Polygon], list[Polygon]]:
    """(soukromé zahrady → oliva, parky a zeleň sídlišť → udržovaná zeleň) podle sousedních budov."""
    import shapely

    if not gardens:
        return [], []
    build = _shapes(buildings)
    tree = shapely.STRtree(build) if build else None
    yards: list[Polygon] = []
    green: list[Polygon] = []
    for poly, g in zip(gardens, [_shapes([p]) for p in gardens]):
        if not g:
            continue
        n = len(tree.query(g[0].buffer(GARDEN_TOUCH_M), predicate="intersects")) if tree is not None else 0
        (yards if n and g[0].area / n <= GARDEN_PER_BUILDING_M2 else green).append(poly)
    return yards, green


def drop_rural_ways(roads: list[Polygon], nature: list[Polygon], settlement: list[Polygon]) -> list[Polygon]:
    """Plochy silnic a cest bez úseků v přírodě (u lesa, louky, pole a daleko od zástavby)."""
    import shapely

    nat = _shapes(nature)
    if not roads or not nat:
        return roads
    rural = shapely.union_all([g.buffer(RURAL_NEAR_M) for g in nat], grid_size=0.01)
    built = _shapes(settlement)
    if built:
        rural = shapely.difference(rural, shapely.union_all([g.buffer(SETTLEMENT_M) for g in built], grid_size=0.01), grid_size=0.01)
    if rural.is_empty:
        return roads
    shapely.prepare(rural)
    out: list[Polygon] = []
    for poly, g in zip(roads, [_shapes([p]) for p in roads]):
        if not g:
            continue
        if not rural.intersects(g[0]):
            out.append(poly)
            continue
        out.extend(_to_polys(shapely.difference(g[0], rural, grid_size=0.01)))
    return out


def classify(data: dict[str, list[dict]]) -> dict[str, list]:
    """Objekty AV roztříděné do souborů DTM (``buildings``, ``roads``, …) + ``decks``, ``yards``, ``public``."""
    from app.pipeline import dtm_bridges

    keys = (
        "buildings", "roads", "sidewalks", "stairs", "parking", "small_paved", "green", "other_cover",
        "garden_cover", "yards", "decks", "fences", "public",
    )
    out: dict[str, list] = {k: [] for k in keys}
    gardens: list[Polygon] = []

    for f in data.get("LCSF") or []:
        art = str((f.get("properties") or {}).get("Art") or "")
        polys = clean_polygons(feature_polygons(f))
        if art in PUBLIC_ART:
            out["public"].extend(polys)
        if art in BUILDING_ART:
            out["buildings"].extend(polys)
        elif art in ROAD_ART:
            out["roads"].extend(polys)
        elif art in SIDEWALK_ART:
            out["sidewalks"].extend(polys)
        elif art in SMALL_PAVED_ART:
            out["small_paved"].extend(polys)
        elif art == GARDEN_ART:
            gardens.extend(polys)
        elif art in GREEN_ART:
            out["green"].extend(polys)
        elif art in GARDEN_COVER_ART:
            out["garden_cover"].extend(polys)
        elif art in NATURE_ART:
            out["other_cover"].extend(polys)
    for f in data.get("SOSF") or []:
        art = str((f.get("properties") or {}).get("Art") or "")
        if art in STAIRS_ART:
            out["stairs"].extend(clean_polygons(feature_polygons(f)))
        elif art in BRIDGE_ART:
            out["decks"].extend(
                {"l": 1, "t": dtm_bridges.STRUCTURE_TYPE, "p": p} for p in clean_polygons(feature_polygons(f))
            )

    yards, park = split_gardens(gardens, out["buildings"])
    out["yards"] = yards
    out["green"].extend(park)
    out["roads"] = drop_rural_ways(
        out["roads"], out["other_cover"], out["buildings"] + gardens + out["small_paved"]
    )
    return out


# Popisy souborů v logu (místo názvů BGT v ``bgt_nl.POLYGON_FILES``).
LOG_LABELS = {
    "buildings": "budovy (Gebäude)",
    "roads": "silnice a cesty (Strasse_Weg)",
    "sidewalks": "chodníky (Trottoir)",
    "stairs": "schody (wichtige Treppe)",
    "small_paved": "ostrůvky a ostatní zpevněné plochy",
    "green": "zeleň a parky",
    "other_cover": "jiný kryt (les, louka, pole)",
    "garden_cover": "vinice a sady",
}


def prepare(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log: LogFn | None = None,
    force: bool = False,  # noqa: ARG001 – stejný podpis jako příprava DTM (AV se necachuje mezi joby)
) -> Path | None:
    """AV pro výřez → soubory ``work_dir/dtm`` (jako DTM); None, když AV nic nevrátí (kanton bez volných dat)."""
    from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514

    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    t0 = time.monotonic()
    build_stats.note_background("dtm", source="ch_av")
    data, nbytes = fetch_layers(bounds)
    build_stats.note_bytes("ch_av", nbytes, cached=False, key="av")
    count = sum(len(v) for v in data.values())
    tiles = len(tile_bounds(bounds, TILE_M))
    build_stats.note_background("dtm", packages=tiles, regions=["CH"], source="ch_av", objects=count)
    if log:
        log(f"AV: {count} objektů z {tiles} dlaždic ({nbytes / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s)")
    if not count:
        if log:
            log("AV: výřez bez volně dostupných dat (kanton bez otevřené AV nebo mimo Švýcarsko), kresba z OSM.")
        return None
    sorted_data = classify(data)
    try:
        sorted_data = node_coverage(sorted_data)
    except Exception as exc:  # bez uzlování se kreslí z původních ploch
        if log:
            log(f"AV: uzlování ploch přeskočeno ({type(exc).__name__}: {exc})")
    return write_dtm_files(
        work_dir, sorted_data, bounds, log=log, source="ch_av", label="AV", labels=LOG_LABELS,
        yard_label="zahrady u budov (Gartenanlage)",
    )
