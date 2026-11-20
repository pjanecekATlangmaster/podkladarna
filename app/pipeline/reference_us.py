"""Služby pro USA (zahraniční build): mračno LiDAR USGS 3DEP jako EPT na AWS a index projektů WESM.

Bez klíče a bez e-mailu, public domain (data USGS, uvádí se „Courtesy of the U.S. Geological Survey“):

- Index WESM (Work Unit Extent Spatial Metadata) na index.nationalmap.gov: polygony projektů snímkování
  s kvalitou (``ql``: QL 0 – QL 3, Other) a obdobím. Bere se jen QL 0 – QL 2 (≥ 2 body/m², přesnost výšky ≤ 10 cm);
  starší projekty „Other“ a QL 3 se nepoužijí a výřez bez nich hlásí, že data nejsou.
- Mračno 3DEP v bucketu ``usgs-lidar-public`` (AWS Open Data) ve formátu Entwine Point Tile: oktalový strom
  souborů LAZ v EPSG:3857 s výškami NAVD88 v metrech (i u projektů původně ve stopách – ověřeno proti DEM 3DEP).
  Jméno složky je jméno projektu WESM, u starších s předponou ``USGS_LPC_`` a příponou roku zpracování; seznam
  složek se jednou za ``EPT_INDEX_MAX_AGE_DAYS`` načte z výpisu bucketu do cache. Novější projekty (zhruba 2024+)
  v bucketu ještě nemusí být – pak se vezme starší projekt téhož místa.
- Obrysy dat v bucketu (``resources.geojson`` z repozitáře hobuinc/usgs-lidar, který bucket plní): skutečná stopa
  každého mračna a počet bodů. Výběr vychází z nich, protože část mračen ve WESM není (město New York 2017, celostátní
  projekty IA / KY / MN, GA Central…) a obrys WESM bývá užší než data (Miami). Kvalita: projekt WESM QL 0 – QL 2,
  mračno mimo WESM jen s hustotou aspoň ``US_MIN_DENSITY`` bodů/m² (QL 2 má v bucketu 10. percentil 3,0, QL 3
  a „Other“ 90. percentil 2,8 a 3,5). Bez obrysů (výpadek, prázdná cache) se volí jen podle WESM a výpisu bucketu.

Souřadnice jobu: NAD83 / UTM zóna podle středu výřezu (EPSG:269xx, ``utm_epsg``). Stav služeb hlídá
``app.source_status`` (sondy ``us_index`` a ``us_elevation``).
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

US_EPT_BUCKET = "https://s3-us-west-2.amazonaws.com/usgs-lidar-public"
US_WESM_LAYER = "https://index.nationalmap.gov/arcgis/rest/services/3DEPElevationIndex/MapServer/24"
US_WESM_QUERY = f"{US_WESM_LAYER}/query"
# Sondy: popis vrstvy indexu (malé JSON) a ept.json jednoho projektu (~3 kB).
US_INDEX_PROBE = f"{US_WESM_LAYER}?f=json"
US_ELEVATION_PROBE = f"{US_EPT_BUCKET}/NY_SE4County_1_A22/ept.json"
US_QUALITY_LEVELS = ("QL 0", "QL 1", "QL 2")
US_EPT_BOUNDARIES = "https://raw.githubusercontent.com/hobuinc/usgs-lidar/master/boundaries/resources.geojson"
US_MIN_DENSITY = 3.0  # bodů/m² u mračna, které WESM nezná (počet bodů / plocha obrysu)
FOOTPRINT_SIMPLIFY_DEG = 0.0005  # ~50 m, jako obrys WESM
# Šablony The National Map (public domain): ortofoto USGS Imagery (NAIP) a USGS Topo; WMS 1.3.0 přijme i NAD83 / UTM.
US_IMAGERY_WMS = "https://basemap.nationalmap.gov/arcgis/services/USGSImageryOnly/MapServer/WMSServer"
US_TOPO_WMS = "https://basemap.nationalmap.gov/arcgis/services/USGSTopo/MapServer/WMSServer"
US_WMS_LAYER = "0"
EPT_INDEX_MAX_AGE_DAYS = 30
EPT_INDEX_MISS_REFRESH_DAYS = 2  # projekt WESM chybí v seznamu: seznam obnovit, je-li starší
HTTP_TIMEOUT_S = 60
HTTP_RETRIES = 3  # opakování přechodné chyby (5xx, 429, síť) jednoho dotazu
_INDEX_LOCK = threading.Lock()
_FOOTPRINT_LOCK = threading.Lock()
_FOOTPRINT_FAILED: dict[str, float] = {}  # cache → čas posledního neúspěšného stažení obrysů (monotonic)
FOOTPRINT_RETRY_S = 600.0
_FOOTPRINTS: dict = {}  # v procesu: {"fetched": čas cache, "items": [Footprint], "tree": STRtree}
_PENDING: dict[tuple, list[str]] = {}  # výřez → projekty QL 0 – QL 2, jejichž mračno v bucketu ještě není


def utm_epsg(lon: float, lat: float) -> int:
    """EPSG NAD83 / UTM zóny bodu (26901–26923; Havaj 4–5, kontinentální USA 10–19)."""
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    return 26900 + max(1, min(23, zone))


def utm_label(bbox_wgs84) -> str:
    w, s, e, n = (float(v) for v in bbox_wgs84)
    return f"EPSG:{utm_epsg((w + e) / 2.0, (s + n) / 2.0)}"


@dataclass(frozen=True)
class Workunit:
    """Projekt snímkování z WESM: jméno, kvalita, konec snímkování (rok) a obrys (WGS84, shapely)."""

    name: str
    ql: str
    year: int
    geometry: object


def _get(url: str, timeout: float = HTTP_TIMEOUT_S, *, retries: int = HTTP_RETRIES, wait_s: float = 2.0) -> bytes:
    """GET s opakováním přechodných chyb (5xx, 429, síť): výřez čte stovky až tisíce uzlů EPT a jedno
    „SlowDown“ z S3 by jinak shodilo celý job."""
    import urllib.error

    from app.pipeline.fetch_openzu import USER_AGENT

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if attempt >= retries or not (exc.code == 429 or exc.code >= 500):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt >= retries:
                raise
        time.sleep(wait_s * (attempt + 1))
    raise AssertionError("unreachable")


def _esri_polygon(geom: dict):
    """Polygon ArcGIS (rings, vnější i díry) → shapely; orientaci kruhů neřeší, díry odečte podle vnoření."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    polys = [Polygon(r) for r in (geom or {}).get("rings") or [] if len(r) >= 4]
    polys = [p if p.is_valid else p.buffer(0) for p in polys]
    if not polys:
        return None
    polys.sort(key=lambda p: -p.area)
    out = []
    for p in polys:
        holder = next((o for o in out if o.contains(p.representative_point())), None)
        if holder is None:
            out.append(p)
        else:
            out[out.index(holder)] = holder.difference(p)
    return unary_union(out)


def parse_wesm(data: dict, qualities=US_QUALITY_LEVELS) -> list[Workunit]:
    """Odpověď dotazu WESM → projekty QL 0 – QL 2 (``qualities`` None = všechny), od nejnovějšího."""
    out = []
    for f in data.get("features") or []:
        a = f.get("attributes") or {}
        ql = str(a.get("ql") or "")
        if (qualities is not None and ql not in qualities) or not a.get("workunit"):
            continue
        geom = _esri_polygon(f.get("geometry") or {})
        if geom is None or geom.is_empty:
            continue
        end = a.get("collect_end") or a.get("collect_start") or 0
        year = time.gmtime(float(end) / 1000.0).tm_year if end else 0
        out.append(Workunit(str(a["workunit"]), ql, year, geom))
    out.sort(key=lambda w: (-w.year, w.ql))
    return out


def _wesm_query(params: dict, fetch) -> dict:
    base = {"outSR": "4326", "outFields": "workunit,ql,collect_start,collect_end", "returnGeometry": "true",
            "maxAllowableOffset": "0.0005", "f": "json"}
    data = json.loads(fetch(f"{US_WESM_QUERY}?{urllib.parse.urlencode({**base, **params})}"))
    if "error" in data:
        raise RuntimeError(f"USA: index WESM vrátil chybu {data['error'].get('code')}")
    return data


def wesm_workunits(bbox_wgs84, *, fetch=_get, qualities=US_QUALITY_LEVELS) -> list[Workunit]:
    """Projekty (výchozí QL 0 – QL 2, None = všechny) zasahující do výřezu (obrys ~50 m), od nejnovějšího."""
    w, s, e, n = (float(v) for v in bbox_wgs84)
    params = {
        "geometry": f"{w},{s},{e},{n}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "where": "ql IN ('QL 0','QL 1','QL 2')" if qualities is not None else "1=1",
    }
    return parse_wesm(_wesm_query(params, fetch), qualities)


def wesm_by_name(workunits: list[str], *, fetch=_get) -> dict[str, Workunit]:
    """Projekty WESM podle jména (bez ohledu na velikost písmen), libovolná kvalita: klíč ``_norm(jméno)``."""
    names = sorted({re.sub(r"[^A-Za-z0-9_\-]", "", n).upper() for n in workunits if n})
    if not names:
        return {}
    where = "UPPER(workunit) IN ({})".format(",".join(f"'{n}'" for n in names))
    data = _wesm_query({"where": where, "returnGeometry": "false"}, fetch)
    out = {}
    for f in data.get("features") or []:
        a = f.get("attributes") or {}
        if not a.get("workunit"):
            continue
        end = a.get("collect_end") or a.get("collect_start") or 0
        year = time.gmtime(float(end) / 1000.0).tm_year if end else 0
        out[_norm(str(a["workunit"]))] = Workunit(str(a["workunit"]), str(a.get("ql") or ""), year, None)
    return out


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def ept_workunit(name: str) -> str:
    """Jméno projektu WESM ze složky EPT: bez ``USGS_LPC_`` / ``USGS_Lidar_Point_Cloud_`` a ``_LAS_<rok>``."""
    m = re.fullmatch(r"(?:USGS_LPC_|USGS_Lidar_Point_Cloud_)(.+?)(?:_LAS_\d{4})?", name, re.IGNORECASE)
    return m.group(1) if m else name


def _name_year(name: str) -> int:
    """Rok snímkování ze jména mračna mimo WESM (``GA_Central_1_2018``, ``UT_WestEast_1_B22``), jinak 0."""
    m = re.search(r"(?<!\d)(20\d{2}|19\d{2})(?!\d)", name) or re.search(r"_[A-D](\d{2})$", name)
    if not m:
        return 0
    v = int(m.group(1))
    return v if v > 1900 else 2000 + v


def _cache_dir() -> Path:
    from app import settings

    return settings.CACHE_DIR / "us_3dep"


def list_bucket(*, fetch=_get) -> list[str]:
    """Jména složek EPT v bucketu (výpis S3 po 1000)."""
    names: list[str] = []
    token = None
    for _page in range(50):
        params = {"list-type": "2", "delimiter": "/", "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        text = fetch(f"{US_EPT_BUCKET}/?{urllib.parse.urlencode(params)}").decode("utf-8", "replace")
        names += re.findall(r"<Prefix>([^<]+)/</Prefix>", text)
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", text)
        if not m:
            break
        token = m.group(1)
    return names


def ept_names(*, max_age_days: float = EPT_INDEX_MAX_AGE_DAYS, fetch=_get) -> list[str]:
    """Seznam složek EPT z cache, starší než ``max_age_days`` se načte znovu (při výpadku zůstane starý)."""
    path = _cache_dir() / "ept_index.json"
    with _INDEX_LOCK:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = {}
        if cached.get("names") and time.time() - float(cached.get("fetched") or 0) < max_age_days * 86400:
            return list(cached["names"])
        try:
            names = list_bucket(fetch=fetch)
        except Exception:  # noqa: BLE001 – výpadek výpisu: poslouží starý seznam
            if cached.get("names"):
                return list(cached["names"])
            raise
        if not names:
            return list(cached.get("names") or [])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps({"fetched": time.time(), "names": names}), encoding="utf-8")
        tmp.replace(path)
        return names


def match_ept(workunit: str, names: list[str]) -> str | None:
    """Složka EPT projektu: stejné jméno, jinak ``USGS_LPC_<projekt>`` případně s ``_LAS_<rok>`` (nejnovější)."""
    if workunit in names:
        return workunit
    pattern = re.compile(rf"USGS_LPC_{re.escape(workunit)}(_LAS_\d{{4}})?", re.IGNORECASE)
    hits = sorted(n for n in names if pattern.fullmatch(n))
    return hits[-1] if hits else None


def ept_for(workunit: str, *, fetch=_get) -> str | None:
    name = match_ept(workunit, ept_names(fetch=fetch))
    if name is None:
        name = match_ept(workunit, ept_names(max_age_days=EPT_INDEX_MISS_REFRESH_DAYS, fetch=fetch))
    return name


@dataclass(frozen=True)
class Footprint:
    """Obrys mračna v bucketu (WGS84, shapely), počet bodů a hustota (bodů/m²)."""

    name: str
    count: int
    density: float
    geometry: object


def parse_footprints(data: dict) -> list[dict]:
    """``resources.geojson`` → [{name, count, density, geometry (GeoJSON zjednodušená ~50 m)}]."""
    from pyproj import Geod
    from shapely.geometry import mapping, shape

    geod = Geod(ellps="WGS84")
    out = []
    for f in data.get("features") or []:
        p = f.get("properties") or {}
        name, count = str(p.get("name") or ""), int(p.get("count") or 0)
        try:
            geom = shape(f.get("geometry"))
        except Exception:  # noqa: BLE001 – vadný obrys jednoho mračna přeskočit
            continue
        if not name or geom.is_empty:
            continue
        area = abs(geod.geometry_area_perimeter(geom)[0])
        simple = geom.simplify(FOOTPRINT_SIMPLIFY_DEG, preserve_topology=True)
        out.append({"name": name, "count": count, "density": round(count / area, 3) if area > 0 else 0.0,
                    "geometry": mapping(simple if not simple.is_empty else geom)})
    return out


def ept_footprints(*, max_age_days: float = EPT_INDEX_MAX_AGE_DAYS, fetch=_get) -> list[Footprint]:
    """Obrysy mračen z cache (obnova po ``max_age_days``, při výpadku starý seznam, bez něj prázdný)."""
    from shapely.geometry import shape

    path = _cache_dir() / "ept_footprints.json"
    with _FOOTPRINT_LOCK:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = {}
        fresh = cached.get("items") and time.time() - float(cached.get("fetched") or 0) < max_age_days * 86400
        if not fresh and time.monotonic() - _FOOTPRINT_FAILED.get(str(path), -1e9) > FOOTPRINT_RETRY_S:
            try:
                items = parse_footprints(json.loads(fetch(US_EPT_BOUNDARIES)))
            except Exception:  # noqa: BLE001 – výpadek: poslouží starý seznam, jinak výběr jen podle WESM
                items = []
            if not items:  # další pokus až za chvíli, ať sondy a joby nečekají na zámku při každém dotazu
                _FOOTPRINT_FAILED[str(path)] = time.monotonic()
            if items:
                cached = {"fetched": time.time(), "items": items}
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
                tmp.write_text(json.dumps(cached), encoding="utf-8")
                tmp.replace(path)
        if not cached.get("items"):
            return []
        key = (str(path), cached.get("fetched"))
        if _FOOTPRINTS.get("key") != key:
            _FOOTPRINTS.clear()
            _FOOTPRINTS.update(key=key, items=[
                Footprint(i["name"], int(i["count"]), float(i["density"]), shape(i["geometry"]))
                for i in cached["items"]
            ])
        return list(_FOOTPRINTS["items"])


def candidates(bbox_wgs84, *, fetch=_get) -> list[tuple[str, Workunit]]:
    """(složka EPT, projekt) pro výřez od nejnovějšího: mračna, jejichž obrys do výřezu zasahuje a mají kvalitu
    QL 0 – QL 2 podle WESM (mimo WESM hustotu ``US_MIN_DENSITY``), a projekty WESM QL 0 – QL 2 v bucketu bez obrysu
    (nová data). Projekty QL 0 – QL 2, jejichž mračno v bucketu není, si pamatuje ``pending_workunits``."""
    from shapely.geometry import box

    area = box(*(float(v) for v in bbox_wgs84))
    footprints = ept_footprints(fetch=fetch)
    try:
        wesm = wesm_workunits(bbox_wgs84, fetch=fetch, qualities=None)
    except Exception:  # noqa: BLE001 – bez WESM rozhodnou obrysy a hustota; bez obrysů nelze nic
        if not footprints:
            raise
        wesm = []
    by_norm = {_norm(w.name): w for w in wesm}
    known = {fp.name for fp in footprints}
    hits = [fp for fp in footprints if fp.geometry.intersects(area)]
    missing = [ept_workunit(fp.name) for fp in hits if _norm(ept_workunit(fp.name)) not in by_norm]
    if missing:  # obrys WESM výřez míjí, nebo jméno ve WESM není vůbec
        try:
            by_norm.update({k: v for k, v in wesm_by_name(missing, fetch=fetch).items() if k not in by_norm})
        except Exception:  # noqa: BLE001 – bez dotazu rozhodne hustota
            pass
    out: list[tuple[str, Workunit]] = []
    used: set[str] = set()
    for fp in hits:
        wu = by_norm.get(_norm(ept_workunit(fp.name)))
        if wu is not None:
            used.add(_norm(wu.name))
            if wu.ql not in US_QUALITY_LEVELS:
                continue
            out.append((fp.name, Workunit(wu.name, wu.ql, wu.year, fp.geometry)))
        elif fp.density >= US_MIN_DENSITY:
            out.append((fp.name, Workunit(fp.name, f"{fp.density:.0f} b/m²", _name_year(fp.name), fp.geometry)))
    pending = []
    for wu in wesm:
        if wu.ql not in US_QUALITY_LEVELS or _norm(wu.name) in used:
            continue
        name = ept_for(wu.name, fetch=fetch)
        if name and name in known:
            continue  # mračno má obrys a do výřezu nezasahuje
        if name:
            out.append((name, wu))
        else:
            pending.append(f"{wu.name} ({wu.year})" if wu.year else wu.name)
    if len(_PENDING) > 500:
        _PENDING.clear()
    _PENDING[_bbox_key(bbox_wgs84)] = pending
    out.sort(key=lambda c: (-c[1].year, c[1].ql))
    return out


def _bbox_key(bbox_wgs84) -> tuple:
    return tuple(round(float(v), 5) for v in bbox_wgs84)


def pending_workunits(bbox_wgs84) -> list[str]:
    """Projekty QL 0 – QL 2 ve výřezu, které USGS nasnímal, ale mračno v bucketu zatím není (z posledního ``candidates``)."""
    return list(_PENDING.get(_bbox_key(bbox_wgs84)) or [])


def coverage_share(bbox_wgs84, cands: list[tuple[str, Workunit]]) -> float:
    """Podíl výřezu pokrytý projekty (rovina lon × cos φ × lat stačí na výřez do desítek km)."""
    from shapely.geometry import box
    from shapely.ops import unary_union

    w, s, e, n = (float(v) for v in bbox_wgs84)
    area = box(w, s, e, n)
    if area.area <= 0 or not cands:
        return 0.0
    union = unary_union([wu.geometry for _name, wu in cands])
    return float(min(1.0, union.intersection(area).area / area.area))


def ept_url(name: str) -> str:
    return f"{US_EPT_BUCKET}/{urllib.parse.quote(name)}"
