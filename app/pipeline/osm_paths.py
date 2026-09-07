"""OSM pěšiny a objekty (studny, hřiště, lavičky, tabule, mokřad, …) do OOM."""

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
from app.pipeline.oom_import import (
    OomObjectPart,
    _area_object,
    _path_object,
    _point_object,
    _pyogrio_layer_rows,
    _wkb_parts,
)
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
# Ulice/silnice bereme ze ZABAGED (přesnější), ne z OSM.
# pedestrian = náměstí / pěší zóny (hlavně sprint).
OSM_HIGHWAYS = frozenset(
    {"path", "footway", "steps", "bridleway", "cycleway", "track", "pedestrian"}
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
# Priorita OSM: ořezávat jen proti pevným komunikacím (ne proti Pesina/Cesta).
ZABAGED_PATH_LAYERS_PRIORITY = frozenset(
    {
        "Ulice",
        "SilniceDalnice",
        "Lavka",
        "Most",
        "Podjezd",
        "Tunel",
    }
)
NEAR_M = 25.0  # zpětná kompatibilita testů / starších volání
# Stejná středová čára (ne „něco v okolí“): OSM a ZABAGED přes sebe.
MATCH_M = 6.0
COVER_DROP = 0.70
# Min. |cos| úhlu tečen – paralelní i protisměr; kolmá cesta se neshoduje.
MIN_DIR_DOT = 0.5
OVERLAP_DROP = 0.45  # alias COVER_DROP pro stará volání
SAMPLE_M = 5.0
MIN_LENGTH_M = 12.0
GRID_M = 30.0
SKIP_FOOTWAY = frozenset({"sidewalk", "crossing"})
SKIP_CYCLEWAY = frozenset({"sidewalk", "crossing", "lane", "share_busway", "track"})


def _overpass_ql(
    south: float,
    west: float,
    north: float,
    east: float,
    *,
    include_benches: bool = False,
    include_lamps: bool = False,
    include_playground_equipment: bool = False,
    osm_priority: bool = False,
) -> str:
    bbox = f"{south},{west},{north},{east}"
    # Jedna regex vrstva – méně Overpass zátěže než 6 samostatných way[...].
    hw = "|".join(sorted(OSM_HIGHWAYS))
    parts = [
        f'way["highway"~"^({hw})$"]({bbox});',
        f'way["man_made"="boardwalk"]({bbox});',
        f'way["man_made"="water_well"]({bbox});',
        f'node["man_made"="water_well"]({bbox});',
        f'node["amenity"="fountain"]({bbox});',
        f'way["amenity"="fountain"]({bbox});',
        f'node["natural"="spring"]({bbox});',
        f'way["natural"="spring"]({bbox});',
        f'way["leisure"="playground"]({bbox});',
        f'way["leisure"="pitch"]({bbox});',
        f'node["leisure"="pitch"]({bbox});',
        f'way["natural"="wetland"]({bbox});',
        f'node["natural"="cave_entrance"]({bbox});',
        f'way["natural"="cave_entrance"]({bbox});',
    ]
    if include_playground_equipment:
        parts.extend(
            [
                f'node["leisure"="playground"]({bbox});',
                f'node["playground"]({bbox});',
                f'way["playground"]({bbox});',
            ]
        )
    if include_benches:
        parts.extend(
            [
                f'node["amenity"="bench"]({bbox});',
                f'way["amenity"="bench"]({bbox});',
                f'node["leisure"="picnic_table"]({bbox});',
                f'node["tourism"="information"]({bbox});',
                f'way["tourism"="information"]({bbox});',
                f'node["information"~"^(board|map|trail_board)$"]({bbox});',
                f'way["information"~"^(board|map|trail_board)$"]({bbox});',
                f'node["leisure"="firepit"]({bbox});',
                f'way["leisure"="firepit"]({bbox});',
                f'node["amenity"="bbq"]({bbox});',
                f'way["amenity"="bbq"]({bbox});',
            ]
        )
    if include_lamps:
        parts.append(f'node["highway"="street_lamp"]({bbox});')
    if osm_priority:
        # Hlavně sprint / urban: ploty, zdi, brány, přístřešky, pomníky, fitness…
        parts.extend(
            [
                f'way["barrier"~"^(fence|wall|hedge|retaining_wall)$"]({bbox});',
                f'node["barrier"~"^(gate|bollard|stile|cycle_barrier|block|lift_gate)$"]({bbox});',
                f'way["barrier"~"^(gate|bollard|stile|cycle_barrier|block|lift_gate)$"]({bbox});',
                f'node["historic"~"^(memorial|monument)$"]({bbox});',
                f'way["historic"~"^(memorial|monument)$"]({bbox});',
                f'node["man_made"="cross"]({bbox});',
                f'node["amenity"="shelter"]({bbox});',
                f'way["amenity"="shelter"]({bbox});',
                f'node["man_made"="canopy"]({bbox});',
                f'way["man_made"="canopy"]({bbox});',
                f'node["leisure"="fitness_station"]({bbox});',
                f'way["leisure"="fitness_station"]({bbox});',
                f'node["natural"="tree"]["denotation"~"^(landmark|natural_monument)$"]({bbox});',
            ]
        )
    return (
        f"[out:json][timeout:{OVERPASS_QL_TIMEOUT_S}];"
        f"("
        + "".join(parts)
        + f");"
        f"out geom;"
    )


_INFO_BOARD_VALUES = frozenset({"board", "map", "trail_board"})
# Povrch / výplň hřiště – ne křížek (pokrývá leisure=playground).
_PLAYGROUND_SURFACE = frozenset({"sandpit", "no"})
_BARRIER_LINES = frozenset({"fence", "wall", "hedge", "retaining_wall"})
_BARRIER_POINTS = frozenset(
    {"gate", "bollard", "stile", "cycle_barrier", "block", "lift_gate"}
)
# Volitelné skupiny (rozšířená nastavení).
_BENCH_KINDS = frozenset({"bench", "info_board", "picnic_table", "firepit"})
_LAMP_KINDS = frozenset({"lamp"})
_PLAYGROUND_EQUIPMENT_KINDS = frozenset({"playground_equipment"})
# Jen při kp_osm_priority (hlavně sprint urban pack).
_PRIORITY_KINDS = frozenset(
    {
        "fence",
        "wall",
        "hedge",
        "barrier_point",
        "memorial",
        "shelter",
        "fitness",
        "landmark_tree",
    }
)
_POINT_FEATURE_KINDS = frozenset(
    {
        "bench",
        "info_board",
        "cave_entrance",
        "water_well",
        "spring",
        "lamp",
        "picnic_table",
        "firepit",
        "playground_equipment",
        "barrier_point",
        "memorial",
        "shelter",
        "fitness",
        "landmark_tree",
    }
)
_LINE_FEATURE_KINDS = frozenset({"fence", "wall", "hedge"})


def _is_boardwalk(tags: dict) -> bool:
    return (
        (tags.get("footway") or "").lower() == "boardwalk"
        or (tags.get("man_made") or "").lower() == "boardwalk"
        or (tags.get("bridge") or "").lower() == "boardwalk"
    )


def classify_osm_feature(
    tags: dict, *, geom: str = "way"
) -> tuple[str, str] | None:
    """(kind, výchozí OOM kód) – kód může přepsat feature_oom_code podle presetu.

    Dřevěný chodník sem nepatří – mapuje se jako běžná pěšina.
    Nábytek filtruje prepare podle include_benches / include_lamps /
    include_playground_equipment. PRIORITY pack (ploty, pomníky, …) podle osm_priority.
    Lampy → 530 (kolečko); lavičky/ohniště/herní prvky/… → 531 (křížek).
    """
    leisure = (tags.get("leisure") or "").lower()
    man_made = (tags.get("man_made") or "").lower()
    building = (tags.get("building") or "").lower()
    amenity = (tags.get("amenity") or "").lower()
    tourism = (tags.get("tourism") or "").lower()
    information = (tags.get("information") or "").lower()
    natural = (tags.get("natural") or "").lower()
    highway = (tags.get("highway") or "").lower()
    playground = (tags.get("playground") or "").lower()
    barrier = (tags.get("barrier") or "").lower()
    historic = (tags.get("historic") or "").lower()
    denotation = (tags.get("denotation") or "").lower()
    is_node = geom == "node"

    if amenity == "bench":
        return "bench", "531"
    if leisure == "picnic_table":
        return "picnic_table", "531"
    if highway == "street_lamp":
        return "lamp", "530"
    if leisure == "firepit" or amenity == "bbq":
        return "firepit", "531"
    # Herní prvek (houpačka, skluzavka, …) nebo bodové hřiště → křížek.
    # Pískoviště apod. přeskočit – je to povrch uvnitř leisure=playground.
    if playground:
        if playground in _PLAYGROUND_SURFACE:
            return None
        return "playground_equipment", "531"
    if leisure == "playground" and is_node:
        return "playground_equipment", "531"
    if leisure == "fitness_station":
        return "fitness", "531"
    if barrier in _BARRIER_LINES:
        if barrier in {"wall", "retaining_wall"}:
            return "wall", "513.2"
        if barrier == "hedge":
            return "hedge", "518"
        return "fence", "518"
    if barrier in _BARRIER_POINTS:
        return "barrier_point", "531"
    if historic in {"memorial", "monument"} or man_made == "cross":
        return "memorial", "526"
    if amenity == "shelter" or man_made == "canopy":
        return "shelter", "522"
    if natural == "tree" and denotation in {"landmark", "natural_monument"}:
        return "landmark_tree", "417"
    if (
        tourism == "information" or information in _INFO_BOARD_VALUES
    ) and information not in {"office", "visitor_centre", "visitor_center"}:
        if building and building not in {"no", "false", "0"}:
            return None
        return "info_board", "531"
    if natural == "wetland":
        return "wetland", "308"
    if natural == "cave_entrance":
        return "cave_entrance", "203.1"
    if natural == "spring":
        return "spring", "312"
    if leisure == "playground":
        return "playground", "401"
    if leisure == "pitch":
        return "pitch", "401"
    if man_made == "water_well" or amenity == "fountain":
        if building and building not in {"no", "false", "0"}:
            return "water_well_building", "521"
        return "water_well", "311"
    return None


def feature_oom_code(kind: str, preset_id: str, stored_code: str = "") -> str:
    """OOM kód podle druhu objektu a presetu (sprint vs les)."""
    sprint = preset_id.startswith("sprint")
    if kind == "cave_entrance":
        return "203.1" if sprint else "203.2"
    if kind == "fence":
        return "518" if sprint else "516"
    if kind == "wall":
        return "513.2" if sprint else "513"
    if kind == "hedge":
        return "518" if sprint else "416"
    if stored_code:
        return stored_code
    defaults = {
        "bench": "531",
        "info_board": "531",
        "lamp": "530",
        "picnic_table": "531",
        "firepit": "531",
        # ISSprOM/ISOM prominent man-made × – na sprintu stejně (čitelné na žluté ploše hřiště).
        "playground_equipment": "531",
        "playground_marker": "531",
        "pitch_marker": "531",
        "barrier_point": "531",
        "fitness": "531",
        "memorial": "526",
        "shelter": "522",
        "landmark_tree": "417",
        "wetland": "308",
        "playground": "401",
        "pitch": "401",
        "water_well": "311",
        "water_well_building": "521",
        "spring": "312",
    }
    return defaults.get(kind, stored_code or "")


def parse_osm_api_map_xml(xml_text: str) -> list[dict]:
    """Vyfiltruje highway + OSM objekty z OSM API map call (.osm XML)."""
    root = ET.fromstring(xml_text)
    nodes: dict[str, tuple[float, float]] = {}
    node_tags: dict[str, dict] = {}
    for node in root.findall("node"):
        nid = node.get("id")
        lat = node.get("lat")
        lon = node.get("lon")
        if nid is None or lat is None or lon is None:
            continue
        nodes[nid] = (float(lat), float(lon))
        tags = {t.get("k"): t.get("v") for t in node.findall("tag") if t.get("k")}
        if tags:
            node_tags[nid] = tags

    elements: list[dict] = []
    for nid, tags in node_tags.items():
        if not classify_osm_feature(tags, geom="node"):
            continue
        lat, lon = nodes[nid]
        elements.append(
            {
                "type": "node",
                "tags": tags,
                "lat": lat,
                "lon": lon,
            }
        )
    for way in root.findall("way"):
        tags = {t.get("k"): t.get("v") for t in way.findall("tag") if t.get("k")}
        hw = (tags.get("highway") or "").lower()
        is_path = hw in OSM_HIGHWAYS or _is_boardwalk(tags)
        is_feat = classify_osm_feature(tags, geom="way") is not None
        if not is_path and not is_feat:
            continue
        geometry: list[dict] = []
        for nd in way.findall("nd"):
            ref = nd.get("ref")
            if ref is None or ref not in nodes:
                continue
            lat, lon = nodes[ref]
            geometry.append({"lat": lat, "lon": lon})
        if is_path and len(geometry) < 2:
            continue
        if is_feat and len(geometry) < 1:
            continue
        elements.append({"type": "way", "tags": tags, "geometry": geometry})
    return elements

def _fetch_overpass(
    west: float,
    south: float,
    east: float,
    north: float,
    *,
    include_benches: bool = False,
    include_lamps: bool = False,
    include_playground_equipment: bool = False,
    osm_priority: bool = False,
    log=None,
) -> tuple[list[dict] | None, Exception | None]:
    ql = _overpass_ql(
        south,
        west,
        north,
        east,
        include_benches=include_benches,
        include_lamps=include_lamps,
        include_playground_equipment=include_playground_equipment,
        osm_priority=osm_priority,
    )
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
                if e.get("type") in {"way", "node"}
                and (
                    e.get("geometry")
                    or (e.get("type") == "node" and e.get("lat") is not None)
                )
            ]
            if log:
                host = url.split("/")[2]
                log(f"OSM Overpass ({host}): {len(elements)} prvků")
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
        log(
            f"OSM API map: {len(elements)} prvků "
            f"(cesty + objekty)"
        )
    return elements


def fetch_osm_path_elements(
    bbox_wgs84: tuple[float, float, float, float],
    *,
    include_benches: bool = False,
    include_lamps: bool = False,
    include_playground_equipment: bool = False,
    osm_priority: bool = False,
    log=None,
) -> list[dict]:
    west, south, east, north = bbox_wgs84
    elements, last_err = _fetch_overpass(
        west,
        south,
        east,
        north,
        include_benches=include_benches,
        include_lamps=include_lamps,
        include_playground_equipment=include_playground_equipment,
        osm_priority=osm_priority,
        log=log,
    )
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
    footway = (tags.get("footway") or "").lower()
    # Dřevěný chodník mapujeme jako pěšinu (ne jako most).
    if _is_boardwalk(tags):
        return None
    if footway in SKIP_FOOTWAY:
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


def highway_width_rank(highway: str) -> int:
    """Vyšší = širší / preferovanější při překryvu střednic."""
    hw = (highway or "path").lower()
    return {
        "track": 40,
        "bridleway": 30,
        "cycleway": 25,
        "pedestrian": 20,
        "path": 10,
        "footway": 10,
        "steps": 5,
    }.get(hw, 10)


def dedup_osm_prefer_wider(
    items: list[tuple[list[tuple[float, float]], str]],
    *,
    match_m: float = MATCH_M,
    cover_drop: float = COVER_DROP,
) -> tuple[list[tuple[list[tuple[float, float]], str]], int]:
    """OSM×OSM: při shodné střednici nechá širší (track > path/footway)."""
    ordered = sorted(
        items,
        key=lambda it: (
            -highway_width_rank(it[1]),
            -polyline_length(it[0]),
        ),
    )
    kept: list[tuple[list[tuple[float, float]], str]] = []
    index = _SegmentIndex()
    dropped = 0
    for pts, hw in ordered:
        if polyline_length(pts) < MIN_LENGTH_M:
            dropped += 1
            continue
        if kept and centerline_cover_fraction(pts, index, match_m=match_m) >= cover_drop:
            dropped += 1
            continue
        kept.append((pts, hw))
        index.add_line(pts)
    return kept, dropped


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
        """Vzdálenost k nejbližšímu segmentu; prohledá buňky do max_m."""
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

    def on_centerline(
        self,
        x: float,
        y: float,
        tx: float,
        ty: float,
        *,
        match_m: float = MATCH_M,
        min_dir_dot: float = MIN_DIR_DOT,
    ) -> bool:
        """True, když bod leží na ZABAGED střednici se stejným směrem (ne kolmá křižovatka)."""
        i = int(math.floor(x / self.cell_m))
        j = int(math.floor(y / self.cell_m))
        radius = max(1, int(math.ceil(match_m / self.cell_m)))
        tang = math.hypot(tx, ty)
        ux, uy = (tx / tang, ty / tang) if tang > 1e-9 else (0.0, 0.0)
        for di in range(-radius, radius + 1):
            for dj in range(-radius, radius + 1):
                for ax, ay, bx, by in self.cells.get((i + di, j + dj), ()):
                    if _point_seg_dist(x, y, ax, ay, bx, by) > match_m:
                        continue
                    sx, sy = bx - ax, by - ay
                    sl = math.hypot(sx, sy)
                    if sl < 1e-9:
                        if ux == 0.0 and uy == 0.0:
                            return True
                        continue
                    # |cos| – stejný i opačný směr střednice.
                    if abs((ux * sx + uy * sy) / sl) >= min_dir_dot or (
                        ux == 0.0 and uy == 0.0
                    ):
                        return True
        return False


def _sample_tangents(
    samples: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Jednotková tečna v každém vzorku (dopředná / zpětná na koncích)."""
    n = len(samples)
    out: list[tuple[float, float]] = []
    for i in range(n):
        if i + 1 < n:
            dx = samples[i + 1][0] - samples[i][0]
            dy = samples[i + 1][1] - samples[i][1]
        elif i > 0:
            dx = samples[i][0] - samples[i - 1][0]
            dy = samples[i][1] - samples[i - 1][1]
        else:
            dx, dy = 0.0, 0.0
        L = math.hypot(dx, dy)
        out.append((dx / L, dy / L) if L > 1e-9 else (0.0, 0.0))
    return out


def centerline_cover_fraction(
    osm_pts: list[tuple[float, float]],
    index: _SegmentIndex,
    *,
    match_m: float = MATCH_M,
    sample_m: float = SAMPLE_M,
) -> float:
    """Podíl délky OSM, který leží na stejné střednici jako ZABAGED."""
    samples = sample_polyline(osm_pts, sample_m)
    if not samples:
        return 0.0
    tangents = _sample_tangents(samples)
    hit = sum(
        1
        for (x, y), (tx, ty) in zip(samples, tangents)
        if index.on_centerline(x, y, tx, ty, match_m=match_m)
    )
    return hit / len(samples)


def overlap_fraction(
    osm_pts: list[tuple[float, float]],
    index: _SegmentIndex,
    *,
    near_m: float = MATCH_M,
    sample_m: float = SAMPLE_M,
) -> float:
    """Zpětná kompatibilita – teď = pokrytí střednicí (near_m = match poloměr)."""
    return centerline_cover_fraction(
        osm_pts, index, match_m=near_m, sample_m=sample_m
    )


def unique_polyline_parts(
    osm_pts: list[tuple[float, float]],
    index: _SegmentIndex,
    *,
    near_m: float = MATCH_M,
    sample_m: float = SAMPLE_M,
) -> list[list[tuple[float, float]]]:
    """Úseky OSM mimo ZABAGED střednici (např. ocas pěšiny do lesa)."""
    samples = sample_polyline(osm_pts, sample_m)
    if len(samples) < 2:
        return []
    tangents = _sample_tangents(samples)
    parts: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for (x, y), (tx, ty) in zip(samples, tangents):
        on_zab = index.on_centerline(x, y, tx, ty, match_m=near_m)
        if not on_zab:
            current.append((x, y))
        elif current:
            parts.append(current)
            current = []
    if current:
        parts.append(current)
    return [p for p in parts if len(p) >= 2]


def _zabaged_shp_members(
    zabaged_clean: Path,
    *,
    layers: frozenset[str] | None = None,
) -> list[tuple[str, str]]:
    """[(kanonický název vrstvy, cesta uvnitř ZIPu k .shp), ...]."""
    layer_set = layers if layers is not None else ZABAGED_PATH_LAYERS
    wanted = {name.lower(): name for name in layer_set}
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


def _iter_line_parts_from_shp(shp_ref: str | Path):
    """Výtěž linií ze SHP – pyogrio, jinak GDAL/OGR (Docker image má OGR)."""
    try:
        import pyogrio.raw  # noqa: F401

        use_pyogrio = True
    except ImportError:
        use_pyogrio = False

    if use_pyogrio:
        for _props, wkb in _pyogrio_layer_rows(shp_ref):
            parts, _ = _wkb_parts(wkb)
            for part in parts:
                if part[0] == "line":
                    pts = [(float(x), float(y)) for x, y in part[1]]  # type: ignore[misc]
                    if len(pts) >= 2:
                        yield pts
        return

    try:
        from osgeo import ogr
    except ImportError:
        return

    ds = ogr.Open(str(shp_ref))
    if not ds:
        return
    layer = ds.GetLayer(0)
    if layer is None:
        return

    def emit(geom) -> list[list[tuple[float, float]]]:
        if geom is None:
            return []
        name = geom.GetGeometryName()
        out: list[list[tuple[float, float]]] = []
        if name == "LINESTRING":
            pts = [
                (float(geom.GetX(i)), float(geom.GetY(i)))
                for i in range(geom.GetPointCount())
            ]
            if len(pts) >= 2:
                out.append(pts)
        elif name in {"MULTILINESTRING", "GEOMETRYCOLLECTION"}:
            for i in range(geom.GetGeometryCount()):
                out.extend(emit(geom.GetGeometryRef(i)))
        return out

    for feature in layer:
        geom = feature.GetGeometryRef()
        for pts in emit(geom):
            yield pts


def _zabaged_path_lines(
    zabaged_clean: Path,
    *,
    log=None,
    layers: frozenset[str] | None = None,
) -> list[list[tuple[float, float]]]:
    """Načte ZABAGED cesty/pěšiny pro dedup – extrakce do temp (spolehlivější než /vsizip/)."""
    lines: list[list[tuple[float, float]]] = []
    try:
        import pyogrio.raw  # noqa: F401

        backend = "pyogrio"
    except ImportError:
        try:
            from osgeo import ogr  # noqa: F401

            backend = "ogr"
        except ImportError:
            if log:
                log("OSM dedup: chybí pyogrio i GDAL/OGR – dedup proti ZABAGED vypnut")
            return lines

    members = _zabaged_shp_members(zabaged_clean, layers=layers)
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
                        f"OSM dedup ({backend}): {shp.stem} → {len(lines) - before} linií"
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


def filter_osm_against_zabaged(
    osm_lines: list[list[tuple[float, float]]],
    zabaged_lines: list[list[tuple[float, float]]],
    *,
    near_m: float = MATCH_M,
    overlap_drop: float = COVER_DROP,
) -> tuple[list[list[tuple[float, float]]], int]:
    """Nechá OSM jen mimo ZABAGED střednici (shoda směru + ≤ match_m)."""
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
        # Stejná střednice po (skoro) celé délce → pryč.
        if centerline_cover_fraction(line, index, match_m=near_m) >= overlap_drop:
            dropped += 1
            continue
        parts = [
            p
            for p in unique_polyline_parts(line, index, near_m=near_m)
            if polyline_length(p) >= MIN_LENGTH_M
            and centerline_cover_fraction(p, index, match_m=near_m) < overlap_drop
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
    near_m: float = MATCH_M,
    overlap_drop: float = COVER_DROP,
) -> tuple[list[tuple[list[tuple[float, float]], str]], int]:
    """Dedup střednicí se zachováním highway tagu u každého úseku."""
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
        if centerline_cover_fraction(line, index, match_m=near_m) >= overlap_drop:
            dropped += 1
            continue
        parts = [
            p
            for p in unique_polyline_parts(line, index, near_m=near_m)
            if polyline_length(p) >= MIN_LENGTH_M
            and centerline_cover_fraction(p, index, match_m=near_m) < overlap_drop
        ]
        if not parts:
            dropped += 1
            continue
        kept.extend((p, hw) for p in parts)
    return kept, dropped


def osm_feature_to_5514(
    element: dict,
) -> tuple[str, str, list[tuple[float, float]]] | None:
    """(kind, oom_code, ring_or_point) v S-JTSK, nebo None."""
    tags = element.get("tags") or {}
    geom = "node" if element.get("type") == "node" else "way"
    classified = classify_osm_feature(tags, geom=geom)
    if not classified:
        return None
    kind, code = classified
    pts: list[tuple[float, float]] = []
    if element.get("type") == "node":
        lat, lon = element.get("lat"), element.get("lon")
        if lat is None or lon is None:
            return None
        pts = [wgs84_to_projected(float(lat), float(lon))]
    else:
        for node in element.get("geometry") or []:
            lat = node.get("lat")
            lon = node.get("lon")
            if lat is None or lon is None:
                continue
            pts.append(wgs84_to_projected(float(lat), float(lon)))
    if not pts:
        return None
    # Ploty / zdi / živé ploty – celá linie (i uzavřený ring jako LineString).
    if kind in _LINE_FEATURE_KINDS:
        if len(pts) < 2:
            return None
        return kind, code, pts
    # Bodové symboly – vždy jeden bod (těžiště).
    if kind in _POINT_FEATURE_KINDS:
        if len(pts) == 1:
            return kind, code, pts
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        return kind, code, [(cx, cy)]
    # Mokřad – jen uzavřená plocha.
    if kind == "wetland":
        if len(pts) < 3:
            return None
        if pts[0] != pts[-1]:
            if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1.5:
                pts = pts[:-1] + [pts[0]]
            else:
                pts.append(pts[0])
        return kind, code, pts
    # Uzavřený ring → plocha; jinak bod (těžiště / jediný uzel).
    if len(pts) >= 3:
        if pts[0] != pts[-1]:
            # téměř uzavřený?
            if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1.5:
                pts = pts[:-1] + [pts[0]]
            else:
                pts.append(pts[0])
        return kind, code, pts
    if len(pts) == 1:
        return kind, code, pts
    # krátká linie → střed
    mid = len(pts) // 2
    return kind, code, [pts[mid]]


def prepare_osm_paths(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    zabaged_clean: Path | None,
    *,
    include_benches: bool = False,
    include_lamps: bool = False,
    include_playground_equipment: bool = False,
    osm_priority: bool = False,
    preset_id: str = "",
    log=None,
) -> Path | None:
    elements = fetch_osm_path_elements(
        bbox_wgs84,
        include_benches=include_benches,
        include_lamps=include_lamps,
        include_playground_equipment=include_playground_equipment,
        osm_priority=osm_priority,
        log=log,
    )
    osm_items: list[tuple[list[tuple[float, float]], str]] = []
    features: list[dict] = []
    skipped = 0
    for el in elements:
        tags = el.get("tags") or {}
        el_geom = "node" if el.get("type") == "node" else "way"
        classified = classify_osm_feature(tags, geom=el_geom)
        if classified:
            kind, _code = classified
            if kind in _BENCH_KINDS and not include_benches:
                skipped += 1
                continue
            if kind in _LAMP_KINDS and not include_lamps:
                skipped += 1
                continue
            if kind in _PLAYGROUND_EQUIPMENT_KINDS and not include_playground_equipment:
                skipped += 1
                continue
            if kind in _PRIORITY_KINDS and not osm_priority:
                skipped += 1
                continue
            feat = osm_feature_to_5514(el)
            if feat is None:
                skipped += 1
                continue
            kind, code, pts = feat
            closed = len(pts) >= 3 and pts[0] == pts[-1]
            if kind in _LINE_FEATURE_KINDS and len(pts) >= 2:
                geometry = {
                    "type": "LineString",
                    "coordinates": [[x, y] for x, y in pts],
                }
            elif closed and kind not in _POINT_FEATURE_KINDS:
                coords = [[x, y] for x, y in pts]
                geometry = {"type": "Polygon", "coordinates": [coords]}
            else:
                x, y = pts[0]
                geometry = {"type": "Point", "coordinates": [x, y]}
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "source": "osm",
                        "kind": kind,
                        "oom_code": code,
                    },
                    "geometry": geometry,
                }
            )
            # Lesní mapa: žlutá 401 splyne se ZABAGED open land → středový křížek.
            if (
                kind in {"playground", "pitch"}
                and geometry.get("type") == "Polygon"
                and len(pts) >= 3
            ):
                ring = pts[:-1] if pts[0] == pts[-1] else pts
                if ring:
                    cx = sum(p[0] for p in ring) / len(ring)
                    cy = sum(p[1] for p in ring) / len(ring)
                    marker_kind = (
                        "playground_marker"
                        if kind == "playground"
                        else "pitch_marker"
                    )
                    features.append(
                        {
                            "type": "Feature",
                            "properties": {
                                "source": "osm",
                                "kind": marker_kind,
                                "oom_code": "531",
                            },
                            "geometry": {
                                "type": "Point",
                                "coordinates": [cx, cy],
                            },
                        }
                    )
            continue
        pts = osm_way_to_5514(el)
        if pts is None:
            skipped += 1
            continue
        hw = ((el.get("tags") or {}).get("highway") or "").lower()
        if not hw and _is_boardwalk(tags):
            hw = "footway"
        if not hw:
            hw = "path"
        osm_items.append((pts, hw))
    zabaged_lines: list[list[tuple[float, float]]] = []
    dedup_layers = (
        ZABAGED_PATH_LAYERS_PRIORITY if osm_priority else ZABAGED_PATH_LAYERS
    )
    if zabaged_clean and zabaged_clean.is_file():
        zabaged_lines = _zabaged_path_lines(
            zabaged_clean, log=log, layers=dedup_layers
        )
        if log:
            sprint = str(preset_id).startswith("sprint")
            if osm_priority:
                mode = (
                    "priorita OSM / sprint urban"
                    if sprint
                    else "priorita OSM (i v lese – urban pack)"
                )
            else:
                mode = "standard"
            log(f"OSM dedup ({mode}): {len(zabaged_lines)} ZABAGED linií")
        if not zabaged_lines and log:
            log(
                "OSM dedup: varování – ZABAGED ZIP je, ale 0 cestovních linií; "
                "OSM pěšiny se neoříznou proti ZABAGED"
            )
    kept, dropped = filter_osm_items_against_zabaged(osm_items, zabaged_lines)
    kept, dropped_self = dedup_osm_prefer_wider(kept)
    dropped += dropped_self
    if log:
        by_hw: dict[str, int] = defaultdict(int)
        for _pts, hw in kept:
            by_hw[hw] += 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(by_hw.items())) or "—"
        log(
            f"OSM cesty: {len(kept)} ponecháno ({summary}), "
            f"{dropped} duplicit/krátkých"
            + (f" (z toho {dropped_self} OSM×OSM širší>užší)" if dropped_self else "")
            + f", {skipped} přeskočeno (tag)"
        )
        by_feat: dict[str, int] = defaultdict(int)
        for feat in features:
            by_feat[str((feat.get("properties") or {}).get("kind") or "?")] += 1
        if features:
            feat_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_feat.items()))
            log(f"OSM objekty: {len(features)} ({feat_summary})")
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
    feat_out = dest_dir / "features.geojson"
    feat_out.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}),
        encoding="utf-8",
    )
    return out if kept else (feat_out if features else None)


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


def build_osm_feature_parts(
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
) -> list[OomObjectPart]:
    gj_path = work_dir / "osm_paths" / "features.geojson"
    if not gj_path.is_file():
        return []
    data = json.loads(gj_path.read_text(encoding="utf-8"))
    grouped: dict[str, list[str]] = defaultdict(list)
    kind_codes: dict[str, str] = {}
    names = {
        "playground": "OSM hřiště (401)",
        "pitch": "OSM sportoviště (401)",
        "playground_equipment": "OSM herní prvky (531 ×)",
        "playground_marker": "OSM hřiště střed (531 ×)",
        "pitch_marker": "OSM sportoviště střed (531 ×)",
        "water_well_building": "OSM studniční objekty",
        "water_well": "OSM studny",
        "spring": "OSM prameny",
        "bench": "OSM lavičky",
        "info_board": "OSM informační tabule",
        "lamp": "OSM lampy",
        "picnic_table": "OSM stoly",
        "firepit": "OSM ohniště",
        "wetland": "OSM mokřad",
        "cave_entrance": "OSM vstup do jeskyně",
        "fence": "OSM ploty",
        "wall": "OSM zdi",
        "hedge": "OSM živé ploty",
        "barrier_point": "OSM brány/sloupky",
        "memorial": "OSM pomníky",
        "shelter": "OSM přístřešky",
        "fitness": "OSM fitness",
        "landmark_tree": "OSM významné stromy",
    }
    kind_order = (
        "water_well_building",
        "water_well",
        "spring",
        "playground",
        "pitch",
        "playground_marker",
        "pitch_marker",
        "playground_equipment",
        "wetland",
        "cave_entrance",
        "fence",
        "wall",
        "hedge",
        "shelter",
        "memorial",
        "landmark_tree",
        "info_board",
        "bench",
        "lamp",
        "picnic_table",
        "firepit",
        "barrier_point",
        "fitness",
    )
    symbol_cache: dict[str, int | None] = {}
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        kind = str(props.get("kind") or "")
        if not kind:
            continue
        code = feature_oom_code(kind, preset_id, str(props.get("oom_code") or ""))
        if not code:
            continue
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        if code not in symbol_cache:
            symbol_cache[code] = symbol_index_for_code(preset_id, scale, code)
        symbol_index = symbol_cache[code]
        if symbol_index is None:
            continue
        if gtype == "Point":
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue
            mx, my = projected_to_map_coord(
                float(coords[0]),
                float(coords[1]),
                ref_x=ref_x,
                ref_y=ref_y,
                scale=scale,
                grivation_deg=grivation_deg,
            )
            obj = _point_object(symbol_index, mx, my)
            if obj:
                grouped[kind].append(obj)
                kind_codes[kind] = code
        elif gtype == "LineString":
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
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
                grouped[kind].append(obj)
                kind_codes[kind] = code
        elif gtype == "Polygon":
            rings = geom.get("coordinates") or []
            if not rings:
                continue
            ring = rings[0]
            mapped = [
                projected_to_map_coord(
                    float(x),
                    float(y),
                    ref_x=ref_x,
                    ref_y=ref_y,
                    scale=scale,
                    grivation_deg=grivation_deg,
                )
                for x, y in ring
            ]
            obj = _area_object(symbol_index, mapped)
            if obj:
                grouped[kind].append(obj)
                kind_codes[kind] = code
    parts: list[OomObjectPart] = []
    for kind in kind_order:
        objects = grouped.get(kind) or []
        if objects:
            parts.append(
                OomObjectPart(
                    name=names.get(kind, f"OSM {kind_codes.get(kind, kind)}"),
                    objects_xml="\n".join(objects),
                    count=len(objects),
                )
            )
    return parts
