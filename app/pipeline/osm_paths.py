"""OSM pěšiny a objekty (studny, hřiště, lavičky, tabule, mokřad, …) do OOM."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile

from app.pipeline.crs_5514 import wgs84_to_projected, write_prj
from app.pipeline.fetch_openzu import USER_AGENT, VECTOR_FETCH_BUFFER_M, expand_bbox_wgs84
from app.pipeline.geom_clip import Bounds, clip_polyline, clip_ring, point_inside
from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import (
    MAP_COORD_DASH_POINT,
    OomObjectPart,
    _area_object_with_holes,
    _hole_rings_as_area_objects,
    _path_object,
    _point_object,
    _pyogrio_layer_rows,
    _wkb_parts,
)
from app.pipeline.oom_symbol_map import symbol_index_for_code
from app.tool_env import gis_subprocess_env, which_tool
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
# Ulice/silnice bereme ze ZABAGED (přesnější), ne z OSM – kromě režimu path_source=osm.
# pedestrian = náměstí / pěší zóny (hlavně sprint).
OSM_HIGHWAYS = frozenset(
    {"path", "footway", "steps", "bridleway", "cycleway", "track", "pedestrian"}
)
# Jen režim „pouze OSM“: silnice a ulice taky z OSM (ZABAGED cesty v omap vypnout).
OSM_ROAD_HIGHWAYS = frozenset(
    {
        "motorway",
        "motorway_link",
        "trunk",
        "trunk_link",
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
        "tertiary",
        "tertiary_link",
        "unclassified",
        "residential",
        "living_street",
        "service",
        "road",
    }
)
# Syntetické šířky po refine (rank 0→road_1 … rank 3→road_4).
ROAD_DRAW_HIGHWAYS = frozenset({f"road_{i}" for i in range(1, 5)})
TRACK_DRAW_HIGHWAYS = frozenset({"track", "track_fast", "track_slow"})
# highway base → výchozí rank 0–3 (před lanes/surface).
_ROAD_BASE_RANK: dict[str, int] = {
    "motorway": 3,
    "motorway_link": 3,
    "trunk": 3,
    "trunk_link": 3,
    "primary": 3,
    "primary_link": 3,
    "secondary": 2,
    "secondary_link": 2,
    "tertiary": 2,
    "tertiary_link": 2,
    "unclassified": 1,
    "residential": 1,
    "living_street": 1,
    "road": 1,
    "service": 0,
}
_PAVED_ROAD_BUMP = frozenset(
    {
        "residential",
        "unclassified",
        "living_street",
        "tertiary",
        "tertiary_link",
        "secondary",
        "secondary_link",
    }
)

PATH_SOURCE_MIXED = "mixed"
PATH_SOURCE_ZABAGED = "zabaged"
PATH_SOURCE_OSM = "osm"
PATH_SOURCE_CHOICES = frozenset(
    {PATH_SOURCE_MIXED, PATH_SOURCE_ZABAGED, PATH_SOURCE_OSM}
)


def resolve_path_source(raw: str | None) -> str:
    val = (raw or PATH_SOURCE_MIXED).strip().lower()
    return val if val in PATH_SOURCE_CHOICES else PATH_SOURCE_MIXED


def osm_highway_set(path_source: str = PATH_SOURCE_MIXED) -> frozenset[str]:
    if resolve_path_source(path_source) == PATH_SOURCE_OSM:
        return OSM_HIGHWAYS | OSM_ROAD_HIGHWAYS
    return OSM_HIGHWAYS

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
# Při path_source=osm vynechat z OOM (zůstanou ve ZIPu zabaged/ pro ruční import).
ZABAGED_OMIT_PATH_LAYERS = ZABAGED_PATH_LAYERS | frozenset({"Tunel"})
# Priorita OSM / sprint: ořezávat OSM jen proti pevným komunikacím (ne Pesina/Cesta).
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
# Sprint OOM: tyto ZABAGED vrstvy ustoupí OSM (KP PNG beze změny).
ZABAGED_PATH_LAYERS_OSM_FIRST = frozenset({"Pesina", "Cesta", "Zabrana"})
NEAR_M = 25.0  # zpětná kompatibilita testů / starších volání
# Stejná středová čára (ne „něco v okolí“): OSM a ZABAGED přes sebe.
MATCH_M = 6.0
COVER_DROP = 0.70
# Min. |cos| úhlu tečen – paralelní i protisměr; kolmá cesta se neshoduje.
MIN_DIR_DOT = 0.5
OVERLAP_DROP = 0.45  # alias COVER_DROP pro stará volání
SAMPLE_M = 5.0
MIN_LENGTH_M = 1.0
# Společný práh – odřízne jen drobný šum, ne krátké lávky/spojky.
MIN_LENGTH_SHORT_M = 1.0
MIN_LENGTH_FOOT_M = 1.0
# Syntetický highway po načtení OSM (bridge=* na way).
# Hodnota ``bridge`` nebo ``bridge:<base>`` (base = původní highway pro značku).
OSM_BRIDGE_HIGHWAY = "bridge"


def is_bridge_highway(highway: str) -> bool:
    hw = (highway or "").lower()
    return hw == OSM_BRIDGE_HIGHWAY or hw.startswith(f"{OSM_BRIDGE_HIGHWAY}:")


def bridge_highway(base_highway: str) -> str:
    """Ulož most s původním highway (sprint kreslí jako navazující cestu)."""
    base = (base_highway or "path").lower() or "path"
    if is_bridge_highway(base):
        base = "path"
    return f"{OSM_BRIDGE_HIGHWAY}:{base}"


def path_draw_highway(highway: str) -> str:
    """Highway pro OOM značku (u mostu vrátí původní typ cesty)."""
    hw = (highway or "path").lower()
    if hw.startswith(f"{OSM_BRIDGE_HIGHWAY}:"):
        return hw.split(":", 1)[1] or "path"
    if hw == OSM_BRIDGE_HIGHWAY:
        return "path"
    return hw or "path"
GRID_M = 30.0
# crossing / přechody bereme (pod silnicemi); dřív se zahazovaly.
SKIP_FOOTWAY_ALWAYS = frozenset()
SKIP_CYCLEWAY = frozenset({"sidewalk", "crossing", "lane", "share_busway", "track"})
# Zpětná kompatibilita testů / starších importů (sidewalk jen když allow_sidewalk=False).
SKIP_FOOTWAY = frozenset({"sidewalk"})
# Lineární railway=platform (2 uzly) → buffer na plochu.
PLATFORM_LINE_HALF_WIDTH_M = 1.5

# Plochy → OOM zpevněná (501 / 501.1), ne žlutá 401.
# leisure=track = běžecká dráha (ne highway=track).
OSM_PAVED_AREA_LEISURE = frozenset(
    {"playground", "pitch", "track", "sports_centre", "ice_rink", "multi"}
)
# area:highway=* – obrys plochy chodníku / pěší (micromapping vedle střednice).
_AREA_HIGHWAY_WALK = frozenset(
    {"footway", "pedestrian", "path", "steps", "cycleway"}
)


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
    path_source: str = PATH_SOURCE_MIXED,
) -> str:
    bbox = f"{south},{west},{north},{east}"
    # Jedna regex vrstva – méně Overpass zátěže než 6 samostatných way[...].
    hw = "|".join(sorted(osm_highway_set(path_source)))
    paved = "|".join(sorted(OSM_PAVED_AREA_LEISURE))
    area_hw = "|".join(sorted(_AREA_HIGHWAY_WALK))
    parts = [
        f'way["highway"~"^({hw})$"]({bbox});',
        f'way["man_made"="boardwalk"]({bbox});',
        f'way["man_made"="water_well"]({bbox});',
        f'node["man_made"="water_well"]({bbox});',
        f'node["amenity"="fountain"]({bbox});',
        f'way["amenity"="fountain"]({bbox});',
        f'node["natural"="spring"]({bbox});',
        f'way["natural"="spring"]({bbox});',
        f'way["leisure"~"^({paved})$"]({bbox});',
        # Parkoviště → zpevněná plocha 501 (stejně jako hřiště).
        f'way["amenity"="parking"]({bbox});',
        f'relation["type"="multipolygon"]["amenity"="parking"]({bbox});',
        # Pěší zóna / náměstí jako plocha (ne linie).
        f'way["highway"="pedestrian"]["area"="yes"]({bbox});',
        f'relation["type"="multipolygon"]["highway"="pedestrian"]({bbox});',
        # Plocha chodníku / pěší (area:highway) – micromapping, vč. děr (relation).
        f'way["area:highway"~"^({area_hw})$"]({bbox});',
        f'relation["type"="multipolygon"]["area:highway"~"^({area_hw})$"]({bbox});',
        # Nástupiště (tram/vlak/bus) → zpevněná plocha 501.
        f'way["railway"="platform"]({bbox});',
        f'way["public_transport"="platform"]({bbox});',
        f'way["highway"="platform"]({bbox});',
        f'relation["type"="multipolygon"]["railway"="platform"]({bbox});',
        f'relation["type"="multipolygon"]["public_transport"="platform"]({bbox});',
        f'way["natural"="wetland"]({bbox});',
        # Vodní plochy → OOM 301 (nepřekonatelné). ČÚZK často nemá celou nádrž.
        f'way["natural"="water"]({bbox});',
        f'way["landuse"~"^(reservoir|basin)$"]({bbox});',
        # Obdělávaná půda → ISOM 412 (zdroj OSM, ne ZABAGED).
        f'way["landuse"="farmland"]({bbox});',
        # Zahrady → oliva 520 (vč. multipolygon relation s dírou na dům).
        f'way["leisure"="garden"]({bbox});',
        f'relation["type"="multipolygon"]["leisure"="garden"]({bbox});',
        f'node["natural"="cave_entrance"]({bbox});',
        f'way["natural"="cave_entrance"]({bbox});',
        # Posed / vodojem (les + MTBO; ve sprintu taky OK).
        f'node["amenity"="hunting_stand"]({bbox});',
        f'node["man_made"="water_tower"]({bbox});',
        f'way["man_made"="water_tower"]({bbox});',
        # Elektrické vedení + sloupy (DashPoint fousy v OOM).
        f'way["power"~"^(line|minor_line)$"]({bbox});',
        f'node["power"~"^(tower|pole)$"]({bbox});',
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
    # Budovy vždy (osm/budovy.geojson) – ne do auto OOM.
    parts.extend(
        [
            f'way["building"]({bbox});',
            f'relation["type"="multipolygon"]["building"]({bbox});',
        ]
    )
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
# Povrch / výplň uvnitř leisure=playground – ne samostatný křížek.
_PLAYGROUND_SURFACE = frozenset({"sandpit", "no"})
_BARRIER_LINES = frozenset({"fence", "wall", "hedge", "retaining_wall"})
_BARRIER_POINTS = frozenset(
    {"gate", "bollard", "stile", "cycle_barrier", "block", "lift_gate"}
)
# Volitelné skupiny (rozšířená nastavení).
_BENCH_KINDS = frozenset({"bench", "info_board", "picnic_table", "firepit"})
_LAMP_KINDS = frozenset({"lamp"})
_PLAYGROUND_EQUIPMENT_KINDS = frozenset({"playground_equipment"})
# Plochy z OSM_PAVED_AREA_LEISURE / pěší zóna / parkoviště / nástupiště → kind … (501).
_PAVED_AREA_KINDS = frozenset(
    {"playground", "pitch", "pedestrian_area", "parking", "platform"}
)
# OSM plochy s ořezem proti ZABAGED (priorita ZABAGED).
# farmland (412) se neořezává – zdroj je OSM, OrnaPuda se do OOM neimportuje.
_OSM_AREA_DEDUP_LAYERS: dict[str, frozenset[str]] = {
    "water_body": frozenset({"VodniPlocha"}),
    "parking": frozenset({"ParkovisteOdpocivka"}),
}
# Budovy z OSM do auto OOM i do osm/OSM_budovy.shp (ruční import).
_OSM_BUILDING_KINDS = frozenset({"building", "water_well_building"})
_CLOSED_AREA_KINDS = frozenset(
    {"wetland", "water_body", "farmland", "garden", "building", "water_well_building"}
) | _PAVED_AREA_KINDS
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
        "hunting_stand",
        "water_tower",
    }
)
_LINE_FEATURE_KINDS = frozenset(
    {"fence", "wall", "hedge", "power_line", "power_line_major"}
)
# Sloupy/stožáry OSM → DashPoint na vedení (MapCoord flag 32).
POWER_SUPPORT_MATCH_M = 1.5
POWER_MAJOR_VOLTAGE_V = 110_000
ZABAGED_OMIT_WHEN_OSM_POWER = frozenset({"ElektrickeVedeni"})


def _is_boardwalk(tags: dict) -> bool:
    return (
        (tags.get("footway") or "").lower() == "boardwalk"
        or (tags.get("man_made") or "").lower() == "boardwalk"
        or (tags.get("bridge") or "").lower() == "boardwalk"
    )


def _is_osm_bridge(tags: dict) -> bool:
    """OSM bridge=* (ne boardwalk) – i krátké lávky chceme v mapě."""
    if _is_boardwalk(tags):
        return False
    bridge = (tags.get("bridge") or "").lower()
    return bool(bridge) and bridge not in {"no", "false", "0"}


def path_min_length_m(highway: str) -> float:
    """Min. délka střednice před zahozením (krátké lávky/schody/spojky)."""
    hw = (highway or "path").lower()
    if is_bridge_highway(hw) or hw in {"steps", "sidewalk"}:
        return MIN_LENGTH_SHORT_M
    draw = path_draw_highway(hw)
    if draw in {"footway", "path", "cycleway", "bridleway", "pedestrian"}:
        return MIN_LENGTH_FOOT_M
    return MIN_LENGTH_M


def _is_osm_building(tags: dict) -> bool:
    building = (tags.get("building") or "").lower()
    return bool(building) and building not in {"no", "false", "0"}


# Zpevněný povrch – ve sprintu kreslíme jako chodník (501.6), ne jako 507.
_PAVED_SURFACES = frozenset(
    {
        "asphalt",
        "paved",
        "concrete",
        "paving_stones",
        "sett",
        "cobblestone",
        "concrete:plates",
        "concrete:lanes",
        "chipseal",
    }
)


def _is_paved_surface(tags: dict) -> bool:
    surface = (tags.get("surface") or "").lower()
    if not surface:
        return False
    if surface in _PAVED_SURFACES:
        return True
    # „asphalt;paving_stones“ apod.
    return any(part.strip() in _PAVED_SURFACES for part in surface.split(";"))


def sprint_line_highway(tags: dict, highway: str) -> str:
    """Sidewalk / zpevněný footway / pěší zóna → vnitřní druh ``sidewalk`` (zpevněná)."""
    hw = (highway or "").lower()
    footway = (tags.get("footway") or "").lower()
    if footway == "sidewalk" or (hw == "footway" and _is_paved_surface(tags)):
        return "sidewalk"
    # highway=pedestrian (i liniová pěší zóna) – typicky zpevněná; surface=paving_stones apod.
    if hw == "pedestrian":
        return "sidewalk"
    return hw or "path"


def _lanes_total(tags: dict) -> int | None:
    """Počet jízdních pruhů (lanes, nebo forward+backward)."""
    raw = (tags.get("lanes") or "").strip()
    if raw:
        try:
            n = int(float(raw.replace(",", ".")))
            if n > 0:
                return n
        except ValueError:
            pass
    fwd = (tags.get("lanes:forward") or "").strip()
    back = (tags.get("lanes:backward") or "").strip()
    if not fwd and not back:
        return None
    total = 0
    for part in (fwd, back):
        if not part:
            continue
        try:
            total += int(float(part.replace(",", ".")))
        except ValueError:
            return None
    return total if total > 0 else None


def road_width_rank(tags: dict, highway: str) -> int:
    """Rank 0–3 → sprint footprint 1.4/2/3/4 m (heavy).

    Base z OSM highway; +1 při lanes≥2, jinak +1 u zpevněného surface u běžných ulic.
    """
    hw = path_draw_highway(highway).lower()
    if hw in ROAD_DRAW_HIGHWAYS:
        try:
            return max(0, min(3, int(hw.split("_", 1)[1]) - 1))
        except ValueError:
            return 1
    base = _ROAD_BASE_RANK.get(hw, 1)
    lanes = _lanes_total(tags)
    if lanes is not None and lanes >= 2:
        base += 1
    elif _is_paved_surface(tags) and hw in _PAVED_ROAD_BUMP:
        base += 1
    return max(0, min(3, base))


def refine_track_highway(tags: dict) -> str:
    """tracktype / surface → track_fast | track | track_slow."""
    tt = (tags.get("tracktype") or "").lower()
    if tt == "grade1" or _is_paved_surface(tags):
        return "track_fast"
    if tt in {"grade4", "grade5"}:
        return "track_slow"
    # grade2/3 i netagované → stejné chování jako dřívější „track“.
    return "track"


def refine_path_highway(tags: dict, highway: str) -> str:
    """Silnice → road_1..road_4; track → track_fast/track/track_slow; jinak beze změny."""
    hw = (highway or "path").lower() or "path"
    draw = path_draw_highway(hw)
    if draw in OSM_ROAD_HIGHWAYS:
        rank = road_width_rank(tags, draw)
        return f"road_{rank + 1}"
    if draw == "track":
        return refine_track_highway(tags)
    return hw


def is_road_draw_highway(highway: str) -> bool:
    draw = path_draw_highway(highway)
    return draw in OSM_ROAD_HIGHWAYS or draw in ROAD_DRAW_HIGHWAYS


def _is_pedestrian_area_tags(tags: dict) -> bool:
    """Pěší zóna / náměstí jako plocha (highway=pedestrian + area nebo multipolygon)."""
    if (tags.get("highway") or "").lower() != "pedestrian":
        return False
    area = (tags.get("area") or "").lower()
    if area in {"yes", "true", "1"}:
        return True
    return (tags.get("type") or "").lower() == "multipolygon"


def _is_area_highway_paved_tags(tags: dict) -> bool:
    """area:highway=footway|… – plocha chodníku; zpevněný surface, nebo typicky chodník/pěší."""
    ah = (tags.get("area:highway") or "").lower()
    if ah not in _AREA_HIGHWAY_WALK:
        return False
    if _is_paved_surface(tags):
        return True
    # Chodník / pěší zóna je skoro vždy zpevněná i bez surface=*.
    return ah in {"footway", "pedestrian"}


def _is_platform_area_tags(tags: dict) -> bool:
    """Tram/vlak/bus nástupiště jako zpevněná plocha (railway/public_transport/highway=platform)."""
    if (tags.get("railway") or "").lower() == "platform":
        return True
    if (tags.get("public_transport") or "").lower() == "platform":
        return True
    return (tags.get("highway") or "").lower() == "platform"


def _max_voltage_v(tags: dict) -> int | None:
    """Nejvyšší voltage=* v voltech (``110000;22000`` → 110000)."""
    raw = (tags.get("voltage") or "").strip()
    if not raw:
        return None
    best: int | None = None
    for part in raw.replace(",", ";").split(";"):
        part = part.strip().lower().replace(" ", "")
        if not part:
            continue
        mult = 1
        if part.endswith("kv"):
            part = part[:-2]
            mult = 1000
        elif part.endswith("v"):
            part = part[:-1]
        try:
            val = int(float(part) * mult)
        except ValueError:
            continue
        if val > 0 and (best is None or val > best):
            best = val
    return best


def _int_tag(tags: dict, key: str) -> int | None:
    raw = (tags.get(key) or "").strip()
    if not raw:
        return None
    try:
        n = int(float(raw.split(";")[0].strip()))
    except ValueError:
        return None
    return n if n > 0 else None


def _is_major_power_line(tags: dict) -> bool:
    """Major = vysoké napětí / víc okruhů / víc fází; minor_line vždy ne."""
    power = (tags.get("power") or "").lower()
    if power == "minor_line":
        return False
    if power != "line":
        return False
    voltage = _max_voltage_v(tags)
    if voltage is not None and voltage >= POWER_MAJOR_VOLTAGE_V:
        return True
    circuits = _int_tag(tags, "circuits")
    if circuits is not None and circuits >= 2:
        return True
    cables = _int_tag(tags, "cables")
    if cables is not None and cables >= 6:
        return True
    return False


def _dash_indices_near_supports(
    pts: list[tuple[float, float]],
    supports: list[tuple[float, float]],
    *,
    match_m: float = POWER_SUPPORT_MATCH_M,
) -> list[int]:
    """Indexy vrcholů way blízko power=tower/pole."""
    if not pts or not supports:
        return []
    match_m2 = match_m * match_m
    out: list[int] = []
    for i, (x, y) in enumerate(pts):
        for sx, sy in supports:
            dx = x - sx
            dy = y - sy
            if dx * dx + dy * dy <= match_m2:
                out.append(i)
                break
    return out


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
    amenity = (tags.get("amenity") or "").lower()
    tourism = (tags.get("tourism") or "").lower()
    information = (tags.get("information") or "").lower()
    natural = (tags.get("natural") or "").lower()
    highway = (tags.get("highway") or "").lower()
    playground = (tags.get("playground") or "").lower()
    barrier = (tags.get("barrier") or "").lower()
    historic = (tags.get("historic") or "").lower()
    denotation = (tags.get("denotation") or "").lower()
    landuse = (tags.get("landuse") or "").lower()
    water = (tags.get("water") or "").lower()
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
        # Budova s tabulemi → 521 (ne křížek); jinak info board.
        if not _is_osm_building(tags):
            return "info_board", "531"
    if natural == "wetland":
        return "wetland", "308"
    if natural == "cave_entrance":
        return "cave_entrance", "203.1"
    if natural == "spring":
        return "spring", "312"
    # Vodní plocha / nádrž / basin → nepřekonatelné vodní těleso (301).
    # Stačí natural=water (v ČR často bez water=reservoir); ořez proti VodniPlocha.
    if (
        natural == "water"
        or landuse in {"reservoir", "basin"}
        or water in {"reservoir", "basin", "pond", "lake", "lagoon", "oxbow", "moat"}
    ):
        if is_node:
            return None
        return "water_body", "301"
    # Obdělávaná půda (ISOM 412) – kreslí se z OSM, pod KP vegetací.
    if landuse == "farmland":
        if is_node:
            return None
        return "farmland", "412"
    # Zahrada (ISSprOM/ISOM 520 oliva) – nepřístupné / soukromé plochy.
    if leisure == "garden":
        if is_node:
            return None
        return "garden", "520"
    # Hřiště / sportoviště / dráha / … = zpevněná plocha (501), ne žlutá 401.
    # S building=* jde o budovu (521), ne o zpevněnou plochu – dřív se zahodilo.
    if leisure in OSM_PAVED_AREA_LEISURE and not _is_osm_building(tags):
        if is_node:
            return None
        kind = "playground" if leisure == "playground" else "pitch"
        return kind, "501"
    # Parkoviště (vč. street_side) → zpevněná 501; budova garáže zůstane 521.
    if amenity == "parking" and not _is_osm_building(tags):
        if is_node:
            return None
        return "parking", "501"
    # Pěší zóna / náměstí (area) → zpevněná 501; linie bez area zůstane cestou.
    if _is_pedestrian_area_tags(tags) and not is_node:
        if _is_osm_building(tags):
            return "building", "521"
        return "pedestrian_area", "501"
    # area:highway=footway (+ surface) – plocha chodníku (např. relation/19273440).
    if _is_area_highway_paved_tags(tags) and not is_node:
        return "pedestrian_area", "501"
    # Nástupiště (tram/vlak/bus) → zpevněná 501 (např. way/180964461).
    if _is_platform_area_tags(tags) and not is_node:
        return "platform", "501"
    if man_made == "water_well" or amenity == "fountain":
        if _is_osm_building(tags):
            return "water_well_building", "521"
        return "water_well", "311"
    # Posed (amenity=hunting_stand) → ISOM 531 × / MTBO 539.
    if amenity == "hunting_stand":
        return "hunting_stand", "531"
    # Vodojem / vysoká věž – vždy bod (i když je to way), ne plocha budovy.
    if man_made == "water_tower":
        return "water_tower", "524"
    # Elektrické vedení (OSM) – major/minor; sloupy se neklasifikují (jen DashPoint).
    power = (tags.get("power") or "").lower()
    if power in {"line", "minor_line"} and not is_node:
        if _is_major_power_line(tags):
            return "power_line_major", "511"
        return "power_line", "510"
    # OSM budova (kavárna/kiosk/building=yes) → 521; ZABAGED má přednost v dedupu.
    if not is_node and _is_osm_building(tags):
        return "building", "521"
    return None


def feature_oom_code(kind: str, preset_id: str, stored_code: str = "") -> str:
    """OOM kód podle druhu objektu a presetu (sprint / les / MTBO)."""
    sprint = preset_id.startswith("sprint")
    mtbo = preset_id.startswith("mtbo")
    if kind == "cave_entrance":
        if sprint:
            return "203.1"
        if mtbo:
            return "205"
        return "203.2"
    if kind == "fence":
        if sprint:
            return "518"
        if mtbo:
            return "522"
        return "516"
    if kind == "wall":
        if sprint:
            return "513.2"
        if mtbo:
            return "521"  # ISMTBOM Stone wall
        return "513"
    if kind == "hedge":
        return "518" if sprint else "416"
    if kind == "power_line_major":
        # ISOM/ISSprOM 511; ISMTBOM 517 Major power line.
        return "517" if mtbo else "511"
    if kind == "power_line":
        # ISOM/ISSprOM 510; ISMTBOM 516 Power line.
        return "516" if mtbo else "510"
    if kind in _PAVED_AREA_KINDS:
        # Zpevněná plocha – žlutá 401 splyne se ZABAGED open land.
        if sprint:
            return "501"
        if mtbo:
            return "529"
        return "501.1"
    if kind == "water_body":
        return "301"
    if kind == "farmland":
        # ISOM 412 Cultivated land; ISMTBOM 412 = Orchard → 415.
        return "415" if mtbo else "412"
    if kind == "garden":
        # ISOM/ISSprOM 520 oliva; ISMTBOM 527 Settlement.
        return "527" if mtbo else "520"
    if kind == "building" or kind == "water_well_building":
        return "526" if mtbo else "521"
    if kind == "water_well":
        return "312" if mtbo else "311"
    if kind == "spring":
        return "313" if mtbo else "312"
    if kind == "wetland":
        return "310" if mtbo else "308"
    if kind == "memorial":
        return "537" if mtbo else "526"
    if kind == "shelter":
        # ISOM canopy 522; ISMTBOM 522 = Fence → special man-made.
        return "539" if mtbo else "522"
    if kind == "landmark_tree":
        return "418" if mtbo else "417"
    if kind == "hunting_stand":
        # ISOM/ISSprOM 531 ×; ISMTBOM 531 = Firing range → 539.
        return "539" if mtbo else "531"
    if kind == "water_tower":
        # ISOM/ISSprOM 524 High tower; ISMTBOM 524 = High fence → 535.
        return "535" if mtbo else "524"
    if kind in {
        "bench",
        "info_board",
        "lamp",
        "picnic_table",
        "firepit",
        "playground_equipment",
        "barrier_point",
        "fitness",
    }:
        # ISOM 530/531; ISMTBOM ty kódy znamenají něco jiného → 539.
        return "539" if mtbo else ("530" if kind == "lamp" else "531")
    if stored_code:
        return stored_code
    defaults = {
        "bench": "531",
        "info_board": "531",
        "lamp": "530",
        "picnic_table": "531",
        "firepit": "531",
        # ISSprOM/ISOM prominent man-made ×.
        "playground_equipment": "531",
        "barrier_point": "531",
        "fitness": "531",
        "hunting_stand": "531",
        "water_tower": "524",
        "memorial": "526",
        "shelter": "522",
        "landmark_tree": "417",
        "wetland": "308",
        "water_body": "301",
        "farmland": "412",
        "garden": "520",
        "playground": "501",
        "pitch": "501",
        "pedestrian_area": "501",
        "parking": "501",
        "platform": "501",
        "building": "521",
        "water_well": "311",
        "water_well_building": "521",
        "spring": "312",
        "power_line": "510",
        "power_line_major": "511",
    }
    return defaults.get(kind, stored_code or "")


def parse_osm_api_map_xml(
    xml_text: str,
    *,
    highways: frozenset[str] | None = None,
) -> list[dict]:
    """Vyfiltruje highway + OSM objekty z OSM API map call (.osm XML)."""
    hw_set = highways if highways is not None else OSM_HIGHWAYS
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
        power = (tags.get("power") or "").lower()
        keep_support = power in {"tower", "pole"}
        if not classify_osm_feature(tags, geom="node") and not keep_support:
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
        is_path = hw in hw_set or _is_boardwalk(tags)
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
    path_source: str = PATH_SOURCE_MIXED,
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
        path_source=path_source,
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
                if (
                    e.get("type") in {"way", "node"}
                    and (
                        e.get("geometry")
                        or (e.get("type") == "node" and e.get("lat") is not None)
                    )
                )
                or (
                    e.get("type") == "relation"
                    and e.get("members")
                    and classify_osm_feature(e.get("tags") or {}, geom="way")
                    is not None
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
    path_source: str = PATH_SOURCE_MIXED,
    log=None,
) -> list[dict]:
    """Export z openstreetmap.org – API map call, pak filtr highway typů."""
    bbox = f"{west},{south},{east},{north}"
    url = f"{OSM_API_MAP_URL}?bbox={bbox}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=OSM_API_TIMEOUT_S) as resp:
        xml_text = resp.read().decode("utf-8")
    elements = parse_osm_api_map_xml(
        xml_text, highways=osm_highway_set(path_source)
    )
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
    path_source: str = PATH_SOURCE_MIXED,
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
        path_source=path_source,
        log=log,
    )
    if elements is not None:
        return elements
    try:
        if log:
            log("OSM Overpass selhal – zkouším Export API (api.openstreetmap.org)…")
        return _fetch_osm_api_map(
            west, south, east, north, path_source=path_source, log=log
        )
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


def _way_skip_reason(
    tags: dict,
    *,
    allow_sidewalk: bool = False,
    highways: frozenset[str] = OSM_HIGHWAYS,
) -> str | None:
    footway = (tags.get("footway") or "").lower()
    railway = (tags.get("railway") or "").lower()
    if railway in {"subway", "subway_entrance"}:
        return "metro"
    # Dřevěný chodník mapujeme jako pěšinu (ne jako most).
    if _is_boardwalk(tags):
        return None
    if footway in SKIP_FOOTWAY_ALWAYS:
        return "přejezd"
    if footway == "sidewalk" and not allow_sidewalk:
        return "chodník"
    if (tags.get("cycleway") or "").lower() in SKIP_CYCLEWAY:
        return "cyklo pruh/chodník"
    if (tags.get("area") or "").lower() == "yes":
        return "area"
    if (tags.get("indoor") or "").lower() == "yes":
        return "indoor"
    hw = (tags.get("highway") or "").lower()
    if hw not in highways:
        return "highway"
    # Čistě silniční cycleway u silnice – ne pěšina v lese.
    if hw == "cycleway" and (tags.get("foot") or "").lower() in {"no", "private"}:
        return "cycleway bez pěších"
    return None


def osm_oom_code(highway: str, preset_id: str) -> str:
    """ISOM/ISSprOM/ISMTBOM kód podle OSM highway (vč. road_1..4 / track_*).

    Sprint heavy footprint: 501.16–501.19 (1.4–4 m).
    Les: 502 široká / 503 silnice / 504 vozová.
    MTBO: 502 major / 503 minor; track 831/833, pěšiny 834.
    """
    raw = highway or "path"
    sprint = preset_id.startswith("sprint")
    mtbo = preset_id.startswith("mtbo")

    if is_bridge_highway(raw):
        # Sprint: stejná značka jako navazující cesta; les 512; MTBO 834.
        if sprint:
            return osm_oom_code(path_draw_highway(raw), preset_id)
        if mtbo:
            return "834"
        return "512"

    hw = path_draw_highway(raw)

    # Legacy OSM road names (starší geojson) → rank podle base (bez tagů).
    if hw in OSM_ROAD_HIGHWAYS:
        hw = f"road_{road_width_rank({}, hw) + 1}"

    if hw in ROAD_DRAW_HIGHWAYS:
        try:
            rank = int(hw.split("_", 1)[1]) - 1
        except ValueError:
            rank = 1
        rank = max(0, min(3, rank))
        if sprint:
            return ("501.16", "501.17", "501.18", "501.19")[rank]
        if mtbo:
            return "502" if rank >= 2 else "503"
        return "502" if rank >= 2 else ("504" if rank == 0 else "503")

    if hw == "steps":
        if sprint:
            return "532.7"
        if mtbo:
            return "843"
        return "532"

    if hw in TRACK_DRAW_HIGHWAYS:
        if hw == "track_fast":
            if sprint:
                return "505.1"
            if mtbo:
                return "831"
            return "504"
        if hw == "track_slow":
            if sprint:
                return "506"
            if mtbo:
                return "834"
            return "506"
        # track (medium / netagované)
        if sprint:
            return "505.1"
        if mtbo:
            return "833"
        return "504"

    if hw == "sidewalk":
        if sprint:
            return "501.6"
        if mtbo:
            return "529"
        return "501.1"

    if mtbo:
        return "834"
    return "506"


def highway_width_rank(highway: str) -> int:
    """Vyšší = širší / preferovanější při překryvu střednic."""
    hw = (highway or "path").lower()
    if is_bridge_highway(hw):
        return 32
    draw = path_draw_highway(hw)
    if draw in ROAD_DRAW_HIGHWAYS:
        try:
            return 50 + int(draw.split("_", 1)[1])
        except ValueError:
            return 52
    if draw in OSM_ROAD_HIGHWAYS:
        return {
            "motorway": 62,
            "motorway_link": 61,
            "trunk": 60,
            "trunk_link": 59,
            "primary": 58,
            "primary_link": 57,
            "secondary": 56,
            "secondary_link": 55,
            "tertiary": 54,
            "tertiary_link": 53,
            "unclassified": 52,
            "residential": 51,
            "living_street": 50,
            "service": 50,
            "road": 50,
        }.get(draw, 52)
    return {
        "track_fast": 42,
        "track": 40,
        "track_slow": 38,
        "steps": 35,
        "bridleway": 30,
        "cycleway": 25,
        "pedestrian": 20,
        "sidewalk": 12,
        "path": 10,
        "footway": 10,
    }.get(draw, 10)


def dedup_osm_prefer_wider(
    items: list[tuple[list[tuple[float, float]], str]],
    *,
    match_m: float = MATCH_M,
    cover_drop: float = COVER_DROP,
) -> tuple[list[tuple[list[tuple[float, float]], str]], int]:
    """OSM×OSM: při shodné střednici nechá širší (track > path/footway).

    Překryv se měří vůči **jedné** už ponechané linii (paralelní duplicita),
    ne vůči celé síti – jinak krátká spojka mezi dvěma cestami (Motol
    way/806853877 ~28 m) zmizí, protože ``MATCH_M`` ji „přikryje“ jen uzly.

    Lávky (``bridge``) se kvůli překryvu nikdy nezahazují.
    """
    ordered = sorted(
        items,
        key=lambda it: (
            -highway_width_rank(it[1]),
            -polyline_length(it[0]),
        ),
    )
    kept: list[tuple[list[tuple[float, float]], str]] = []
    dropped = 0
    for pts, hw in ordered:
        if polyline_length(pts) < path_min_length_m(hw):
            dropped += 1
            continue
        if not is_bridge_highway(hw) and kept:
            max_cover = 0.0
            for kpts, _khw in kept:
                idx = _SegmentIndex()
                idx.add_line(kpts)
                max_cover = max(
                    max_cover,
                    centerline_cover_fraction(pts, idx, match_m=match_m),
                )
                if max_cover >= cover_drop:
                    break
            if max_cover >= cover_drop:
                dropped += 1
                continue
        kept.append((pts, hw))
    return kept, dropped


def osm_way_to_5514(
    element: dict,
    *,
    allow_sidewalk: bool = False,
    highways: frozenset[str] = OSM_HIGHWAYS,
) -> list[tuple[float, float]] | None:
    tags = element.get("tags") or {}
    if _way_skip_reason(tags, allow_sidewalk=allow_sidewalk, highways=highways):
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


def _iter_polygon_rings_from_shp(shp_ref: str | Path):
    """Vnější prstence polygonů ze SHP (S-JTSK)."""
    try:
        import pyogrio.raw  # noqa: F401

        use_pyogrio = True
    except ImportError:
        use_pyogrio = False

    if use_pyogrio:
        for _props, wkb in _pyogrio_layer_rows(shp_ref):
            parts, _ = _wkb_parts(wkb)
            for part in parts:
                # Polygon exterior = closed line ring (viz _wkb_parts base_type 3).
                if part[0] == "line" and len(part) >= 3 and part[2]:
                    ring = [(float(x), float(y)) for x, y in part[1]]  # type: ignore[misc]
                    if len(ring) >= 3:
                        yield ring
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
        if name == "POLYGON":
            ring = geom.GetGeometryRef(0)
            if ring is not None:
                pts = [
                    (float(ring.GetX(i)), float(ring.GetY(i)))
                    for i in range(ring.GetPointCount())
                ]
                if len(pts) >= 3:
                    out.append(pts)
        elif name in {"MULTIPOLYGON", "GEOMETRYCOLLECTION"}:
            for i in range(geom.GetGeometryCount()):
                out.extend(emit(geom.GetGeometryRef(i)))
        return out

    for feature in layer:
        geom = feature.GetGeometryRef()
        for ring in emit(geom):
            yield ring


def _zabaged_polygons(
    zabaged_clean: Path,
    *,
    layers: frozenset[str],
    log=None,
) -> list[list[tuple[float, float]]]:
    """Načte polygonové vrstvy ZABAGED pro ořez OSM ploch."""
    polys: list[list[tuple[float, float]]] = []
    if not layers:
        return polys
    members = _zabaged_shp_members(zabaged_clean, layers=layers)
    if not members:
        return polys

    import shutil
    import tempfile

    stage = Path(tempfile.mkdtemp(prefix="osm_zab_poly_"))
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
        want = {n.lower() for n in layers}
        for shp in sorted(stage.glob("*.shp")):
            if shp.stem.lower() not in want:
                continue
            try:
                before = len(polys)
                polys.extend(_iter_polygon_rings_from_shp(shp))
                if log:
                    log(
                        f"OSM plocha dedup: {shp.stem} → {len(polys) - before} polygonů"
                    )
            except Exception as exc:
                if log:
                    log(f"OSM plocha dedup: {shp.name} selhalo ({exc})")
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return polys


def _point_in_ring(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    """Ray casting; ring může být otevřený i uzavřený."""
    n = len(ring)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-15) + xi
        ):
            inside = not inside
        j = i
    return inside


def _ring_centroid(ring: list[tuple[float, float]]) -> tuple[float, float]:
    pts = ring[:-1] if len(ring) >= 2 and ring[0] == ring[-1] else ring
    if not pts:
        return 0.0, 0.0
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def filter_osm_area_features_against_zabaged(
    features: list[dict],
    zabaged_clean: Path | None,
    *,
    log=None,
) -> tuple[list[dict], int]:
    """Zahodí OSM plochy, jejichž těžiště leží v prioritní ZABAGED vrstvě."""
    if not zabaged_clean or not zabaged_clean.is_file():
        return features, 0
    needed: set[str] = set()
    for feat in features:
        kind = str((feat.get("properties") or {}).get("kind") or "")
        needed |= set(_OSM_AREA_DEDUP_LAYERS.get(kind) or ())
    if not needed:
        return features, 0
    polys_by_layer: dict[str, list[list[tuple[float, float]]]] = {}
    for layer in sorted(needed):
        polys_by_layer[layer] = _zabaged_polygons(
            zabaged_clean, layers=frozenset({layer}), log=log
        )
    kept: list[dict] = []
    dropped = 0
    for feat in features:
        props = feat.get("properties") or {}
        kind = str(props.get("kind") or "")
        layers = _OSM_AREA_DEDUP_LAYERS.get(kind)
        geom = feat.get("geometry") or {}
        if not layers or geom.get("type") != "Polygon":
            kept.append(feat)
            continue
        coords = (geom.get("coordinates") or [[]])[0] or []
        if len(coords) < 3:
            dropped += 1
            continue
        ring = [(float(x), float(y)) for x, y in coords]
        cx, cy = _ring_centroid(ring)
        covered = False
        for layer in layers:
            for poly in polys_by_layer.get(layer) or []:
                if _point_in_ring(cx, cy, poly):
                    covered = True
                    break
            if covered:
                break
        if covered:
            dropped += 1
            continue
        kept.append(feat)
    if log and dropped:
        log(
            f"OSM plocha dedup: {dropped} oříznuto (ZABAGED priorita: "
            + ", ".join(sorted(needed))
            + ")"
        )
    return kept, dropped


def filter_osm_against_zabaged(
    osm_lines: list[list[tuple[float, float]]],
    zabaged_lines: list[list[tuple[float, float]]],
    *,
    near_m: float = MATCH_M,
    overlap_drop: float = COVER_DROP,
) -> tuple[list[list[tuple[float, float]]], int]:
    """Zahodí jen celé OSM linie se shodnou střednicí ZABAGED; konce neořezává."""
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
        # Jen celá shoda střednice → pryč. Částečný souběh (konec u silnice) nechat.
        if centerline_cover_fraction(line, index, match_m=near_m) >= overlap_drop:
            dropped += 1
            continue
        kept.append(line)
    return kept, dropped


def filter_osm_items_against_zabaged(
    osm_items: list[tuple[list[tuple[float, float]], str]],
    zabaged_lines: list[list[tuple[float, float]]],
    *,
    near_m: float = MATCH_M,
    overlap_drop: float = COVER_DROP,
) -> tuple[list[tuple[list[tuple[float, float]], str]], int]:
    """Dedup celých střednic se zachováním highway tagu.

    Neořezává konce / úseky – jen zahodí linii, když je skoro celá shodná
    se ZABAGED. ``steps``, ``sidewalk`` a lávky (``bridge``) se proti
    ZABAGED nefiltrují.
    """
    def _keep_vs_zabaged(hw: str) -> bool:
        return is_bridge_highway(hw) or hw in {"steps", "sidewalk"}

    if not zabaged_lines:
        kept = [
            (pts, hw)
            for pts, hw in osm_items
            if polyline_length(pts) >= path_min_length_m(hw)
        ]
        return kept, len(osm_items) - len(kept)
    index = _SegmentIndex()
    for line in zabaged_lines:
        index.add_line(line)
    kept: list[tuple[list[tuple[float, float]], str]] = []
    dropped = 0
    for line, hw in osm_items:
        if polyline_length(line) < path_min_length_m(hw):
            dropped += 1
            continue
        if _keep_vs_zabaged(hw):
            kept.append((line, hw))
            continue
        if centerline_cover_fraction(line, index, match_m=near_m) >= overlap_drop:
            dropped += 1
            continue
        kept.append((line, hw))
    return kept, dropped


def filter_lines_against_centerlines(
    lines: list[list[tuple[float, float]]],
    blockers: list[list[tuple[float, float]]],
    *,
    near_m: float = MATCH_M,
    overlap_drop: float = COVER_DROP,
    min_length_m: float = MIN_LENGTH_M,
) -> tuple[list[list[tuple[float, float]]], int]:
    """Zahodí jen celé linie se shodnou střednicí ``blockers``; konce neořezává."""
    if not blockers:
        kept = [ln for ln in lines if polyline_length(ln) >= min_length_m]
        return kept, len(lines) - len(kept)
    index = _SegmentIndex()
    for line in blockers:
        index.add_line(line)
    kept: list[list[tuple[float, float]]] = []
    dropped = 0
    for line in lines:
        if polyline_length(line) < min_length_m:
            dropped += 1
            continue
        if centerline_cover_fraction(line, index, match_m=near_m) >= overlap_drop:
            dropped += 1
            continue
        kept.append(line)
    return kept, dropped


def load_osm_path_lines(work_dir: Path) -> list[list[tuple[float, float]]]:
    """Načte dedupované OSM cesty z ``work/osm_paths/paths.geojson``."""
    gj_path = work_dir / "osm_paths" / "paths.geojson"
    if not gj_path.is_file():
        return []
    try:
        data = json.loads(gj_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[list[tuple[float, float]]] = []
    for feat in data.get("features") or []:
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        out.append([(float(x), float(y)) for x, y in coords])
    return out


def _close_ring_5514(
    pts: list[tuple[float, float]],
) -> list[tuple[float, float]] | None:
    if len(pts) < 3:
        return None
    out = list(pts)
    if out[0] != out[-1]:
        if math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) < 1.5:
            out = out[:-1] + [out[0]]
        else:
            out.append(out[0])
    return out if len(out) >= 4 else None


def _line_buffer_ring_5514(
    pts: list[tuple[float, float]],
    *,
    half_width_m: float = PLATFORM_LINE_HALF_WIDTH_M,
) -> list[tuple[float, float]] | None:
    """Obdélníkový / pásový buffer kolem linie (lineární nástupiště = 2 uzly)."""
    if len(pts) < 2 or half_width_m <= 0:
        return None
    left: list[tuple[float, float]] = []
    right: list[tuple[float, float]] = []
    n = len(pts)
    for i in range(n):
        if i == 0:
            dx = pts[1][0] - pts[0][0]
            dy = pts[1][1] - pts[0][1]
        elif i == n - 1:
            dx = pts[i][0] - pts[i - 1][0]
            dy = pts[i][1] - pts[i - 1][1]
        else:
            dx = (pts[i][0] - pts[i - 1][0]) + (pts[i + 1][0] - pts[i][0])
            dy = (pts[i][1] - pts[i - 1][1]) + (pts[i + 1][1] - pts[i][1])
        length = math.hypot(dx, dy) or 1.0
        nx = -dy / length * half_width_m
        ny = dx / length * half_width_m
        x, y = pts[i]
        left.append((x + nx, y + ny))
        right.append((x - nx, y - ny))
    ring = left + list(reversed(right))
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring if len(ring) >= 4 else None


def _nodes_geometry_to_5514(geometry: list[dict] | None) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for node in geometry or []:
        lat = node.get("lat")
        lon = node.get("lon")
        if lat is None or lon is None:
            continue
        pts.append(wgs84_to_projected(float(lat), float(lon)))
    return pts


def osm_area_polygons_5514(
    element: dict,
    *,
    buffer_line_m: float | None = None,
) -> list[list[list[tuple[float, float]]]]:
    """Uzavřené plochy z way nebo multipolygon relation → seznam [outer, *holes].

    ``buffer_line_m``: když way nejde uzavřít (např. 2uzlové nástupiště),
    udělej pásový buffer kolem střednice.
    """
    el_type = element.get("type")
    if el_type == "way":
        pts = _nodes_geometry_to_5514(element.get("geometry"))
        ring = _close_ring_5514(pts)
        if not ring and buffer_line_m and len(pts) >= 2:
            ring = _line_buffer_ring_5514(pts, half_width_m=buffer_line_m)
        return [[ring]] if ring else []
    if el_type != "relation":
        return []
    outers: list[list[tuple[float, float]]] = []
    inners: list[list[tuple[float, float]]] = []
    for mem in element.get("members") or []:
        if mem.get("type") != "way":
            continue
        ring = _close_ring_5514(_nodes_geometry_to_5514(mem.get("geometry")))
        if not ring:
            continue
        role = (mem.get("role") or "outer").lower()
        if role == "inner":
            inners.append(ring)
        else:
            outers.append(ring)
    if not outers:
        return []
    if len(outers) == 1:
        return [[outers[0]] + inners]
    # Více outerů: díry nepřiřazujeme (vzácné) – každý outer zvlášť.
    return [[outer] for outer in outers]


def osm_feature_to_5514(
    element: dict,
) -> tuple[str, str, list[tuple[float, float]]] | None:
    """(kind, oom_code, ring_or_point) v S-JTSK, nebo None."""
    tags = element.get("tags") or {}
    # Metro / podzemní dráha – do podkladu nepatří.
    railway = (tags.get("railway") or "").lower()
    if railway in {"subway", "subway_entrance"}:
        return None
    geom = "node" if element.get("type") == "node" else "way"
    if element.get("type") == "relation":
        geom = "way"
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
    elif element.get("type") == "relation":
        return None  # plochy z relation řeší osm_area_rings_5514
    else:
        pts = _nodes_geometry_to_5514(element.get("geometry"))
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
    # Mokřad / nádrž / orná / hřiště / zahrada – jen uzavřená plocha.
    if kind in _CLOSED_AREA_KINDS:
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


def write_zabaged_omitting_layers(
    src_zip: Path,
    dest_zip: Path,
    omit_layers: frozenset[str],
) -> Path:
    """Zkopíruje ZABAGED ZIP bez sidecarů vrstev z ``omit_layers`` (stem shp)."""
    omit_stems = {name.lower() for name in omit_layers}
    keep_suffixes = {".shp", ".shx", ".dbf", ".prj", ".cpg"}
    if dest_zip.exists():
        dest_zip.unlink()
    with ZipFile(src_zip) as src, ZipFile(dest_zip, "w") as dest:
        for info in src.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name
            if not name or name.startswith("."):
                continue
            suffix = Path(name).suffix.lower()
            if suffix not in keep_suffixes:
                dest.writestr(info, src.read(info))
                continue
            if Path(name).stem.lower() in omit_stems:
                continue
            dest.writestr(info, src.read(info))
    return dest_zip


def osm_features_have_power_lines(work_dir: Path) -> bool:
    """True, pokud features.geojson obsahuje OSM elektrické vedení."""
    gj_path = work_dir / "osm_paths" / "features.geojson"
    if not gj_path.is_file():
        return False
    try:
        data = json.loads(gj_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    for feat in data.get("features") or []:
        kind = str((feat.get("properties") or {}).get("kind") or "")
        if kind in {"power_line", "power_line_major"}:
            return True
    return False


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
) -> None:
    highways = osm_highway_set(PATH_SOURCE_OSM)
    fetch_bbox = expand_bbox_wgs84(bbox_wgs84, VECTOR_FETCH_BUFFER_M)
    elements = fetch_osm_path_elements(
        fetch_bbox,
        include_benches=include_benches,
        include_lamps=include_lamps,
        include_playground_equipment=include_playground_equipment,
        osm_priority=osm_priority,
        path_source=PATH_SOURCE_OSM,
        log=log,
    )
    osm_items: list[tuple[list[tuple[float, float]], str]] = []
    features: list[dict] = []
    skipped = 0
    # Sloupy/stožáry pro DashPoint na vedení (nejsou samostatné OOM objekty).
    power_supports: list[tuple[float, float]] = []
    for el in elements:
        if el.get("type") != "node":
            continue
        tags = el.get("tags") or {}
        if (tags.get("power") or "").lower() not in {"tower", "pole"}:
            continue
        lat, lon = el.get("lat"), el.get("lon")
        if lat is None or lon is None:
            continue
        power_supports.append(wgs84_to_projected(float(lat), float(lon)))
    # Chodník / zpevněný footway bereme ve všech disciplínách (symbolika se liší).
    allow_sidewalk = True
    for el in elements:
        tags = el.get("tags") or {}
        # Metro do podkladu nepatří (ani jako cesta, ani jako objekt).
        railway = (tags.get("railway") or "").lower()
        if railway in {"subway", "subway_entrance"}:
            skipped += 1
            continue
        # power=tower/pole – jen podklady pro DashPoint, ne samostatný symbol.
        if (tags.get("power") or "").lower() in {"tower", "pole"}:
            continue
        el_geom = "node" if el.get("type") == "node" else "way"
        if el.get("type") == "relation":
            el_geom = "way"
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
            if kind in _CLOSED_AREA_KINDS:
                buf = (
                    PLATFORM_LINE_HALF_WIDTH_M if kind == "platform" else None
                )
                polygons = osm_area_polygons_5514(el, buffer_line_m=buf)
                if not polygons:
                    skipped += 1
                    continue
                code = classified[1]
                for rings in polygons:
                    coords = [[[x, y] for x, y in ring] for ring in rings]
                    features.append(
                        {
                            "type": "Feature",
                            "properties": {
                                "source": "osm",
                                "kind": kind,
                                "oom_code": code,
                            },
                            "geometry": {"type": "Polygon", "coordinates": coords},
                        }
                    )
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
            props: dict = {
                "source": "osm",
                "kind": kind,
                "oom_code": code,
            }
            if kind in {"power_line", "power_line_major"} and len(pts) >= 2:
                dash = _dash_indices_near_supports(pts, power_supports)
                if dash:
                    props["dash_indices"] = dash
            features.append(
                {
                    "type": "Feature",
                    "properties": props,
                    "geometry": geometry,
                }
            )
            continue
        if el.get("type") == "relation":
            skipped += 1
            continue
        pts = osm_way_to_5514(
            el, allow_sidewalk=allow_sidewalk, highways=highways
        )
        if pts is None:
            skipped += 1
            continue
        hw = ((el.get("tags") or {}).get("highway") or "").lower()
        if not hw and _is_boardwalk(tags):
            hw = "footway"
        if allow_sidewalk:
            hw = sprint_line_highway(tags, hw)
        elif not hw:
            hw = "path"
        # Šířka silnice / track podle lanes + surface (road_1..4, track_*).
        hw = refine_path_highway(tags, hw)
        # Krátké lávky (bridge=yes) – zachovej typ cesty pro sprint značku.
        if _is_osm_bridge(tags):
            hw = bridge_highway(hw or "path")
        # Uzavřená pěší zóna i bez area=yes → zpevněná plocha (ne střednicová pěšina).
        # Kontrola podle tagů – sprint_line_highway už pedestrian přemapuje na sidewalk.
        if (tags.get("highway") or "").lower() == "pedestrian" and len(pts) >= 3:
            ring = list(pts)
            if ring[0] != ring[-1]:
                if math.hypot(ring[0][0] - ring[-1][0], ring[0][1] - ring[-1][1]) < 1.5:
                    ring = ring[:-1] + [ring[0]]
                else:
                    ring = []
            if len(ring) >= 4 and ring[0] == ring[-1]:
                features.append(
                    {
                        "type": "Feature",
                        "properties": {
                            "source": "osm",
                            "kind": "pedestrian_area",
                            "oom_code": "501",
                        },
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[x, y] for x, y in ring]],
                        },
                    }
                )
                continue
        osm_items.append((pts, hw))
    zabaged_lines: list[list[tuple[float, float]]] = []
    if zabaged_clean and zabaged_clean.is_file():
        # Stejný dedup pro všechny disciplíny: OSM > Pesina/Cesta (Ulice zůstává).
        dedup_layers = ZABAGED_PATH_LAYERS_PRIORITY
        zabaged_lines = _zabaged_path_lines(
            zabaged_clean, log=log, layers=dedup_layers
        )
        if log:
            log(f"OSM dedup (OSM > Pesina/Cesta): {len(zabaged_lines)} ZABAGED linií")
        if not zabaged_lines and log:
            log(
                "OSM dedup: varování – ZABAGED ZIP je, ale 0 cestovních linií; "
                "OSM pěšiny se neoříznou proti ZABAGED"
            )

    # 1. Varianta OSM: všechny cesty vč. silnic, bez ořezu proti ZABAGED
    #    i bez OSM×OSM dedupu (ten je jen u kombinace – jinak krátká lávka
    #    u delšího tracku zmizí, Motol way/551847479).
    kept_osm = [
        (pts, hw)
        for pts, hw in osm_items
        if polyline_length(pts) >= path_min_length_m(hw)
    ]
    dropped_short_osm = len(osm_items) - len(kept_osm)

    # 2. Varianta MIXED: pěšiny + lávky, ořez proti ZABAGED + OSM×OSM širší>užší
    mixed_highways = osm_highway_set(PATH_SOURCE_MIXED) | frozenset({OSM_BRIDGE_HIGHWAY})
    osm_items_mixed = [
        (pts, hw)
        for pts, hw in osm_items
        if path_draw_highway(hw) in mixed_highways or is_bridge_highway(hw)
    ]
    kept_mixed, dropped_mixed = filter_osm_items_against_zabaged(osm_items_mixed, zabaged_lines)
    kept_mixed, dropped_self_mixed = dedup_osm_prefer_wider(kept_mixed)
    dropped_mixed += dropped_self_mixed

    features, dropped_areas = filter_osm_area_features_against_zabaged(
        features, zabaged_clean, log=log
    )
    if log:
        by_hw: dict[str, int] = defaultdict(int)
        for _pts, hw in kept_mixed:
            by_hw[hw] += 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(by_hw.items())) or "—"
        log(
            f"OSM cesty (mix): {len(kept_mixed)} ponecháno ({summary}), "
            f"{dropped_mixed} duplicit/krátkých"
            + (f" (z toho {dropped_self_mixed} OSM×OSM širší>užší)" if dropped_self_mixed else "")
            + f", {skipped} přeskočeno (tag)"
        )
        log(
            f"OSM cesty (jen OSM): {len(kept_osm)} ponecháno "
            f"(bez ZABAGED/OSM×OSM dedup"
            + (f", {dropped_short_osm} krátkých" if dropped_short_osm else "")
            + ")"
        )
        by_feat: dict[str, int] = defaultdict(int)
        for feat in features:
            by_feat[str((feat.get("properties") or {}).get("kind") or "?")] += 1
        if features or dropped_areas:
            feat_summary = (
                ", ".join(f"{k}={v}" for k, v in sorted(by_feat.items())) or "—"
            )
            extra = (
                f", {dropped_areas} ploch oříznuto (ZABAGED priorita)"
                if dropped_areas
                else ""
            )
            log(f"OSM objekty: {len(features)} ({feat_summary}){extra}")
    
    dest_dir = work_dir / "osm_paths"
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    def write_geojson(kept_list, filename):
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
                for line, hw in kept_list
            ],
        }
        (dest_dir / filename).write_text(json.dumps(gj), encoding="utf-8")

    write_geojson(kept_mixed, "paths_mixed.geojson")
    write_geojson(kept_osm, "paths_osm.geojson")
    # Lávky i do režimu jen-ZABAGED (krátké OSM bridge často v ČÚZK chybí).
    write_geojson(
        [(pts, hw) for pts, hw in kept_osm if is_bridge_highway(hw)],
        "paths_bridges.geojson",
    )
    # Zpětná kompatibilita (např. pro testy)
    write_geojson(kept_mixed, "paths.geojson")

    # OSM budovy: v features.geojson (auto OOM) i buildings.geojson (SHP do osm/).
    building_feats = [
        f
        for f in features
        if str((f.get("properties") or {}).get("kind") or "") in _OSM_BUILDING_KINDS
    ]
    (dest_dir / "buildings.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": building_feats}),
        encoding="utf-8",
    )

    feat_out = dest_dir / "features.geojson"
    feat_out.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}),
        encoding="utf-8",
    )


def highway_to_zabaged_vrstva(highway: str) -> str | None:
    """Mapování OSM highway → ZABAGED vrstva pro KP vectorconf (vrstva=…).

    ``None`` = neposílat do KP PNG (schody KP neumí – jen OOM 532).
    """
    hw = path_draw_highway(highway or "path")
    if hw == "steps":
        return None
    if hw in OSM_ROAD_HIGHWAYS or hw in ROAD_DRAW_HIGHWAYS:
        return "Ulice"  # KP road-path|503
    if hw in TRACK_DRAW_HIGHWAYS:
        return "Cesta"  # KP road-path|505
    if is_bridge_highway(highway or ""):
        return "Lavka"  # KP road-path|506
    # sidewalk / footway / path → KP pěšina (506)
    return "Pesina"  # KP road-path|506


def paths_geojson_for_kp(paths_gj: dict) -> dict:
    """GeoJSON pro KP: u každé linie `vrstva=Pesina|Cesta` (match vectorconf)."""
    features: list[dict] = []
    for feat in paths_gj.get("features") or []:
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        props = feat.get("properties") or {}
        hw = str(props.get("highway") or "path")
        vrstva = highway_to_zabaged_vrstva(hw)
        if not vrstva:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "vrstva": vrstva,
                    "highway": hw,
                    "source": "osm",
                },
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        )
    return {"type": "FeatureCollection", "features": features}


def write_osm_kp_zip(
    work_dir: Path,
    *,
    log=None,
) -> Path | None:
    """Sestaví plochý SHP ZIP pro Karttapullautin (druhý ZIP vedle ZABAGED).

    Čte hustší ``osm_paths/paths_osm.geojson`` (ne mixed/dedup). Při chybě ogr2ogr
    vrátí None – PNG zůstane jen ze ZABAGED, OOM OSM objekty beze změny.
    """
    paths_gj = work_dir / "osm_paths" / "paths_osm.geojson"
    if not paths_gj.is_file():
        # Zpětná kompatibilita pro starší joby
        for fallback in ("paths_mixed.geojson", "paths.geojson"):
            candidate = work_dir / "osm_paths" / fallback
            if candidate.is_file():
                paths_gj = candidate
                break
        else:
            return None
    try:
        data = json.loads(paths_gj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    kp_gj = paths_geojson_for_kp(data)
    n = len(kp_gj["features"])
    if n == 0:
        if log:
            log("OSM→KP PNG: žádné cesty")
        return None

    ogr2ogr = which_tool("ogr2ogr")
    if not ogr2ogr:
        if log:
            log("OSM→KP PNG: chybí ogr2ogr – cesty jen do OOM, ne na PNG")
        return None

    dest_zip = work_dir / "osm_kp.zip"
    stage = Path(tempfile.mkdtemp(prefix="osm_kp_"))
    try:
        gj_path = stage / "osm_paths_kp.geojson"
        gj_path.write_text(json.dumps(kp_gj), encoding="utf-8")
        shp = stage / "OSM_cesty.shp"
        cmd = [
            ogr2ogr,
            "-f",
            "ESRI Shapefile",
            "-overwrite",
            "-s_srs",
            "EPSG:5514",
            "-t_srs",
            "EPSG:5514",
            "-lco",
            "ENCODING=UTF-8",
            "-nlt",
            "LINESTRING",
            str(shp),
            str(gj_path),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=gis_subprocess_env(ogr2ogr),
        )
        if result.returncode != 0 or not shp.is_file():
            err = (result.stderr or result.stdout or "ogr2ogr failed").strip()
            if log:
                log(f"OSM→KP PNG: ogr2ogr selhal ({err[:200]})")
            return None
        write_prj(shp)
        if dest_zip.exists():
            dest_zip.unlink()
        with ZipFile(dest_zip, "w") as zf:
            for path in stage.iterdir():
                if path.suffix.lower() in {
                    ".shp",
                    ".shx",
                    ".dbf",
                    ".prj",
                    ".cpg",
                }:
                    zf.write(path, path.name)
        by_v: dict[str, int] = defaultdict(int)
        for feat in kp_gj["features"]:
            by_v[str((feat.get("properties") or {}).get("vrstva") or "?")] += 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(by_v.items()))
        if log:
            log(f"OSM→KP PNG: {n} linií ({summary}) → {dest_zip.name}")
        return dest_zip
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# Ruční skládání mapy (jako zabaged/): kind → (stem SHP, popis, výchozí ISOM kód).
OSM_MANUAL_LAYER_SPECS: dict[str, tuple[str, str, str]] = {
    "water_well": ("OSM_studny", "studny / kašny", "311"),
    "spring": ("OSM_prameny", "prameny", "312"),
    "water_body": ("OSM_voda", "vodní plochy", "301"),
    "farmland": ("OSM_orna", "obdělávaná půda", "412"),
    "garden": ("OSM_zahrady", "zahrady (oliva)", "520"),
    "playground": ("OSM_hriste", "hřiště (zpevněná)", "501"),
    "pitch": ("OSM_sport", "sportoviště (zpevněná)", "501"),
    "pedestrian_area": ("OSM_pesi_zony", "pěší zóny (zpevněná)", "501"),
    "parking": ("OSM_parkoviste", "parkoviště (zpevněná)", "501"),
    "platform": ("OSM_nastupiste", "nástupiště (zpevněná)", "501"),
    "playground_equipment": ("OSM_herni_prvky", "herní prvky", "531"),
    "wetland": ("OSM_mokrad", "mokřad", "308"),
    "cave_entrance": ("OSM_jeskyne", "vstup do jeskyně", "203.2"),
    "fence": ("OSM_ploty", "ploty", "516"),
    "wall": ("OSM_zdi", "zdi", "513"),
    "hedge": ("OSM_zive_ploty", "živé ploty", "416"),
    "power_line": ("OSM_vedeni", "elektrické vedení", "510"),
    "power_line_major": ("OSM_vedeni_velke", "velké elektrické vedení", "511"),
    "shelter": ("OSM_pristresky", "přístřešky", "522"),
    "memorial": ("OSM_pomniky", "pomníky", "526"),
    "landmark_tree": ("OSM_stromy", "významné stromy", "417"),
    "water_tower": ("OSM_veze", "vodojemy / vysoké věže", "524"),
    "hunting_stand": ("OSM_posedy", "posedy", "531"),
    "info_board": ("OSM_tabule", "informační tabule", "531"),
    "bench": ("OSM_lavicky", "lavičky", "531"),
    "lamp": ("OSM_lampy", "lampy", "530"),
    "picnic_table": ("OSM_stoly", "stoly", "531"),
    "firepit": ("OSM_ohniste", "ohniště", "531"),
    "barrier_point": ("OSM_brany", "brány / sloupky", "531"),
    "fitness": ("OSM_fitness", "fitness", "531"),
}


def _shapefile_nlt(features: list[dict]) -> str:
    types = {
        str((f.get("geometry") or {}).get("type") or "")
        for f in features
        if f.get("geometry")
    }
    types.discard("")
    if types <= {"Point", "MultiPoint"}:
        return "POINT"
    if types <= {"LineString", "MultiLineString"}:
        return "LINESTRING"
    if types <= {"Polygon", "MultiPolygon"}:
        return "POLYGON"
    return "GEOMETRY"


def _geojson_to_shapefile(
    features: list[dict],
    dest_shp: Path,
    *,
    nlt: str,
    log=None,
    label: str = "OSM",
) -> bool:
    """Zapíše Feature list do SHP (EPSG:5514). Při chybě/ogr2ogr vrátí False."""
    if not features:
        return False
    ogr2ogr = which_tool("ogr2ogr")
    if not ogr2ogr:
        if log:
            log(f"{label}→SHP: chybí ogr2ogr")
        return False
    dest_shp.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="osm_shp_"))
    try:
        gj_path = stage / "layer.geojson"
        gj_path.write_text(
            json.dumps({"type": "FeatureCollection", "features": features}),
            encoding="utf-8",
        )
        staged = stage / dest_shp.name
        cmd = [
            ogr2ogr,
            "-f",
            "ESRI Shapefile",
            "-overwrite",
            "-s_srs",
            "EPSG:5514",
            "-t_srs",
            "EPSG:5514",
            "-lco",
            "ENCODING=UTF-8",
            "-nlt",
            nlt,
            str(staged),
            str(gj_path),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=gis_subprocess_env(ogr2ogr),
        )
        if result.returncode != 0 or not staged.is_file():
            err = (result.stderr or result.stdout or "ogr2ogr failed").strip()
            if log:
                log(f"{label}→SHP: ogr2ogr selhal ({err[:200]})")
            return False
        write_prj(staged)
        for path in stage.iterdir():
            if path.suffix.lower() in {".shp", ".shx", ".dbf", ".prj", ".cpg"}:
                shutil.copy2(path, dest_shp.parent / path.name)
        return dest_shp.is_file()
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def write_osm_buildings_shapefile(
    work_dir: Path,
    *,
    log=None,
) -> Path | None:
    """Převede ``osm_paths/buildings.geojson`` na SHP pro import v OOM (CRT dialog)."""
    gj = work_dir / "osm_paths" / "buildings.geojson"
    if not gj.is_file():
        return None
    try:
        data = json.loads(gj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    feats = list(data.get("features") or [])
    if not feats:
        return None
    dest_dir = work_dir / "osm_paths" / "manual"
    shp = dest_dir / "OSM_budovy.shp"
    ok = _geojson_to_shapefile(
        feats, shp, nlt="POLYGON", log=log, label="OSM budovy"
    )
    if ok and log:
        log(f"OSM budovy→SHP: {len(feats)} polygonů → osm_paths/manual/")
    return shp if ok else None


def write_osm_manual_shapefiles(
    work_dir: Path,
    *,
    log=None,
) -> int:
    """SHP vrstvy pro ruční skládání mapy (obdoba zabaged/).

    Cesty, objekty (posedy, studny, …) a budovy → ``osm_paths/manual/`` (+ budovy/).
    """
    dest = work_dir / "osm_paths" / "manual"
    dest.mkdir(parents=True, exist_ok=True)
    written = 0

    paths_gj = work_dir / "osm_paths" / "paths_osm.geojson"
    if not paths_gj.is_file():
        paths_gj = work_dir / "osm_paths" / "paths.geojson"
    if paths_gj.is_file():
        try:
            pdata = json.loads(paths_gj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pdata = {}
        pfeats = list(pdata.get("features") or [])
        if pfeats and _geojson_to_shapefile(
            pfeats,
            dest / "OSM_cesty.shp",
            nlt="LINESTRING",
            log=log,
            label="OSM cesty",
        ):
            written += 1

    feat_gj = work_dir / "osm_paths" / "features.geojson"
    by_kind: dict[str, list[dict]] = defaultdict(list)
    if feat_gj.is_file():
        try:
            fdata = json.loads(feat_gj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fdata = {}
        for feat in fdata.get("features") or []:
            kind = str((feat.get("properties") or {}).get("kind") or "")
            if not kind or kind in _OSM_BUILDING_KINDS:
                continue
            by_kind[kind].append(feat)

    for kind, feats in sorted(by_kind.items()):
        spec = OSM_MANUAL_LAYER_SPECS.get(kind)
        stem = spec[0] if spec else f"OSM_{kind}"
        nlt = _shapefile_nlt(feats)
        if _geojson_to_shapefile(
            feats,
            dest / f"{stem}.shp",
            nlt=nlt,
            log=log,
            label=f"OSM {kind}",
        ):
            written += 1

    if write_osm_buildings_shapefile(work_dir, log=log):
        written += 1

    readme = dest / "README.txt"
    lines = [
        "OSM vrstvy pro ruční skládání mapy (obdoba zabaged/)",
        "====================================================",
        "",
        "Importuj vybrané SHP do OOM (File → Importovat…) a přiřaď symbol.",
        "Objekty už jsou i v .omap; sem patří pro volné poskládání / doladění.",
        "",
        "Vrstva              Typ        Doporučený symbol (les/sprint; MTBO se liší)",
        "-----              ---        ---------------------------------------------",
        "OSM_cesty          linie      506 (pěšina) / 501.* silnice – dle highway",
        "OSM_budovy         polygony   521 (les/sprint) nebo 526 (MTBO)",
    ]
    for kind, feats in sorted(by_kind.items()):
        spec = OSM_MANUAL_LAYER_SPECS.get(kind)
        if spec:
            stem, desc, code = spec
        else:
            stem, desc, code = f"OSM_{kind}", kind, "?"
        nlt = _shapefile_nlt(feats)
        geom = {"POINT": "body", "LINESTRING": "linie", "POLYGON": "polygony"}.get(
            nlt, "smíšené"
        )
        lines.append(f"{stem:<18} {geom:<10} {code}  ({desc})")
    lines.append("")
    lines.append("Souřadnice: EPSG:5514 (S-JTSK).")
    lines.append("")
    readme.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if log:
        log(f"OSM ruční vrstvy: {written} SHP → osm_paths/manual/")
    return written


def build_osm_path_parts(
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    clip_bounds: Bounds | None = None,
    path_source: str = PATH_SOURCE_MIXED,
) -> list[OomObjectPart]:
    if path_source == PATH_SOURCE_ZABAGED:
        filename = "paths_bridges.geojson"
    elif path_source == PATH_SOURCE_OSM:
        filename = "paths_osm.geojson"
    else:
        filename = "paths_mixed.geojson"
    gj_path = work_dir / "osm_paths" / filename
    if not gj_path.is_file():
        # Fallback for older jobs
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
            # Fallback: les/sprint 506; MTBO Path medium 834 (504–507 jsou skryté).
            fallback = "834" if preset_id.startswith("mtbo") else "506"
            if fallback not in symbol_cache:
                symbol_cache[fallback] = symbol_index_for_code(
                    preset_id, scale, fallback
                )
            symbol_index = symbol_cache[fallback]
        if symbol_index is None:
            continue
        line = [(float(x), float(y)) for x, y in coords]
        pieces = clip_polyline(line, clip_bounds) if clip_bounds else [line]
        for piece in pieces:
            mapped = [
                projected_to_map_coord(
                    x,
                    y,
                    ref_x=ref_x,
                    ref_y=ref_y,
                    scale=scale,
                    grivation_deg=grivation_deg,
                )
                for x, y in piece
            ]
            obj = _path_object(symbol_index, mapped)
            if obj:
                objects.append(obj)
    if not objects:
        return []
    part_name = (
        "OSM lávky"
        if path_source == PATH_SOURCE_ZABAGED
        else "OSM cesty"
    )
    return [
        OomObjectPart(
            name=part_name,
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
    clip_bounds: Bounds | None = None,
    aopk_tree_points: list[tuple[float, float]] | None = None,
    courtyard_olive: bool = False,
) -> list[OomObjectPart]:
    gj_path = work_dir / "osm_paths" / "features.geojson"
    if not gj_path.is_file():
        return []
    data = json.loads(gj_path.read_text(encoding="utf-8"))
    feats = list(data.get("features") or [])
    if aopk_tree_points:
        from app.pipeline.aopk_trees import filter_osm_landmark_trees_near_aopk

        feats, _dropped = filter_osm_landmark_trees_near_aopk(feats, aopk_tree_points)
    grouped: dict[str, list[str]] = defaultdict(list)
    kind_codes: dict[str, str] = {}
    courtyard_objects: list[str] = []
    names = {
        "building": "OSM budovy",
        "water_well_building": "OSM budovy (studny)",
        "playground": "OSM hřiště (501 zpevněná)",
        "pitch": "OSM sportoviště (501 zpevněná)",
        "pedestrian_area": "OSM pěší zóny (501 zpevněná)",
        "parking": "OSM parkoviště (501 zpevněná)",
        "platform": "OSM nástupiště (501 zpevněná)",
        "playground_equipment": "OSM herní prvky (531 ×)",
        "water_body": "OSM vodní nádrž (301)",
        "farmland": "OSM obdělávaná půda (412)",
        "garden": "OSM zahrady (520 oliva)",
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
        "power_line": "OSM elektrické vedení (510)",
        "power_line_major": "OSM velké elektrické vedení (511)",
        "barrier_point": "OSM brány/sloupky",
        "memorial": "OSM pomníky",
        "shelter": "OSM přístřešky",
        "fitness": "OSM fitness",
        "landmark_tree": "OSM významné stromy",
        "hunting_stand": "OSM posedy (531 ×)",
        "water_tower": "OSM vodojemy / vysoké věže (524)",
    }
    kind_order = (
        "building",
        "water_well_building",
        "water_well",
        "spring",
        "water_body",
        "farmland",
        "garden",
        "playground",
        "pitch",
        "pedestrian_area",
        "parking",
        "platform",
        "playground_equipment",
        "wetland",
        "cave_entrance",
        "fence",
        "wall",
        "hedge",
        "power_line",
        "power_line_major",
        "shelter",
        "memorial",
        "landmark_tree",
        "water_tower",
        "hunting_stand",
        "info_board",
        "bench",
        "lamp",
        "picnic_table",
        "firepit",
        "barrier_point",
        "fitness",
    )
    symbol_cache: dict[str, int | None] = {}
    mtbo = preset_id.startswith("mtbo")
    olive_code = "527" if mtbo else "520"
    olive_index = (
        symbol_index_for_code(preset_id, scale, olive_code) if courtyard_olive else None
    )

    def to_map(pts):
        return [
            projected_to_map_coord(
                x,
                y,
                ref_x=ref_x,
                ref_y=ref_y,
                scale=scale,
                grivation_deg=grivation_deg,
            )
            for x, y in pts
        ]

    for feat in feats:
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
            x, y = float(coords[0]), float(coords[1])
            if clip_bounds and not point_inside(x, y, clip_bounds):
                continue
            mx, my = to_map([(x, y)])[0]
            obj = _point_object(symbol_index, mx, my)
            if obj:
                grouped[kind].append(obj)
                kind_codes[kind] = code
        elif gtype == "LineString":
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue
            line = [(float(x), float(y)) for x, y in coords]
            dash_raw = props.get("dash_indices") or []
            dash_pts: list[tuple[float, float]] = []
            if isinstance(dash_raw, list):
                for idx in dash_raw:
                    try:
                        i = int(idx)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= i < len(line):
                        dash_pts.append(line[i])
            match_m2 = POWER_SUPPORT_MATCH_M * POWER_SUPPORT_MATCH_M
            for piece in clip_polyline(line, clip_bounds) if clip_bounds else [line]:
                mapped: list[tuple[int, int] | tuple[int, int, int]] = []
                for x, y in piece:
                    mx, my = projected_to_map_coord(
                        x,
                        y,
                        ref_x=ref_x,
                        ref_y=ref_y,
                        scale=scale,
                        grivation_deg=grivation_deg,
                    )
                    flags = 0
                    for dx, dy in dash_pts:
                        ddx = x - dx
                        ddy = y - dy
                        if ddx * ddx + ddy * ddy <= match_m2:
                            flags = MAP_COORD_DASH_POINT
                            break
                    if flags:
                        mapped.append((mx, my, flags))
                    else:
                        mapped.append((mx, my))
                obj = _path_object(symbol_index, mapped)
                if obj:
                    grouped[kind].append(obj)
                    kind_codes[kind] = code
        elif gtype == "Polygon":
            raw_rings = geom.get("coordinates") or []
            if not raw_rings:
                continue
            rings = [[(float(x), float(y)) for x, y in r] for r in raw_rings]
            if clip_bounds:
                rings = [clip_ring(r, clip_bounds) for r in rings]
                if len(rings[0]) < 3:
                    continue
                rings = [rings[0]] + [r for r in rings[1:] if len(r) >= 3]
            obj = _area_object_with_holes(symbol_index, [to_map(r) for r in rings])
            if obj:
                grouped[kind].append(obj)
                kind_codes[kind] = code
            if (
                olive_index is not None
                and kind in _OSM_BUILDING_KINDS
                and len(rings) > 1
            ):
                outer = rings[0]
                holes = rings[1:]
                hole_parts: list = [("line", outer, True)]
                for h in holes:
                    hole_parts.append(("hole", h))
                courtyard_objects.extend(
                    _hole_rings_as_area_objects(
                        hole_parts,
                        olive_index,
                        ref_x=ref_x,
                        ref_y=ref_y,
                        scale=scale,
                        grivation_deg=grivation_deg,
                        clip_bounds=None,
                    )
                )
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
    if courtyard_objects:
        parts.append(
            OomObjectPart(
                name="OSM – dvory (oliva)",
                objects_xml="\n".join(courtyard_objects),
                count=len(courtyard_objects),
            )
        )
    return parts
