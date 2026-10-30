"""Vegetace ``work/vegetation/vegetation.shp`` (hustota / CHM) → objekty .omap."""

from __future__ import annotations

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
        prj = shp.with_suffix(".prj")
        prj_aside: Path | None = None
        if prj.is_file():
            prj_aside = prj.with_suffix(".prj.aside")
            prj_aside.unlink(missing_ok=True)
            prj.rename(prj_aside)
        try:
            ds = ogr.Open(str(shp))
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
            if prj_aside is not None and prj_aside.is_file():
                prj.unlink(missing_ok=True)
                prj_aside.rename(prj)
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


def _file_stamp(path: Path) -> tuple:
    try:
        st = path.stat()
    except OSError:
        return (str(path), None)
    return (str(path.resolve()), st.st_mtime_ns, st.st_size)


def _shaped_memo(work_dir: Path, shp: Path, smooth_level: int, sprint: bool) -> dict[int, tuple[list, bool]]:
    osm_dir = Path(work_dir) / "osm_paths"
    key = (
        _file_stamp(shp),
        _file_stamp(shp.with_suffix(".dbf")),
        _file_stamp(osm_dir / "paths_mixed.geojson"),
        _file_stamp(osm_dir / "features.geojson"),
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
            hit = shaped_rows[row] = _shaped_parts(wkb, code, smooth_level, barriers, sprint)
        geom_parts, curves = hit
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
