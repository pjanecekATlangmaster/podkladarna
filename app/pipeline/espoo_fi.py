"""Finsko (Espoo): rejstřík ulic InfStreet a budovy Espoo jako obdoba české DTM, druhé město vedle Helsinek (YLRE).

Espoo vede ulice po částech (``GIS:InfStreet``, WFS 1.1 kartat.espoo.fi bez klíče, ``ows:Fees`` None): vozovky
(``PARTCLASSTEXT`` Ajorata), chodníky, cyklostezky a náměstí (Kevyt liikenne), parkoviště, ostrůvky a schody
(``ADDINFO2TEXT`` Portaat), k tomu budovy (``GIS:Rakennukset``). Uliční prostor od hranice k hranici pozemku
(``GIS:Katualueet``) je za přihlášením (401), proto se pro sprint skládá ze všech částí ulice včetně dělicích pásů.

Server (Tekla) umí jen WFS 1.1 a GML; ``bbox`` bez CRS v EPSG:3879 (východ, sever) – s CRS vrací nic. Plochy mají
oblouky (``gml:Arc``), ty se rozkládají na lomené čáry. Volá ho ``ylre_fi.prepare``, když výřez zasahuje do Espoo.
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Callable

ESPOO_WFS = "https://kartat.espoo.fi/teklaogcweb/wfs.ashx"
SOURCE_CRS = "EPSG:3879"
HTTP_TIMEOUT_S = 180
HTTP_RETRIES = 3
MAX_RESPONSE_BYTES = 300_000_000  # 3 × 3 km ulic ≈ 31 MB
STREETS = "GIS:InfStreet"
BUILDINGS = "GIS:Rakennukset"
LAYERS = (STREETS, BUILDINGS)
AREA_FILE = Path(__file__).resolve().parents[2] / "configs" / "countries" / "fi_cities.geojson"
ARC_STEP_RAD = math.radians(10)  # oblouk po 10°

# USETYPETEXT → soubor DTM (vozovka i lehký provoz). Dělicí pásy (Erotus/Välikaista) bývají zelené, parkové cesty
# (Puistoraitti) kreslí OSM čára – ani jedno se nebere, do uličního prostoru pro sprint ano.
USE_TYPES = {
    "Pääkatu": "roads",
    "Kokoojakatu": "roads",
    "Tontti-/asunto-/liityntä-/pihakatu": "roads",
    "Pysäköintialue": "parking",
    "Liikenteenjakaja": "small_paved",
    "Polkupyöräpysäköinti": "small_paved",
    "Potkulautapysäköinti": "small_paved",
    "Jalkakäytävä": "sidewalks",
    "Yhdistetty pyörätie ja jalkakäytävä": "sidewalks",
    "Pyörätie": "sidewalks",
    "Tori/Aukio": "sidewalks",
}
STREET_SPACE_SKIP = frozenset({"Puistoraitti"})
STAIRS_INFO = "Portaat"
UNDER = "Under"  # podchody a tunely

LogFn = Callable[[str], None]
GML = "{http://www.opengis.net/gml}"


@lru_cache(maxsize=1)
def _area_rings() -> tuple:
    """Vnější obrysy Espoo z ``fi_cities.geojson`` (prvek ``city: Espoo``) ve tvaru ``countries.rings_share``."""
    data = json.loads(AREA_FILE.read_text(encoding="utf-8"))
    rings = []
    for f in data["features"]:
        if f["properties"].get("city") != "Espoo":
            continue
        geom = f["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        rings.extend(tuple((float(x), float(y)) for x, y in poly[0]) for poly in polys)
    return tuple(rings)


def intersects(bbox_wgs84) -> bool:
    from app.countries import rings_share

    return rings_share(tuple(float(v) for v in bbox_wgs84), _area_rings()) > 0.0


def wfs_url(layer: str, bounds: tuple[float, float, float, float]) -> str:
    """GetFeature WFS 1.1 vrstvy v obálce EPSG:3879 (``bbox`` bez CRS: s ním server nic nevrátí)."""
    x0, y0, x1, y1 = bounds
    query = {
        "service": "WFS",
        "version": "1.1.0",
        "request": "GetFeature",
        "typeName": layer,
        "bbox": f"{x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f}",
    }
    return f"{ESPOO_WFS}?{urllib.parse.urlencode(query)}"


def probe_url() -> str:
    """Lehký dotaz pro stav zdrojů: budovy v okně 100 × 100 m v Tapiole."""
    return wfs_url(BUILDINGS, (25_490_950.0, 6_675_150.0, 25_491_050.0, 6_675_250.0))


def _get(url: str) -> bytes:
    from app.pipeline.fetch_openzu import USER_AGENT

    last: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"Espoo WFS: odpověď větší než {MAX_RESPONSE_BYTES // 1_000_000} MB")
            return raw
        except Exception as exc:  # síť / 5xx: krátce počkat a zkusit znovu
            last = exc
            if attempt < HTTP_RETRIES:
                time.sleep(2.0 * (attempt + 1))
    assert last is not None
    raise last


def _points(el) -> list[tuple[float, float]]:
    """Body ``gml:posList`` nebo řady ``gml:pos`` přímo pod prvkem."""
    pos_list = el.find(f"{GML}posList")
    if pos_list is not None and pos_list.text:
        vals = [float(v) for v in pos_list.text.split()]
        dim = int(pos_list.get("srsDimension") or el.get("srsDimension") or 2)
        return [(vals[i], vals[i + 1]) for i in range(0, len(vals) - dim + 1, dim)]
    out = []
    for pos in el.findall(f"{GML}pos"):
        vals = (pos.text or "").split()
        if len(vals) >= 2:
            out.append((float(vals[0]), float(vals[1])))
    return out


def arc_points(p0, p1, p2) -> list[tuple[float, float]]:
    """Oblouk kružnice přes tři body jako lomená čára (po ``ARC_STEP_RAD``); přímka, když jsou body v řadě."""
    (ax, ay), (bx, by), (cx, cy) = p0, p1, p2
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return [p0, p1, p2]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, a1, a2 = (math.atan2(y - uy, x - ux) for x, y in (p0, p1, p2))

    def ccw_span(a, b):
        return (b - a) % (2 * math.pi)

    if ccw_span(a0, a1) <= ccw_span(a0, a2):
        span = ccw_span(a0, a2)  # proti směru hodinových ručiček přes prostřední bod
    else:
        span = -ccw_span(a2, a0)
    n = max(2, int(abs(span) / ARC_STEP_RAD) + 1)
    pts = [(ux + r * math.cos(a0 + span * i / n), uy + r * math.sin(a0 + span * i / n)) for i in range(n)]
    return pts + [p2]


def _ring(el) -> list[tuple[float, float]]:
    """``gml:LinearRing`` nebo ``gml:Ring`` ze segmentů (``LineStringSegment``, ``Arc``) jako uzavřený seznam bodů."""
    tag = el.tag.removeprefix(GML)
    if tag == "LinearRing":
        pts = _points(el)
    else:
        pts = []
        for seg in el.iter():
            name = seg.tag.removeprefix(GML)
            if name == "LineStringSegment":
                part = _points(seg)
            elif name in ("Arc", "ArcString"):
                raw = _points(seg)
                part = []
                for i in range(0, len(raw) - 2, 2):
                    part.extend(arc_points(raw[i], raw[i + 1], raw[i + 2])[(1 if part else 0):])
            else:
                continue
            pts.extend(part[1:] if pts and part and part[0] == pts[-1] else part)
    if len(pts) >= 3 and pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts if len(pts) >= 4 else []


def _polygons(geom_el) -> list[list[list[tuple[float, float]]]]:
    polys = []
    for el in geom_el.iter():
        if el.tag.removeprefix(GML) not in ("Polygon", "PolygonPatch"):
            continue
        rings = []
        for part in ("exterior", "interior"):
            for holder in el.findall(f"{GML}{part}"):
                for ring_el in holder:
                    ring = _ring(ring_el)
                    if ring:
                        rings.append(ring)
                    elif part == "exterior":
                        rings = []
        if rings:
            polys.append(rings)
    return polys


def parse_features(raw: bytes) -> list[dict]:
    """GML 3.1 odpověď → GeoJSON-like objekty (``MultiPolygon``, vlastnosti z listových prvků)."""
    root = ET.fromstring(raw)
    if root.tag.endswith("ExceptionReport"):
        text = " ".join(t.strip() for t in root.itertext() if t.strip())
        raise RuntimeError(f"Espoo WFS: {text[:200]}")
    feats = []
    for member in root.iter(f"{GML}featureMember"):
        for feat_el in member:
            props: dict[str, str] = {}
            polys: list = []
            for child in feat_el:
                if len(child):
                    polys.extend(_polygons(child))
                elif child.text and child.text.strip():
                    props[child.tag.split("}")[-1]] = child.text.strip()
            if polys:
                feats.append({"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": polys}, "properties": props})
    return feats


def fetch_layers(
    bounds: tuple[float, float, float, float], layers=LAYERS, *, fetch: Callable[[str], bytes] = _get
) -> tuple[dict[str, list[dict]], int]:
    """``{vrstva: [objekt, …]}`` pro obálku v EPSG:3879 (server nestránkuje, výřez sprintu je pod limitem)."""
    data, nbytes = {}, 0
    for layer in layers:
        raw = fetch(wfs_url(layer, bounds))
        nbytes += len(raw)
        data[layer] = parse_features(raw)
    return data, nbytes


def street_target(props: dict) -> str | None:
    """Soubor DTM pro část ulice; None = nebere se (dělicí pás, parková cesta, podchod)."""
    if props.get("PARTLAYER") == UNDER:
        return None
    if props.get("ADDINFO2TEXT") == STAIRS_INFO and props.get("PARTCLASSTEXT") != "Ajorata":
        return "stairs"
    target = USE_TYPES.get(str(props.get("USETYPETEXT") or ""))
    if target is None and props.get("PARTCLASSTEXT") == "Ajorata" and props.get("USETYPETEXT") not in ("Erotus/Välikaista",):
        return "roads"  # jiná vozovka (sběrná, průmyslová …)
    return target


def classify(data: dict[str, list[dict]]) -> dict[str, list]:
    """Objekty Espoo do souborů DTM + ``streets`` (uliční prostor pro sprint) ve tvaru ``ylre_fi.classify``."""
    from app.pipeline.bgt_nl import clean_polygons, feature_polygons

    keys = ("buildings", "roads", "sidewalks", "stairs", "parking", "small_paved", "streets")
    out: dict[str, list] = {k: [] for k in keys}
    for f in data.get(BUILDINGS) or []:
        out["buildings"].extend(feature_polygons(f))
    for f in data.get(STREETS) or []:
        props = f.get("properties") or {}
        polys = feature_polygons(f)
        target = street_target(props)
        if target:
            out[target].extend(polys)
        if props.get("PARTLAYER") != UNDER and props.get("USETYPETEXT") not in STREET_SPACE_SKIP:
            out["streets"].extend(polys)
    return {k: clean_polygons(v) for k, v in out.items()}
