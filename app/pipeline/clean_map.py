"""Čistá verze mapy les a MTBO (Petr 2026-11-21): jen to „jisté“, pro mapaře, který si zbytek domaluje sám.

Mapa se skládá jednou (plná kresba); před zápisem se z hotových objektů odvodí druhá, čistá verze:

* zeleň 406 / 408 / 410 z výšek (CHM, hustota odrazů, zrnitost porostu) se vynechá celá,
* otevřený terén 401 a drsný otevřený terén 403 z výšek / ortofota zůstane jen jako souvislé plochy
  (401 a 403 dohromady, i přes řezy objektů podle počtu vrcholů) od ``CLEAN_OPEN_MIN_M2`` – jasné velké
  paseky a louky; drobné louky a ploška náletu zmizí,
* (``CLEAN_DROP_DEM``) srázy, skály, balvany, kupky a prohlubně odvozené z DEM se vynechají.

Data (OSM, ZABAGED, DTM, RÚIAN, AOPK) a vrstevnice zůstávají, i když mají symbol porostu (OSM křoví, sad,
vinice, louky a pole ze ZABAGED). Čistá mapa nese hlavní jméno souboru, plná má příponu ``_full``. Sprint
beze změny.

Práh 0,3 ha (měřeno 2026-11-21 na mapách z master): Janov Okrouhlice / Svobodka zůstane 140 z ~2 000
ploch 401 ∪ 403 a 94 % jejich výměry, Praha 8 z 202 (96 %); zbytek jsou drobky. 0,2 ha nechá o třetinu víc
drobných ploch při +1–2 % výměry, 0,5 ha už ubírá skutečné paseky (−2 % výměry, polovina ploch).
"""

from __future__ import annotations

import re
from pathlib import Path

from app.pipeline.oom_import import OomObjectPart

# Hlavní vypínač: les a MTBO zapíšou čistou (hlavní jméno) i plnou mapu (``_full``).
CLEAN_MAPS = True
FULL_TAG = "_full"
# Nejmenší souvislá plocha otevřeného terénu z výšek (401 ∪ 403), která v čisté mapě zůstane.
CLEAN_OPEN_MIN_M2 = 3000.0
# Vynechat i srázy, skály, balvany, kupky a prohlubně z DEM (část „… – vektory“ z ``build_dxf_object_part``).
CLEAN_DROP_DEM = True

# Jména částí z ``vegetation_gdal.build_vegetation_parts`` (vegetation.shp z výšek / ortofota).
GREEN_PART_NAMES = frozenset({"Vegetace pomalý běh", "Vegetace chůze", "Vegetace boj"})
OPEN_PART_NAMES = frozenset({"Otevřený terén", "Drsný otevřený terén"})
# Část z ``build_dxf_object_part`` (srázy, skály, balvany, kupky a prohlubně z DEM).
DEM_PART_SUFFIX = " – vektory ("
DEM_PART_PREFIXES = ("DEM kandidáti", "legacy raster")

_OBJECT_RE = re.compile(r"<object\b.*?</object>", re.S)
_COORDS_RE = re.compile(r"<coords[^>]*>([^<]*)</coords>")
# Příznaky souřadnice v .omap: 2 = konec uzavřeného prstence, 16 = konec díry.
_RING_END = 2 | 16
# Sousední kusy téže plochy (řez podle počtu vrcholů, 401 vedle 403) se spojí do jednoho celku.
_TOUCH_M = 0.05


def full_variant_name(name: str) -> str:
    """``Mapa-les.omap`` → ``Mapa-les_full.omap``."""
    p = Path(name)
    return f"{p.stem}{FULL_TAG}{p.suffix}"


def strip_full_tag(stem: str) -> str:
    return stem[: -len(FULL_TAG)] if stem.endswith(FULL_TAG) else stem


def is_dem_part(name: str) -> bool:
    return DEM_PART_SUFFIX in name and name.startswith(DEM_PART_PREFIXES)


def _object_polygon(obj_xml: str, um_to_m: float):
    from shapely.geometry import Polygon

    m = _COORDS_RE.search(obj_xml)
    if not m:
        return None
    rings: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    for tok in m.group(1).strip().strip(";").split(";"):
        a = tok.split()
        if len(a) < 2:
            continue
        cur.append((int(a[0]) * um_to_m, int(a[1]) * um_to_m))
        if len(a) > 2 and int(a[2]) & _RING_END:
            rings.append(cur)
            cur = []
    if cur:
        rings.append(cur)
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return None
    poly = Polygon(rings[0], rings[1:])
    if not poly.is_valid:
        poly = poly.buffer(0)
    return None if poly.is_empty else poly


def _big_open_objects(parts: list[OomObjectPart], scale: int, min_m2: float) -> dict[int, list[str]]:
    """Index části → objekty 401 / 403 ze souvislých ploch ≥ ``min_m2``."""
    from shapely import STRtree
    from shapely.ops import unary_union

    um_to_m = float(scale) / 1e6
    items: list[tuple[int, str, object]] = []
    for i, part in enumerate(parts):
        if part.name not in OPEN_PART_NAMES:
            continue
        for obj in _OBJECT_RE.findall(part.objects_xml):
            items.append((i, obj, _object_polygon(obj, um_to_m)))
    kept: dict[int, list[str]] = {i: [] for i, p in enumerate(parts) if p.name in OPEN_PART_NAMES}
    polys = [g for _, _, g in items if g is not None]
    if not polys:
        return kept
    merged = unary_union([g.buffer(_TOUCH_M) for g in polys])
    comps = [c for c in getattr(merged, "geoms", [merged]) if c.area >= min_m2]
    if not comps:
        return kept
    tree = STRtree(comps)
    for i, obj, geom in items:
        if geom is None:
            continue
        pt = geom.representative_point()
        if any(comps[j].contains(pt) for j in tree.query(pt)):
            kept[i].append(obj)
    return kept


def clean_object_parts(
    parts: list[OomObjectPart],
    *,
    scale: int,
    min_open_m2: float = CLEAN_OPEN_MIN_M2,
    drop_dem: bool = CLEAN_DROP_DEM,
    stats: dict | None = None,
) -> list[OomObjectPart]:
    """Části čisté mapy z hotových částí plné mapy (vstup se nemění)."""
    kept_open = _big_open_objects(parts, scale, min_open_m2)
    out: list[OomObjectPart] = []
    st = {"green": 0, "open_dropped": 0, "open_kept": 0, "dem": 0}
    for i, part in enumerate(parts):
        if part.name in GREEN_PART_NAMES:
            st["green"] += part.count
            continue
        if drop_dem and is_dem_part(part.name):
            st["dem"] += part.count
            continue
        if i in kept_open:
            objs = kept_open[i]
            st["open_kept"] += len(objs)
            st["open_dropped"] += max(0, len(_OBJECT_RE.findall(part.objects_xml)) - len(objs))
            if objs:
                out.append(OomObjectPart(name=part.name, objects_xml="\n".join(objs), count=len(objs)))
            continue
        out.append(part)
    if stats is not None:
        stats.update(st)
    return out


def log_line(stats: dict, min_open_m2: float = CLEAN_OPEN_MIN_M2) -> str:
    ha = f"{min_open_m2 / 1e4:g}".replace(".", ",")
    return (
        f"OOM: čistá verze – vynecháno {stats.get('green', 0)} ploch zeleně z výšek, "
        f"{stats.get('open_dropped', 0)} ploch otevřeného terénu pod {ha} ha "
        f"(zůstává {stats.get('open_kept', 0)}), {stats.get('dem', 0)} objektů z DEM"
    )
