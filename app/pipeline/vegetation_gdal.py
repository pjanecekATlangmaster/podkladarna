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
}

# Max. vrcholů na jeden plošný objekt – větší louka/porost se rozřízne
# (OCAD přepočítává objekt bod po bodu; výplně vegetace nemají obrys, řez není vidět).
VEG_MAX_AREA_VERTICES = 1500

_CLASS_NAMES: dict[str, str] = {
    "401": "Otevřený terén",
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


def build_vegetation_parts(
    work_dir: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
) -> list[OomObjectPart]:
    shp = work_dir / "vegetation" / "vegetation.shp"
    if not shp.is_file():
        return []

    grouped: dict[str, list[str]] = {code: [] for code in _CLASS_TO_CODE.values()}
    for props, wkb in _iter_vege_rows(shp):
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
        geom_parts, _ = _wkb_parts(wkb)
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
            )
        )

    parts: list[OomObjectPart] = []
    for code in ("401", "406", "408", "410"):
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
