"""Vegetace ``work/vegetation/vegetation.shp`` (hustota / CHM) → objekty .omap."""

from __future__ import annotations

import os
from pathlib import Path

from app.pipeline.oom_import import (
    OomObjectPart,
    _geom_parts_to_objects,
    _pyogrio_layer_rows,
    _wkb_parts,
)
from app.pipeline.oom_symbol_map import symbol_index_for_code
from app.proj_env import ensure_proj_data

# Třída v rastru vegetace: 0 bílý les, 1 open land, 2–4 zeleně.
_CLASS_TO_CODE: dict[int, str] = {
    1: "401",
    2: "406",
    3: "408",
    4: "410",
    5: "403",
}

# Max. vrcholů a plochy na jeden plošný objekt – větší louka/porost se rozřízne,
# přednostně v úzkých místech (area_split). OCAD přepočítává objekt bod po bodu
# a s loukou přes půl mapy se špatně pracuje; výplně vegetace nemají obrys,
# řez není vidět. 4 ha ≈ 200 × 200 m (2 × 2 cm v 1 : 10 000).
VEG_MAX_AREA_VERTICES = 1000
VEG_MAX_AREA_M2 = 40_000.0
# Vyhlazení tvarů z rastru (schodky po pixelech) + převod na Bézierovy křivky
# jako „Převést na křivky“ v Mapperu. Les a MTBO: všechny porosty bez posunu
# hranice (veg_smooth.smooth_area, stupně SMOOTH_LEVELS). vegetation.shp nese
# přesný obrys z rastru, stupeň 0 jsou kostičky 1:1.
VEG_AS_CURVES = True
# Les a MTBO: obrys porostu se před převodem na XML zjednoduší jako Ctrl+M v Mapperu (0,1 mm mapy,
# oom_simplify). Záložní vypínač; sprint beze změny.
VEG_SIMPLIFY = True
# Sprint beze změny (do 2.46): DP 1,5 m pro všechny porosty, zaoblení
# (veg_smooth.smooth_area_legacy) jen louky (401) a hustý porost (410).
VEG_SMOOTH_CODES = frozenset({"401", "410"})
SPRINT_SIMPLIFY_M = 1.5
# Stupeň z GUI → původní stupeň sprintu: výchozí 1 kreslí sprint jako dřív výchozí 2.
_SPRINT_LEVEL = {0: 0, 1: 2, 2: 2, 3: 3}
# Otevřený terén (401) se podle plochy neřeže (velká louka s jednoduchým obrysem
# je v OCADu v pohodě, řezy jsou vidět); dělí se jen kvůli počtu vrcholů.
_NO_AREA_SPLIT_CODES = frozenset({"401", "403"})

_CLASS_NAMES: dict[str, str] = {
    "401": "Otevřený terén",
    "403": "Drsný otevřený terén",
    "406": "Vegetace pomalý běh",
    "408": "Vegetace chůze",
    "410": "Vegetace boj",
}


def vege_class_to_oom_code(cls: int) -> str | None:
    return _CLASS_TO_CODE.get(cls)


def _iter_vege_rows(shp: Path):
    """Čte SHP bez načítání .prj (PROJ identify v Dockeru padá)."""
    ensure_proj_data()
    try:
        from osgeo import ogr
    except ImportError:
        ogr = None
    if ogr is not None:
        # .prj se nečte (PROJ identify padá): čte se kopie souborů bez něj v dočasné složce, originál
        # se nemění (les a MTBO se mohou sestavovat souběžně).
        import shutil
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="vege_noprj_"))
        try:
            for suffix in (".shp", ".shx", ".dbf", ".cpg"):
                src = shp.with_suffix(suffix)
                if not src.is_file():
                    continue
                try:
                    os.link(src, tmp / src.name)
                except OSError:
                    shutil.copy2(src, tmp / src.name)
            ds = ogr.Open(str(tmp / shp.name))
            if ds:
                layer = ds.GetLayer(0)
                if layer is not None:
                    for feature in layer:
                        geom = feature.GetGeometryRef()
                        if geom is None:
                            continue
                        props: dict[str, object] = {}
                        for i in range(feature.GetFieldCount()):
                            defn = feature.GetFieldDefnRef(i)
                            if defn:
                                props[defn.GetName()] = feature.GetField(i)
                        yield props, bytes(geom.ExportToWkb())
                    return
        finally:
            ds = None
            shutil.rmtree(tmp, ignore_errors=True)
    try:
        import pyogrio
    except ImportError:
        return
    for layer_name, _t in pyogrio.list_layers(shp):
        yield from _pyogrio_layer_rows(shp, layer=layer_name)


def _shaped_parts(wkb: bytes, code: str, smooth_level: int, barriers, sprint: bool):
    """Obrys porostu podle stupně → (díly pro ``_geom_parts_to_objects``, křivky?)."""
    from shapely import from_wkb

    from app.pipeline.area_split import polygons_of
    from app.pipeline.veg_smooth import smooth_area, smooth_area_legacy

    try:
        polys = polygons_of(from_wkb(bytes(wkb)))
    except Exception:
        return _wkb_parts(wkb)[0], False
    areas: list[tuple[list, list]] = []
    curves = False
    for poly in polys:
        if sprint:
            poly = poly.simplify(SPRINT_SIMPLIFY_M, preserve_topology=True)
            if poly.is_empty:
                continue
        ring = list(poly.exterior.coords)
        holes = [list(i.coords) for i in poly.interiors]
        if sprint and smooth_level and code in VEG_SMOOTH_CODES:
            areas.extend(smooth_area_legacy(ring, holes, smooth_level, barriers))
            curves = VEG_AS_CURVES
        elif not sprint and smooth_level:
            areas.extend(
                smooth_area(ring, holes, smooth_level, barriers, as_curves=VEG_AS_CURVES)
            )
            curves = VEG_AS_CURVES
        else:
            areas.append((ring, holes))
    parts: list = []
    for ring, holes in areas:
        parts.append(("line", ring, True))
        parts.extend(("hole", h) for h in holes)
    return parts, curves


# Vyhlazené obrysy porostů po řádcích ``vegetation.shp``: les a MTBO jednoho jobu skládají mapu ze stejného
# souboru se stejným stupněm (``smooth_area`` je ~20 % skládání lesa), MTBO je tak vezme hotové. Klíč jsou
# cesty a časy / velikosti vstupů (shp, dbf, překážky z ``osm_paths``) + stupeň; drží se jen poslední výřez.
_SHAPED_MEMO: dict[tuple, dict[int, tuple[list, bool]]] = {}


def _simplified_geom_parts(geom_parts: list, scale: int) -> list:
    """Zjednoduší každý prstenec i díru (tolerance v metrech v terénu podle měřítka)."""
    from app.pipeline.oom_simplify import SIMPLIFY_TOLERANCE_MM, simplify_ring

    tol = SIMPLIFY_TOLERANCE_MM * scale / 1000.0
    out: list = []
    for part in geom_parts:
        if part[0] == "line":
            out.append(("line", simplify_ring(part[1], tol, closed=True), *part[2:]))
        elif part[0] == "hole":
            out.append(("hole", simplify_ring(part[1], tol, closed=True), *part[2:]))
        else:
            out.append(part)
    return out


# Kolej OSM (mimo Česko) jako veto porostu z CHM: půlka šířky pásu v metrech (trolej, vlak, štěrkové lože).
RAIL_VETO_HALF_M = 1.5
# Veto platí jen pro zeleň (falešný strom); otevřený terén 401 z nízkého CHM nevadí a pod čarou zmizí.
VETO_CODES = frozenset({"406", "408", "410"})
# Menší budova (kůlna v lese, les a MTBO ji kreslí bodem) porost nevyřízne – nevznikne díra v zeleni.
BUILDING_VETO_MIN_M2 = 25.0


def _raster_veto_mask(work_dir: Path, shp: Path, sprint: bool):
    """Plochy, na kterých se porost z rastru CHM nekreslí (None = nic neořezávat).

    DSM − DTM bez klasifikovaných bodů: mostovka nad dopočteným terénem, střecha, vlak nebo trolej vychází
    jako porost (Gent, Sint-Michielsbrug: 410 přes celý most). Mimo Česko proto mají přednost OSM a technická
    mapa (Petr 2026-11-15): mosty, budovy, vodní plochy, koleje a ve sprintu i vozovky (v lese a MTBO by pás
    vozovky širší než čára silnice nechal bílý lem v lese). Jen zeleň (``VETO_CODES``), jen u porostu z rastru
    (značka ``CHM_SOURCE_MARK``) a jen mimo Česko – český build (hustota bodů i záložní CHM) zůstává beze změny.
    """
    from app.pipeline.country_profile import process_uses_cz_sources
    from app.pipeline.vegetation_chm import CHM_SOURCE_MARK

    if process_uses_cz_sources() or not (shp.parent / CHM_SOURCE_MARK).is_file():
        return None
    import shapely

    # Díly bez globálního sjednocení ve STRtree: řádek porostu odečte jen díly, které protíná (čas na řádek
    # nezávisí na ploše výřezu; rozdíl proti celé masce rostl kvadraticky).
    parts = []
    for layer in _veto_parts(Path(work_dir), roads=sprint):
        if layer is None or layer.is_empty:
            continue
        parts.extend(g for g in getattr(layer, "geoms", [layer]) if not g.is_empty)
    if not parts:
        return None
    return parts, shapely.STRtree(parts)


def _veto_parts(work_dir: Path, *, roads: bool = True) -> list:
    """Vrstvy veta jako syrové díly (bez sjednocení, to by z uliční sítě udělalo jeden obří polygon a ořez by
    rostl kvadraticky); vrstva, která se nenačte, se vynechá (zbytek platí)."""
    import json

    from shapely.geometry import LineString, Polygon, shape

    from app.pipeline import dtm_import, osm_paths

    out: list = []

    def add(fn):
        try:
            got = fn()
        except Exception:
            return
        if got is not None:
            out.extend(got if isinstance(got, list) else [got])

    def polys_from_rings(rings_list) -> list:
        res = []
        for rings in rings_list:
            if rings and len(rings[0]) >= 4:
                poly = Polygon(rings[0], rings[1:])
                res.append(poly if poly.is_valid else poly.buffer(0))
        return res

    def read_feats(name: str) -> list:
        gj = work_dir / "osm_paths" / name
        return json.loads(gj.read_text(encoding="utf-8")).get("features") or [] if gj.is_file() else []

    add(lambda: osm_paths.bridge_deck_geometry(work_dir))  # mostů je málo

    def osm_areas():
        res = []
        for f in read_feats("features.geojson"):
            kind = (f.get("properties") or {}).get("kind")
            geom = f.get("geometry") or {}
            if kind in {"building", "water_body"} and geom.get("type") in ("Polygon", "MultiPolygon"):
                g = shape(geom)
                g = g if g.is_valid else g.buffer(0)
                if kind == "water_body" or g.area >= BUILDING_VETO_MIN_M2:
                    res.append(g)
            elif kind in {"rail_line", "tram_line"} and geom.get("type") == "LineString":
                coords = geom.get("coordinates") or []
                if len(coords) >= 2:
                    res.append(LineString(coords).buffer(RAIL_VETO_HALF_M, cap_style="flat"))
        return res

    add(osm_areas)
    # Budovy technické mapy (DTM, GRB, BGT, …); překryv s OSM nevadí, sjednocují se jen díly u řádku.
    add(lambda: [g for g in polys_from_rings(dtm_import._load_polygons_file(dtm_import.buildings_path(work_dir)))
                 if g.area >= BUILDING_VETO_MIN_M2])

    if roads:
        add(lambda: polys_from_rings(dtm_import._load_road_polygons(work_dir)))  # vozovky technické mapy

        def osm_roads():
            res = []
            for f in read_feats("paths_osm.geojson"):
                hw = str((f.get("properties") or {}).get("highway") or "")
                coords = (f.get("geometry") or {}).get("coordinates") or []
                # nad tunelem porost být může
                if osm_paths.is_tunnel_highway(hw) or not osm_paths.path_draw_highway(hw).startswith("road_"):
                    continue
                if (f.get("geometry") or {}).get("type") == "LineString" and len(coords) >= 2:
                    half = max(1.0, osm_paths.drawn_width_m(hw) / 2.0)  # jako ``road_carriageway_geometry``
                    res.append(LineString(coords).buffer(half, cap_style=2, join_style=2))
            return res

        add(osm_roads)
    return out


def _clip_wkb(wkb: bytes, mask) -> bytes | None:
    """Porost bez ploch veta (``_raster_veto_mask``); None = nic nezbylo."""
    from shapely import from_wkb, to_wkb, union_all

    parts, tree = mask
    try:
        geom = from_wkb(bytes(wkb))
        idx = tree.query(geom, predicate="intersects")
        if len(idx) == 0:
            return wkb
        rest = geom.difference(union_all([parts[i] for i in idx]))
    except Exception:
        return wkb
    if rest.is_empty or rest.area < 1.0:
        return None
    return bytes(to_wkb(rest))


def _file_stamp(path: Path) -> tuple:
    try:
        st = path.stat()
    except OSError:
        return (str(path), None)
    return (str(path.resolve()), st.st_mtime_ns, st.st_size)


def _shaped_memo(work_dir: Path, shp: Path, smooth_level: int, sprint: bool) -> dict[int, tuple[list, bool]]:
    from app.pipeline.vegetation_chm import CHM_SOURCE_MARK

    osm_dir = Path(work_dir) / "osm_paths"
    key = (
        _file_stamp(shp),
        _file_stamp(shp.with_suffix(".dbf")),
        _file_stamp(osm_dir / "paths_mixed.geojson"),
        _file_stamp(osm_dir / "features.geojson"),
        _file_stamp(osm_dir / "paths_osm.geojson"),
        _file_stamp(osm_dir / "bridge_decks.geojson"),
        _file_stamp(Path(work_dir) / "dtm" / "roads.json"),
        _file_stamp(Path(work_dir) / "dtm" / "buildings.json"),
        _file_stamp(shp.parent / CHM_SOURCE_MARK),
        smooth_level,
        sprint,
        VEG_AS_CURVES,
    )
    rows = _SHAPED_MEMO.get(key)
    if rows is None:
        _SHAPED_MEMO.clear()
        rows = _SHAPED_MEMO[key] = {}
    return rows


def build_vegetation_parts(
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    smooth_level: int = 0,
) -> list[OomObjectPart]:
    """``smooth_level``: 0 = kostičky z rastru, 1–3 = stupeň vyhlazení (viz veg_smooth)."""
    shp = work_dir / "vegetation" / "vegetation.shp"
    if not shp.is_file():
        return []

    from app.pipeline.veg_size_filter import is_sprint_preset

    sprint = is_sprint_preset(preset_id=preset_id, map_scale=scale)
    if sprint:
        smooth_level = _SPRINT_LEVEL.get(smooth_level, 2)
    shaped_rows = _shaped_memo(work_dir, shp, smooth_level, sprint)
    barriers = None  # načtou se až u prvního řádku, který memo ještě nemá
    veto_mask: object = False  # False = ještě nenačteno; None = bez ořezu

    grouped: dict[str, list[str]] = {code: [] for code in _CLASS_TO_CODE.values()}
    for row, (props, wkb) in enumerate(_iter_vege_rows(shp)):
        code = str(props.get("code") or "")
        if code not in grouped:
            cls = props.get("cls")
            try:
                mapped = vege_class_to_oom_code(int(cls)) if cls is not None else None
            except (TypeError, ValueError):
                mapped = None
            code = mapped or ""
        if code not in grouped:
            continue
        symbol_index = symbol_index_for_code(preset_id, scale, code)
        if symbol_index is None:
            continue
        hit = shaped_rows.get(row)
        if hit is None:
            if smooth_level and barriers is None:
                from app.pipeline.veg_smooth import load_smooth_barriers

                barriers = load_smooth_barriers(work_dir)
            if veto_mask is False:
                veto_mask = _raster_veto_mask(work_dir, shp, sprint)
            if veto_mask is not None and code in VETO_CODES:
                wkb = _clip_wkb(wkb, veto_mask)
            hit = shaped_rows[row] = (
                ([], False) if wkb is None else _shaped_parts(wkb, code, smooth_level, barriers, sprint)
            )
        geom_parts, curves = hit
        if VEG_SIMPLIFY and curves and not sprint:
            # Až po memo (les a MTBO mají jiné měřítko, memo drží neupravený tvar).
            geom_parts = _simplified_geom_parts(geom_parts, scale)
        grouped[code].extend(
            _geom_parts_to_objects(
                geom_parts,
                symbol_index,
                ref_x=ref_x,
                ref_y=ref_y,
                scale=scale,
                grivation_deg=grivation_deg,
                as_area=True,
                max_area_vertices=VEG_MAX_AREA_VERTICES,
                max_area_m2=None if code in _NO_AREA_SPLIT_CODES else VEG_MAX_AREA_M2,
                area_as_curves=curves,
            )
        )

    parts: list[OomObjectPart] = []
    for code in ("401", "403", "406", "408", "410"):
        objects = grouped[code]
        if objects:
            parts.append(
                OomObjectPart(
                    name=_CLASS_NAMES[code],
                    objects_xml="\n".join(objects),
                    count=len(objects),
                )
            )
    return parts
