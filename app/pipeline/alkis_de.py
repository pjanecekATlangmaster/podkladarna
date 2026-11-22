"""Německé země: ALKIS vereinfacht (katastr AdV) jako obdoba české DTM pro kresbu.

ALKIS vede ve všech zemích stejné zjednodušené schéma (``alkis-vereinfacht`` 2.0 AdV) a země ho dávají přes WFS 2.0
bez klíče. Bereme z něj dvě vrstvy:

- ``Nutzung`` (skutečné využití): ``Straßenverkehr`` je uliční prostor od plotu k plotu (bez dělení na vozovku
  a chodník, obdoba parcel komunikací RÚIAN). Sprint ho kreslí jako ulici v celé šířce stejnou cestou jako ETAK,
  GRB a YLRE (``work_dir/ruian_parcels/komunikace.geojson``). ``Platz`` / pěší zóna jsou pěší plochy, parkoviště
  plochy parkování.
- ``GebaeudeBauwerk`` s ``gebnutzbez = Gebäude``: budovy z katastru (ne části budov ani stavby).

Ostatní využití (bydlení, les, zemědělství) kreslí OSM; ALKIS nemá chodníky, obrubníky ani ploty.
Licence: budovy a parcely jsou od 6/2024 High Value Dataset (EU 2023/138) a země je dávají zdarma, NRW pod
dl-de/zero-2.0, ostatní dl-de/by-2.0 (uvést zdroj). Výsledek se zapíše do ``work_dir/dtm`` jako u BGT nebo ETAK.
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.pipeline import build_stats
from app.pipeline.bgt_nl import clean_polygons, feature_polygons, node_coverage, write_dtm_files
from app.pipeline.etak_ee import write_streets
from app.pipeline.fmzk_at import reproject_features

WORKERS = 2
PAGE_SIZE = 5000
MAX_PAGES = 20
HTTP_TIMEOUT_S = 180
HTTP_RETRIES = 3
MAX_RESPONSE_BYTES = 300_000_000
LAYERS = ("Nutzung", "GebaeudeBauwerk")


@dataclass(frozen=True)
class State:
    code: str  # kód země v countries.yaml
    url: str  # WFS ALKIS vereinfacht
    epsg: int  # nativní souřadnice služby
    label: str  # poskytovatel do logu a sond
    probe_xy: tuple[float, float]  # bod (v ``epsg``) pro lehkou sondu služby
    active: bool = True  # země je v countries.yaml s ``technical_map: de_alkis`` (sonda stavu zdrojů)


# Ověřeno 2026-11-22: GetCapabilities s vrstvou Nutzung a dotaz bboxem (centrum zemského hlavního města nebo Kolína).
STATES: dict[str, State] = {
    s.code: s
    for s in (
        State("DE-NW", "https://www.wfs.nrw.de/geobasis/wfs_nw_alkis_vereinfacht", 25832, "Geobasis NRW", (356310.0, 5644590.0)),
        State("DE-NI", "https://opendata.lgln.niedersachsen.de/doorman/noauth/alkis_wfs_einfach", 25832, "LGLN", (550040.0, 5802670.0)),
        State("DE-SN", "https://geodienste.sachsen.de/aaa/public_alkis/vereinf/wfs", 25833, "GeoSN", (411560.0, 5656330.0)),
        State("DE-TH", "https://www.geoproxy.geoportal-th.de/geoproxy/services/adv_alkis_wfs", 25832, "TLBG", (642440.0, 5649340.0), active=False),  # výšky TH zatím nejsou
        State("DE-BB", "https://isk.geobasis-bb.de/ows/alkis_vereinf_wfs", 25833, "LGB Brandenburg", (368010.0, 5807080.0)),
        State("DE-HE", "https://www.gds.hessen.de/wfs2/aaa-suite/cgi-bin/alkis/vereinf/wfs", 25832, "HVBG", (477260.0, 5551020.0)),
        State(
            "DE-ST",
            "https://www.geodatenportal.sachsen-anhalt.de/wss/service/ST_LVermGeo_ALKIS_WFS_OpenData/guest",
            25832,
            "LVermGeo Sachsen-Anhalt",
            (682000.0, 5777700.0),
        ),
        State("DE-RP", "https://www.geoportal.rlp.de/registry/wfs/519", 25832, "LVermGeo Rheinland-Pfalz", (447750.0, 5538770.0)),
        State("DE-MV", "https://www.geodaten-mv.de/dienste/alkis_wfs_einfach", 25833, "LAiV M-V", (310270.0, 5997320.0)),
    )
}

LogFn = Callable[[str], None]
_GML = "{http://www.opengis.net/gml/3.2}"


def probe_url(state: State) -> str:
    """Lehká sonda služby: jedna budova v malém okně kolem ``probe_xy``."""
    x, y = state.probe_xy
    box = (x - 50.0, y - 50.0, x + 50.0, y + 50.0)
    return wfs_url(state, "GebaeudeBauwerk", box, count=1)


def wfs_url(state: State, layer: str, bounds: tuple[float, float, float, float], start: int = 0, count: int = PAGE_SIZE) -> str:
    xmin, ymin, xmax, ymax = bounds
    query = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": f"ave:{layer}",
        "COUNT": str(count),
        "BBOX": f"{xmin:.1f},{ymin:.1f},{xmax:.1f},{ymax:.1f},urn:ogc:def:crs:EPSG::{state.epsg}",
    }
    if start:
        query["STARTINDEX"] = str(start)
    return f"{state.url}?{urllib.parse.urlencode(query)}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _ring(el) -> list[list[float]]:
    """Souřadnice ``gml:LinearRing`` (``posList`` nebo řada ``pos``), dvojice x, y."""
    pos_list = el.find(f"{_GML}posList")
    if pos_list is not None and pos_list.text:
        dim = int(pos_list.get("srsDimension") or el.get("srsDimension") or 2)
        vals = [float(v) for v in pos_list.text.split()]
    else:
        pts = [p.text.split() for p in el.findall(f"{_GML}pos") if p.text]
        dim = len(pts[0]) if pts else 2
        vals = [float(v) for p in pts for v in p]
    dim = max(2, dim)
    return [[vals[i], vals[i + 1]] for i in range(0, len(vals) - dim + 1, dim)]


def _polygons(geom_el) -> list[list[list[list[float]]]]:
    polys = []
    for poly in list(geom_el.iter(f"{_GML}Polygon")) + list(geom_el.iter(f"{_GML}PolygonPatch")):
        rings = []
        for part in ("exterior", "interior"):
            for bound in poly.findall(f"{_GML}{part}"):
                ring_el = bound.find(f"{_GML}LinearRing")
                if ring_el is not None:
                    ring = _ring(ring_el)
                    if len(ring) >= 4:
                        rings.append(ring)
            if part == "exterior" and not rings:
                break
        if rings:
            polys.append(rings)
    return polys


def parse_features(raw: bytes) -> tuple[list[dict], int]:
    """GML odpověď WFS → ``([GeoJSON objekt], počet členů)``; atributy bez jmenného prostoru (NRW bez, RP / TH s ``ave:``)."""
    root = ET.fromstring(raw)
    if _local(root.tag) == "ExceptionReport":
        text = " ".join(t.strip() for t in root.itertext() if t.strip())
        raise RuntimeError(f"ALKIS WFS: {text[:200]}")
    feats: list[dict] = []
    members = 0
    for member in root:
        if _local(member.tag) != "member":
            continue
        members += 1
        for feat_el in member:
            props: dict[str, str] = {}
            polys: list = []
            for child in feat_el:
                name = _local(child.tag)
                if name == "geometrie":
                    polys = _polygons(child)
                elif len(child) == 0 and child.text and child.text.strip():
                    props[name] = child.text.strip()
            if polys:
                geom = {"type": "MultiPolygon", "coordinates": polys}
                feats.append({"type": "Feature", "geometry": geom, "properties": props, "layer": _local(feat_el.tag)})
    return feats, members


def _get(url: str) -> bytes:
    from app.pipeline.fetch_openzu import USER_AGENT

    last: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"ALKIS: odpověď větší než {MAX_RESPONSE_BYTES // 1_000_000} MB")
            return raw
        except Exception as exc:  # síť / 5xx: krátce počkat a zkusit znovu
            last = exc
            if attempt < HTTP_RETRIES:
                time.sleep(2.0 * (attempt + 1))
    assert last is not None
    raise last


def _fetch_layer(state: State, layer: str, bounds, fetch, log: LogFn | None = None) -> tuple[list[dict], int]:
    feats: list[dict] = []
    nbytes = 0
    for page in range(MAX_PAGES):
        raw = fetch(wfs_url(state, layer, bounds, page * PAGE_SIZE))
        nbytes += len(raw)
        batch, members = parse_features(raw)
        feats.extend(batch)
        if members < PAGE_SIZE:
            break
    else:
        if log:
            log(f"ALKIS {layer}: dosažen limit {MAX_PAGES} stránek po {PAGE_SIZE}, zbytek výřezu jen z OSM")
    return feats, nbytes


def fetch_layers(
    state: State,
    bounds: tuple[float, float, float, float],
    layers=LAYERS,
    *,
    workers: int = WORKERS,
    fetch: Callable[[str], bytes] = _get,
    log: LogFn | None = None,
) -> tuple[dict[str, list[dict]], int]:
    """``{vrstva: [objekt, …]}`` pro obálku výřezu v nativních souřadnicích země (stránkování po ``PAGE_SIZE``)."""
    layers = tuple(layers)
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="alkis-de") as ex:
        results = list(ex.map(lambda layer: _fetch_layer(state, layer, bounds, fetch, log), layers))
    return {layer: feats for layer, (feats, _n) in zip(layers, results)}, sum(n for _f, n in results)


def _norm(value: str | None) -> str:
    """Hodnota katalogu bez diakritiky a mezer (Durynsko píše „Strassenverkehr“, „Flaeche“)."""
    s = (value or "").strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return "".join(ch for ch in s if ch.isalnum())


PEDESTRIAN_BEZ = frozenset({"fussgaengerzone", "marktplatz"})
PARKING_BEZ = frozenset({"parkplatz", "rastplatz"})


def nutzung_target(props: dict) -> str | None:
    """Soubor DTM pro plochu skutečného využití, nebo None (kreslí OSM)."""
    nutzart, bez = _norm(props.get("nutzart")), _norm(props.get("bez"))
    if bez in PEDESTRIAN_BEZ:
        return "sidewalks"
    if nutzart == "strassenverkehr":
        return "parking" if bez in PARKING_BEZ else "streets"
    if nutzart == "platz":
        if bez in PARKING_BEZ:
            return "parking"
        return "sidewalks" if not bez or bez == "platz" else None
    return None


def classify(data: dict[str, list[dict]]) -> dict[str, list]:
    """Objekty roztříděné do souborů DTM + ``streets`` (uliční prostor pro sprint) a ``public``."""
    keys = (
        "buildings", "roads", "sidewalks", "stairs", "parking", "small_paved", "green", "other_cover",
        "garden_cover", "yards", "decks", "fences", "public", "streets",
    )
    out: dict[str, list] = {k: [] for k in keys}
    for f in data.get("GebaeudeBauwerk") or []:
        if _norm((f.get("properties") or {}).get("gebnutzbez")) == "gebaeude":
            out["buildings"].extend(clean_polygons(feature_polygons(f)))
    for f in data.get("Nutzung") or []:
        target = nutzung_target(f.get("properties") or {})
        if target is None:
            continue
        polys = clean_polygons(feature_polygons(f))
        out[target].extend(polys)
        out["public"].extend(polys)
    return out


def state_for(bbox_wgs84: tuple[float, float, float, float]) -> State | None:
    """Země jobu (``country_profile``), jinak země s největším podílem výřezu."""
    from app import countries
    from app.pipeline import country_profile

    country = country_profile.process_country()
    if country is not None and country.code in STATES:
        return STATES[country.code]
    best, share = None, 0.0
    all_countries = countries.load_countries()
    for code, state in STATES.items():
        c = all_countries.get(code)
        s = countries.bbox_share(tuple(bbox_wgs84), c) if c is not None else 0.0
        if s > share:
            best, share = state, s
    return best


def _with_leipzig(sorted_data: dict[str, list], bbox_wgs84, bounds, crs: str, log: LogFn | None) -> dict[str, list]:
    """V Lipsku doplní vozovky, chodníky, stání, schody a ploty z městské DSGK; výpadek nechá jen ALKIS."""
    from app.pipeline import dsgk_leipzig

    if not dsgk_leipzig.intersects(bbox_wgs84):
        return sorted_data
    try:
        return dsgk_leipzig.augment(sorted_data, bounds, crs, log=log)
    except Exception as exc:  # doplněk: bez DSGK zůstává ALKIS
        if log:
            log(f"⚠ DSGK Lipsko nedostupná ({type(exc).__name__}: {exc}), kresba z ALKIS a OSM.")
        return sorted_data


LOG_LABELS = {
    "buildings": "budovy (Gebäude)",
    "roads": "vozovky (Lipsko DSGK)",
    "sidewalks": "pěší zóny a náměstí (Platz, Fußgängerzone; v Lipsku i chodníky DSGK)",
    "stairs": "schody (Lipsko DSGK)",
    "parking": "parkoviště (Parkplatz; v Lipsku i stání DSGK)",
    "small_paved": "ostatní zpevněné plochy",
    "green": "zeleň",
    "other_cover": "jiný kryt",
}


def prepare(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log: LogFn | None = None,
    force: bool = False,  # noqa: ARG001 – stejný podpis jako příprava DTM (data se necachují mezi joby)
) -> Path | None:
    """ALKIS pro výřez → soubory ``work_dir/dtm`` a uliční prostor pro sprint; None mimo podporované země nebo bez dat."""
    from pyproj import Transformer

    from app.pipeline.crs_5514 import CRS_LABEL
    from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514

    state = state_for(bbox_wgs84)
    if state is None:
        if log:
            log("ALKIS: výřez mimo země s otevřeným WFS ALKIS, kresba z OSM.")
        return None
    src = f"EPSG:{state.epsg}"
    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    fetch_box = Transformer.from_crs(CRS_LABEL, src, always_xy=True).transform_bounds(*bounds, densify_pts=21)
    t0 = time.monotonic()
    build_stats.note_background("dtm", source="de_alkis")
    data, nbytes = fetch_layers(state, fetch_box, log=log)
    reproject_features(data, src, CRS_LABEL)
    build_stats.note_bytes("de_alkis", nbytes, cached=False, key="alkis_de")
    count = sum(len(v) for v in data.values())
    build_stats.note_background("dtm", packages=len(LAYERS), regions=[state.code], source="de_alkis", objects=count)
    if log:
        log(f"ALKIS {state.label}: {count} objektů z {len(LAYERS)} vrstev ({nbytes / 1e6:.1f} MB, {time.monotonic() - t0:.0f} s)")
    if not count:
        if log:
            log("ALKIS: ve výřezu nic, kresba z OSM.")
        return None
    sorted_data = classify(data)
    streets = sorted_data.pop("streets")
    if write_streets(work_dir, streets) is not None and log:
        log(f"ALKIS uliční prostor (Straßenverkehr): {len(streets)} ploch – sprint ho kreslí jako ulici v celé šířce")
    if state.code == "DE-SN":
        sorted_data = _with_leipzig(sorted_data, bbox_wgs84, bounds, CRS_LABEL, log)
    try:
        sorted_data = node_coverage(sorted_data)
    except Exception as exc:  # bez uzlování se kreslí z původních ploch
        if log:
            log(f"ALKIS: uzlování ploch přeskočeno ({type(exc).__name__}: {exc})")
    return write_dtm_files(
        work_dir, sorted_data, bounds, log=log, source="de_alkis", label="ALKIS", labels=LOG_LABELS,
        yard_label="soukromé pozemky",
    )
