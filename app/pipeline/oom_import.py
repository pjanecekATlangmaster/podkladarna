from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.pipeline.karttapullautin_dxf import collect_dxf_for_zip
from app.pipeline.oom_symbol_map import (
    oom_code_for_dxf,
    oom_code_for_vectorconf_rule,
    symbol_index_for_code,
)
from app.pipeline.oom_vectorconf import load_vectorconf, match_feature


@dataclass
class OomObjectPart:
    name: str
    objects_xml: str
    count: int


def projected_to_map_coord(
    x: float,
    y: float,
    *,
    ref_x: float,
    ref_y: float,
    scale: int,
) -> tuple[int, int]:
    """Nativní souřadnice OOM (0,001 mm na papíře)."""
    fac = 1_000_000.0 / float(scale)
    return round((x - ref_x) * fac), round((y - ref_y) * fac)


def _fmt(x: int, y: int, flags: int = 0) -> str:
    if flags:
        return f"{x} {y} {flags}"
    return f"{x} {y}"


def _path_object(
    symbol_index: int,
    coords: list[tuple[int, int]],
    *,
    close: bool = False,
) -> str:
    if len(coords) < 2:
        return ""
    pts = [_fmt(x, y) for x, y in coords]
    if close:
        x0, y0 = coords[0]
        pts.append(_fmt(x0, y0, 18))
    body = ";".join(pts) + ";"
    return (
        f'            <object type="1" symbol="{symbol_index}">\n'
        f'                <coords count="{len(pts)}">{body}</coords>\n'
        f'                <pattern rotation="0"><coord x="0" y="0"/></pattern>\n'
        f"            </object>"
    )


def _point_object(symbol_index: int, x: int, y: int) -> str:
    return (
        f'            <object type="0" symbol="{symbol_index}">\n'
        f'                <coords count="1">{_fmt(x, y)};</coords>\n'
        f"            </object>"
    )


def _geom_objects(
    geom,
    symbol_index: int,
    *,
    ref_x: float,
    ref_y: float,
    scale: int,
    ogr,
) -> list[str]:
    from osgeo import ogr as ogr_mod

    gtype = geom.GetGeometryType()
    out: list[str] = []

    def line_coords(line) -> list[tuple[int, int]]:
        return [
            projected_to_map_coord(
                line.GetX(i), line.GetY(i), ref_x=ref_x, ref_y=ref_y, scale=scale
            )
            for i in range(line.GetPointCount())
        ]

    if gtype in (ogr_mod.wkbPoint, ogr_mod.wkbPoint25D):
        mx, my = projected_to_map_coord(
            geom.GetX(), geom.GetY(), ref_x=ref_x, ref_y=ref_y, scale=scale
        )
        obj = _point_object(symbol_index, mx, my)
        if obj:
            out.append(obj)
    elif gtype in (ogr_mod.wkbLineString, ogr_mod.wkbLineString25D):
        obj = _path_object(symbol_index, line_coords(geom))
        if obj:
            out.append(obj)
    elif gtype in (ogr_mod.wkbMultiLineString, ogr_mod.wkbMultiLineString25D):
        for i in range(geom.GetGeometryCount()):
            sub = geom.GetGeometryRef(i)
            if sub:
                obj = _path_object(symbol_index, line_coords(sub))
                if obj:
                    out.append(obj)
    elif gtype in (ogr_mod.wkbPolygon, ogr_mod.wkbPolygon25D):
        ring = geom.GetGeometryRef(0)
        if ring:
            obj = _path_object(symbol_index, line_coords(ring), close=True)
            if obj:
                out.append(obj)
    elif gtype in (ogr_mod.wkbMultiPolygon, ogr_mod.wkbMultiPolygon25D):
        for i in range(geom.GetGeometryCount()):
            poly = geom.GetGeometryRef(i)
            if not poly:
                continue
            ring = poly.GetGeometryRef(0)
            if ring:
                obj = _path_object(symbol_index, line_coords(ring), close=True)
                if obj:
                    out.append(obj)
    return out


def _extract_shp_from_zip(zabaged_clean: Path, shp_name: str, dest_dir: Path) -> Path | None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(shp_name).stem
    with zipfile.ZipFile(zabaged_clean) as zf:
        for info in zf.infolist():
            if Path(info.filename).name.startswith(stem + "."):
                out = dest_dir / Path(info.filename).name
                out.write_bytes(zf.read(info))
    shp = dest_dir / shp_name
    return shp if shp.is_file() else None


def feature_props(feature, *, layer_name: str) -> dict[str, object]:
    props: dict[str, object] = {}
    for i in range(feature.GetFieldCount()):
        name = feature.GetFieldDefnRef(i).GetName()
        if not name:
            continue
        props[name] = feature.GetField(i)
    if "vrstva" not in props:
        props["vrstva"] = layer_name
    return props


def build_zabaged_object_parts(
    zabaged_clean: Path,
    *,
    vectorconf_name: str,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    work_dir: Path,
) -> list[OomObjectPart]:
    try:
        from osgeo import ogr
    except ImportError:
        return []

    rules = load_vectorconf(vectorconf_name)
    stage = work_dir / "_oom_objects"
    if stage.exists():
        import shutil

        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    parts: list[OomObjectPart] = []
    with zipfile.ZipFile(zabaged_clean) as zf:
        shp_names = sorted(
            Path(n).name for n in zf.namelist() if n.lower().endswith(".shp")
        )

    for shp_name in shp_names:
        layer_name = Path(shp_name).stem
        shp_path = _extract_shp_from_zip(zabaged_clean, shp_name, stage / layer_name)
        if not shp_path:
            continue
        ds = ogr.Open(str(shp_path))
        if not ds:
            continue
        layer = ds.GetLayer()
        if not layer:
            continue
        objects: list[str] = []
        for feature in layer:
            props = feature_props(feature, layer_name=layer_name)
            rule = match_feature(props, rules)
            if not rule:
                continue
            code = oom_code_for_vectorconf_rule(
                rule.symbol_name,
                rule.kp_code,
                layer_name,
                preset_id=preset_id,
                scale=scale,
            )
            if not code:
                continue
            symbol_index = symbol_index_for_code(preset_id, scale, code)
            if symbol_index is None:
                continue
            geom = feature.GetGeometryRef()
            if geom is None:
                continue
            objects.extend(
                _geom_objects(
                    geom,
                    symbol_index,
                    ref_x=ref_x,
                    ref_y=ref_y,
                    scale=scale,
                    ogr=ogr,
                )
            )
        if objects:
            parts.append(
                OomObjectPart(
                    name=f"ZABAGED – {layer_name}",
                    objects_xml="\n".join(objects),
                    count=len(objects),
                )
            )
    return parts


def build_dxf_object_part(
    kp_cwd: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
) -> OomObjectPart | None:
    try:
        from osgeo import ogr
    except ImportError:
        return None

    temp = kp_cwd / "temp"
    if not temp.is_dir():
        return None
    dxf_map = collect_dxf_for_zip(temp)
    if not dxf_map:
        return None

    objects: list[str] = []
    for zip_name, path in sorted(dxf_map.items()):
        code = oom_code_for_dxf(zip_name)
        if not code:
            continue
        symbol_index = symbol_index_for_code(preset_id, scale, code)
        if symbol_index is None:
            continue
        ds = ogr.Open(str(path))
        if not ds:
            continue
        for i in range(ds.GetLayerCount()):
            layer = ds.GetLayerByIndex(i)
            if not layer:
                continue
            for feature in layer:
                geom = feature.GetGeometryRef()
                if geom is None:
                    continue
                objects.extend(
                    _geom_objects(
                        geom,
                        symbol_index,
                        ref_x=ref_x,
                        ref_y=ref_y,
                        scale=scale,
                        ogr=ogr,
                    )
                )
    if not objects:
        return None
    return OomObjectPart(
        name="Karttapullautin – vektory",
        objects_xml="\n".join(objects),
        count=len(objects),
    )
