"""OSM pěšiny a lesní cesty do OOM, bez duplicit se ZABAGED."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile

from app.pipeline.crs_5514 import wgs84_to_projected
from app.pipeline.fetch_openzu import USER_AGENT
from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import OomObjectPart, _path_object, _pyogrio_layer_rows, _wkb_parts
from app.pipeline.oom_symbol_map import symbol_index_for_code

# Veřejná zrcadla – hlavní DE často hlásí 504; rotujeme rychle.
OVERPASS_URLS = (
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
)
# Stejné jako Export na openstreetmap.org – celý bbox, filtrujeme relevantní highway.
OSM_API_MAP_URL = "https://api.openstreetmap.org/api/0.6/map"
# HTTP timeout na jeden pokus (krátký, ať při 504 stihneme další zrcadlo).
QUERY_TIMEOUT_S = 25
OVERPASS_QL_TIMEOUT_S = 25
OSM_API_TIMEOUT_S = 45

# path/footway nestačí – v ČR je spousta použitelných cest jako track/bridleway.
OSM_HIGHWAYS = frozenset(
    {"path", "footway", "steps", "bridleway", "cycleway", "track"}
)

# Vrstvy ZABAGED, vůči kterým bereme OSM jako duplicitní.
ZABAGED_PATH_LAYERS = frozenset(
    {
        "Cesta",
        "Pesina",
        "Ulice",
        "SilniceDalnice",
        "Lavka",
        "Most",
        "Podjezd",
        "Zabrana",
    }
)
NEAR_M = 25.0
OVERLAP_DROP = 0.45
SAMPLE_M = 10.0
MIN_LENGTH_M = 12.0
GRID_M = 30.0
SKIP_FOOTWAY = frozenset({"sidewalk", "crossing"})
SKIP_CYCLEWAY = frozenset({"sidewalk", "crossing", "lane", "share_busway", "track"})


def _overpass_ql(south: float, west: float, north: float, east: float) -> str:
    bbox = f"{south},{west},{north},{east}"
    # Jedna regex vrstva – méně Overpass zátěže než 6 samostatných way[...].
    hw = "|".join(sorted(OSM_HIGHWAYS))
    return (
        f"[out:json][timeout:{OVERPASS_QL_TIMEOUT_S}];"
        f'way["highway"~"^({hw})$"]({bbox});'
        f"out geom;"
    )


def parse_osm_api_map_xml(xml_text: str) -> list[dict]:
    """Vyfiltruje relevantní highway z OSM API map call (.osm XML)."""
    root = ET.fromstring(xml_text)
    nodes: dict[str, tuple[float, float]] = {}
    for node in root.findall("node"):
        nid = node.get("id")
        lat = node.get("lat")
        lon = node.get("lon")
        if nid is None or lat is None or lon is None:
            continue
        nodes[nid] = (float(lat), float(lon))

    elements: list[dict] = []
    for way in root.findall("way"):
        tags = {t.get("k"): t.get("v") for t in way.findall("tag") if t.get("k")}
        if (tags.get("highway") or "").lower() not in OSM_HIGHWAYS:
            continue
        geometry: list[dict] = []
        for nd in way.findall("nd"):
            ref = nd.get("ref")
            if ref is None or ref not in nodes:
                continue
            lat, lon = nodes[ref]
            geometry.append({"lat": lat, "lon": lon})
        if len(geometry) < 2:
            continue
        elements.append({"type": "way", "tags": tags, "geometry": geometry})
    return elements

def _fetch_overpass(
    west: float,
    south: float,
    east: float,
    north: float,
    *,
    log=None,
) -> tuple[list[dict] | None, Exception | None]:
    ql = _overpass_ql(south, west, north, east)
    body = urllib.parse.urlencode({"data": ql}).encode("utf-8")
    last_err: Exception | None = None
    for url in OVERPASS_URLS:
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=QUERY_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            elements = [
                e
                for e in (data.get("elements") or [])
                if e.get("type") == "way" and e.get("geometry")
            ]
            if log:
                host = url.split("/")[2]
                log(f"OSM Overpass ({host}): {len(elements)} way")
            return elements, None
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            last_err = exc
            if log:
                log(f"OSM Overpass {url}: {exc}")
    return None, last_err


def _fetch_osm_api_map(
    west: float,
    south: float,
    east: float,
    north: float,
    *,
    log=None,
) -> list[dict]:
    """Export z openstreetmap.org – API map call, pak filtr highway typů."""
    bbox = f"{west},{south},{east},{north}"
    url = f"{OSM_API_MAP_URL}?bbox={bbox}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=OSM_API_TIMEOUT_S) as resp:
        xml_text = resp.read().decode("utf-8")
    elements = parse_osm_api_map_xml(xml_text)
    if log:
        log(f"OSM API map: {len(elements)} way ({'/'.join(sorted(OSM_HIGHWAYS))})")
    return elements


def fetch_osm_path_elements(
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log=None,
) -> list[dict]:
    west, south, east, north = bbox_wgs84
    elements, last_err = _fetch_overpass(west, south, east, north, log=log)
    if elements is not None:
        return elements
    try:
        if log:
            log("OSM Overpass selhal – zkouším Export API (api.openstreetmap.org)…")
        return _fetch_osm_api_map(west, south, east, north, log=log)
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        ET.ParseError,
        OSError,
    ) as exc:
        if log:
            log(
                f"OSM pěšiny: Overpass ({last_err}) i API map ({exc}) selhaly "
                "– job pokračuje bez nich"
            )
        return []


def _way_skip_reason(tags: dict) -> str | None:
    if (tags.get("footway") or "").lower() in SKIP_FOOTWAY:
        return "chodník/přejezd"
    if (tags.get("cycleway") or "").lower() in SKIP_CYCLEWAY:
        return "cyklo pruh/chodník"
    if (tags.get("area") or "").lower() == "yes":
        return "area"
    if (tags.get("indoor") or "").lower() == "yes":
        return "indoor"
    hw = (tags.get("highway") or "").lower()
    if hw not in OSM_HIGHWAYS:
        return "highway"
    # Čistě silniční cycleway u silnice – ne pěšina v lese.
    if hw == "cycleway" and (tags.get("foot") or "").lower() in {"no", "private"}:
        return "cycleway bez pěších"
    return None


def osm_oom_code(highway: str, preset_id: str) -> str:
    """ISOM/ISSprOM kód podle OSM highway."""
    hw = (highway or "path").lower()
    sprint = preset_id.startswith("sprint")
    if hw == "track":
        # Lesní / polní cesta (vozová) – ne úzká pěšina.
        return "506" if sprint else "504"
    return "507"


def osm_way_to_5514(element: dict) -> list[tuple[float, float]] | None:
    tags = element.get("tags") or {}
    if _way_skip_reason(tags):
        return None
    pts: list[tuple[float, float]] = []
    for node in element.get("geometry") or []:
        lat = node.get("lat")
        lon = node.get("lon")
        if lat is None or lon is None:
            continue
        pts.append(wgs84_to_projected(float(lat), float(lon)))
    if len(pts) < 2:
        return None
    return pts


def polyline_length(pts: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(1, len(pts)):
        dx = pts[i][0] - pts[i - 1][0]
        dy = pts[i][1] - pts[i - 1][1]
        total += math.hypot(dx, dy)
    return total


def sample_polyline(pts: list[tuple[float, float]], step_m: float) -> list[tuple[float, float]]:
    if len(pts) < 2:
        return list(pts)
    out = [pts[0]]
    remain = float(step_m)
    for i in range(1, len(pts)):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg <= 1e-6:
            continue
        t = 0.0
        while remain <= seg - t:
            t += remain
            f = t / seg
            out.append((x0 + f * (x1 - x0), y0 + f * (y1 - y0)))
            remain = float(step_m)
        remain -= seg - t
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


def _point_seg_dist(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    dx, dy = bx - ax, by - ay
    len2 = dx * dx + dy * dy
    if len2 < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class _SegmentIndex:
    def __init__(self, cell_m: float = GRID_M) -> None:
        self.cell_m = cell_m
        self.cells: dict[tuple[int, int], list[tuple[float, float, float, float]]] = (
            defaultdict(list)
        )

    def add_line(self, pts: list[tuple[float, float]]) -> None:
        for i in range(1, len(pts)):
            ax, ay = pts[i - 1]
            bx, by = pts[i]
            xmin, xmax = min(ax, bx), max(ax, bx)
            ymin, ymax = min(ay, by), max(ay, by)
            i0 = int(math.floor(xmin / self.cell_m))
            i1 = int(math.floor(xmax / self.cell_m))
            j0 = int(math.floor(ymin / self.cell_m))
            j1 = int(math.floor(ymax / self.cell_m))
            seg = (ax, ay, bx, by)
            for ii in range(i0, i1 + 1):
                for jj in range(j0, j1 + 1):
                    self.cells[(ii, jj)].append(seg)

    def nearest(self, x: float, y: float, *, max_m: float | None = None) -> float:
        """Vzdálenost k nejbližšímu segmentu; prohledá buňky do max_m (ne jen 3×3)."""
        i = int(math.floor(x / self.cell_m))
        j = int(math.floor(y / self.cell_m))
        radius = 1
        if max_m is not None and max_m > 0:
            radius = max(1, int(math.ceil(max_m / self.cell_m)))
        best = float("inf")
        for di in range(-radius, radius + 1):
            for dj in range(-radius, radius + 1):
                for ax, ay, bx, by in self.cells.get((i + di, j + dj), ()):
                    best = min(best, _point_seg_dist(x, y, ax, ay, bx, by))
        return best


def overlap_fraction(
    osm_pts: list[tuple[float, float]],
    index: _SegmentIndex,
    *,
    near_m: float = NEAR_M,
    sample_m: float = SAMPLE_M,
) -> float:
    samples = sample_polyline(osm_pts, sample_m)
    if not samples:
        return 0.0
    near = sum(
        1 for x, y in samples if index.nearest(x, y, max_m=near_m) <= near_m
    )
    return near / len(samples)


def _iter_line_parts_from_shp(shp_ref: str | Path):
    for _props, wkb in _pyogrio_layer_rows(shp_ref):
        parts, _ = _wkb_parts(wkb)
        for part in parts:
            if part[0] == "line":
                pts = [(float(x), float(y)) for x, y in part[1]]  # type: ignore[misc]
                if len(pts) >= 2:
                    yield pts


def _zabaged_shp_members(zabaged_clean: Path) -> list[tuple[str, str]]:
    """[(kanonický název vrstvy, cesta uvnitř ZIPu k .shp), ...]."""
    wanted = {name.lower(): name for name in ZABAGED_PATH_LAYERS}
    found: dict[str, str] = {}
    with ZipFile(zabaged_clean) as zf:
        for name in zf.namelist():
            path = Path(name)
            if path.suffix.lower() != ".shp":
                continue
            canon = wanted.get(path.stem.lower())
            if canon is None:
                continue
            # Preferuj soubor přímo v kořeni ZIPu.
            prev = found.get(canon)
            if prev is None or "/" not in name.replace("\\", "/"):
                found[canon] = name
    return [(canon, member) for canon, member in sorted(found.items())]


def _zabaged_path_lines(zabaged_clean: Path, *, log=None) -> list[list[tuple[float, float]]]:
    """Načte ZABAGED cesty/pěšiny pro dedup – extrakce do temp (spolehlivější než /vsizip/)."""
    lines: list[list[tuple[float, float]]] = []
    try:
        import pyogrio.raw  # noqa: F401
    except ImportError:
        if log:
            log("OSM dedup: pyogrio není k dispozici")
        return lines

    members = _zabaged_shp_members(zabaged_clean)
    if not members:
        if log:
            log("OSM dedup: v ZABAGED ZIPu nejsou vrstvy cest/pěšin")
        return lines

    import shutil
    import tempfile

    stage = Path(tempfile.mkdtemp(prefix="osm_zab_"))
    try:
        with ZipFile(zabaged_clean) as zf:
            names = set(zf.namelist())
            for _canon, member in members:
                stem = Path(member).stem
                for n in names:
                    nn = n.replace("\\", "/")
                    if Path(nn).stem.lower() != stem.lower():
                        continue
                    if Path(nn).suffix.lower() not in {
                        ".shp",
                        ".shx",
                        ".dbf",
                        ".prj",
                        ".cpg",
                    }:
                        continue
                    dest = stage / Path(nn).name
                    if not dest.exists():
                        dest.write_bytes(zf.read(n))
        for shp in sorted(stage.glob("*.shp")):
            if shp.stem.lower() not in {n.lower() for n in ZABAGED_PATH_LAYERS}:
                continue
            try:
                before = len(lines)
                lines.extend(_iter_line_parts_from_shp(shp))
                if log:
                    log(
                        f"OSM dedup: {shp.stem} → {len(lines) - before} linií"
                    )
            except Exception as exc:
                if log:
                    log(f"OSM dedup: {shp.name} selhalo ({exc})")
                continue
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    if not lines:
        # Poslední pokus: /vsizip/ (když extrakce sidecars selže).
        zip_posix = zabaged_clean.resolve().as_posix()
        for canon, member in members:
            vsi = f"/vsizip/{zip_posix}/{member.replace(chr(92), '/')}"
            try:
                before = len(lines)
                lines.extend(_iter_line_parts_from_shp(vsi))
                if log and len(lines) > before:
                    log(f"OSM dedup: {canon} (/vsizip/) → {len(lines) - before} linií")
            except Exception as exc:
                if log:
                    log(f"OSM dedup: {canon} /vsizip/ selhalo ({exc})")
                continue
    return lines


def unique_polyline_parts(
    osm_pts: list[tuple[float, float]],
    index: _SegmentIndex,
    *,
    near_m: float = NEAR_M,
    sample_m: float = SAMPLE_M,
) -> list[list[tuple[float, float]]]:
    """Úseky OSM linie, které neleží u ZABAGED. Celou way kvůli kusu u silnice nesekáme."""
    samples = sample_polyline(osm_pts, sample_m)
    if len(samples) < 2:
        return []
    parts: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for x, y in samples:
        if index.nearest(x, y, max_m=near_m) > near_m:
            current.append((x, y))
        elif current:
            parts.append(current)
            current = []
    if current:
        parts.append(current)
    return [p for p in parts if len(p) >= 2]


def filter_osm_against_zabaged(
    osm_lines: list[list[tuple[float, float]]],
    zabaged_lines: list[list[tuple[float, float]]],
    *,
    near_m: float = NEAR_M,
    overlap_drop: float = OVERLAP_DROP,
) -> tuple[list[list[tuple[float, float]]], int]:
    if not zabaged_lines:
        kept = [line for line in osm_lines if polyline_length(line) >= MIN_LENGTH_M]
        return kept, len(osm_lines) - len(kept)
    index = _SegmentIndex()
    for line in zabaged_lines:
        index.add_line(line)
    kept: list[list[tuple[float, float]]] = []
    dropped = 0
    for line in osm_lines:
        if polyline_length(line) < MIN_LENGTH_M:
            dropped += 1
            continue
        # Skoro celá way na ZABAGED → rovnou pryč (šetří unique_polyline_parts).
        if overlap_fraction(line, index, near_m=near_m) >= max(overlap_drop, 0.85):
            dropped += 1
            continue
        parts = [
            p
            for p in unique_polyline_parts(line, index, near_m=near_m)
            if polyline_length(p) >= MIN_LENGTH_M
            and overlap_fraction(p, index, near_m=near_m) < overlap_drop
        ]
        if not parts:
            dropped += 1
            continue
        kept.extend(parts)
    return kept, dropped


def filter_osm_items_against_zabaged(
    osm_items: list[tuple[list[tuple[float, float]], str]],
    zabaged_lines: list[list[tuple[float, float]]],
    *,
    near_m: float = NEAR_M,
    overlap_drop: float = OVERLAP_DROP,
) -> tuple[list[tuple[list[tuple[float, float]], str]], int]:
    """Dedup proti ZABAGED se zachováním highway tagu u každého úseku."""
    if not zabaged_lines:
        kept = [
            (pts, hw)
            for pts, hw in osm_items
            if polyline_length(pts) >= MIN_LENGTH_M
        ]
        return kept, len(osm_items) - len(kept)
    index = _SegmentIndex()
    for line in zabaged_lines:
        index.add_line(line)
    kept: list[tuple[list[tuple[float, float]], str]] = []
    dropped = 0
    for line, hw in osm_items:
        if polyline_length(line) < MIN_LENGTH_M:
            dropped += 1
            continue
        if overlap_fraction(line, index, near_m=near_m) >= max(overlap_drop, 0.85):
            dropped += 1
            continue
        parts = [
            p
            for p in unique_polyline_parts(line, index, near_m=near_m)
            if polyline_length(p) >= MIN_LENGTH_M
            and overlap_fraction(p, index, near_m=near_m) < overlap_drop
        ]
        if not parts:
            dropped += 1
            continue
        kept.extend((p, hw) for p in parts)
    return kept, dropped


def prepare_osm_paths(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    zabaged_clean: Path | None,
    *,
    log=None,
) -> Path | None:
    elements = fetch_osm_path_elements(bbox_wgs84, log=log)
    osm_items: list[tuple[list[tuple[float, float]], str]] = []
    skipped = 0
    for el in elements:
        pts = osm_way_to_5514(el)
        if pts is None:
            skipped += 1
            continue
        hw = ((el.get("tags") or {}).get("highway") or "path").lower()
        osm_items.append((pts, hw))
    zabaged_lines: list[list[tuple[float, float]]] = []
    if zabaged_clean and zabaged_clean.is_file():
        zabaged_lines = _zabaged_path_lines(zabaged_clean, log=log)
        if log:
            log(f"OSM dedup: {len(zabaged_lines)} ZABAGED linií (cesty/pěšiny)")
        if not zabaged_lines and log:
            log(
                "OSM dedup: varování – ZABAGED ZIP je, ale 0 cestovních linií; "
                "OSM pěšiny se neoříznou proti ZABAGED"
            )
    kept, dropped = filter_osm_items_against_zabaged(osm_items, zabaged_lines)
    if log:
        by_hw: dict[str, int] = defaultdict(int)
        for _pts, hw in kept:
            by_hw[hw] += 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(by_hw.items())) or "—"
        log(
            f"OSM cesty: {len(kept)} ponecháno ({summary}), "
            f"{dropped} duplicit/krátkých, {skipped} přeskočeno (tag)"
        )
    dest_dir = work_dir / "osm_paths"
    dest_dir.mkdir(parents=True, exist_ok=True)
    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"source": "osm", "highway": hw},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[x, y] for x, y in line],
                },
            }
            for line, hw in kept
        ],
    }
    out = dest_dir / "paths.geojson"
    out.write_text(json.dumps(gj), encoding="utf-8")
    return out if kept else None


def build_osm_path_parts(
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
) -> list[OomObjectPart]:
    gj_path = work_dir / "osm_paths" / "paths.geojson"
    if not gj_path.is_file():
        return []
    data = json.loads(gj_path.read_text(encoding="utf-8"))
    objects: list[str] = []
    symbol_cache: dict[str, int | None] = {}
    for feat in data.get("features") or []:
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") != "LineString" or len(coords) < 2:
            continue
        hw = str((feat.get("properties") or {}).get("highway") or "path")
        code = osm_oom_code(hw, preset_id)
        if code not in symbol_cache:
            symbol_cache[code] = symbol_index_for_code(preset_id, scale, code)
        symbol_index = symbol_cache[code]
        if symbol_index is None:
            # Fallback na pěšinu, když symbol set kód nemá.
            if "507" not in symbol_cache:
                symbol_cache["507"] = symbol_index_for_code(preset_id, scale, "507")
            symbol_index = symbol_cache["507"]
        if symbol_index is None:
            continue
        mapped = [
            projected_to_map_coord(
                float(x),
                float(y),
                ref_x=ref_x,
                ref_y=ref_y,
                scale=scale,
                grivation_deg=grivation_deg,
            )
            for x, y in coords
        ]
        obj = _path_object(symbol_index, mapped)
        if obj:
            objects.append(obj)
    if not objects:
        return []
    return [
        OomObjectPart(
            name="OSM cesty",
            objects_xml="\n".join(objects),
            count=len(objects),
        )
    ]
