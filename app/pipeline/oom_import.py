from __future__ import annotations

import math
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.pipeline.cliff_height import filter_by_drop
from app.pipeline.geom_clip import Bounds, clip_polyline, clip_ring, point_inside
from app.pipeline.cliff_merge import (
    merge_cliff_ticks,
    min_line_length_m,
    polyline_to_strip_ring,
)
from app.pipeline.karttapullautin_dxf import collect_dxf_for_zip
from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_symbol_map import (
    KP_CLIFF_206_CODE,
    KP_CLIFF_DENSE_CODE,
    KP_CLIFF_EARTH_BANK,
    KP_CLIFF_OFF,
    KP_CLIFF_ROCK_FACE,
    KP_CLIFF_SYMBOL_206,
    oom_code_for_dxf,
    oom_code_for_vectorconf_rule,
    symbol_index_for_code,
)
from app.pipeline.oom_vectorconf import load_vectorconf, match_feature

# ('point', x, y) | ('line', [(x, y), ...], close)
_WkbPart = tuple[str, object]

_CLIFF_DXF_NAMES = frozenset({"cliffs_small.dxf", "cliffs_large.dxf"})
_TAG_SIDE_OFFSET_M = 2.0


def orient_polyline_tags_downhill(
    pts: list[tuple[float, float]],
    *,
    elev_at,
    to_map,
    offset_m: float = _TAG_SIDE_OFFSET_M,
) -> list[tuple[float, float]]:
    """Otočí lomenou čáru tak, aby tagy OOM (vlevo od směru) mířily ze svahu dolů.

    ``elev_at(x, y)`` → výška v metrech (projected), ``to_map(x, y)`` → map mm.
    """
    if len(pts) < 2 or elev_at is None or to_map is None:
        return pts
    a, b = pts[0], pts[-1]
    mx = (a[0] + b[0]) / 2.0
    my = (a[1] + b[1]) / 2.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return pts
    nx, ny = -dy / length, dx / length
    p_left = (mx + nx * offset_m, my + ny * offset_m)
    p_right = (mx - nx * offset_m, my - ny * offset_m)
    e_left = elev_at(*p_left)
    e_right = elev_at(*p_right)
    if e_left is None or e_right is None:
        return pts
    downhill = p_left if e_left <= e_right else p_right
    am = to_map(a[0], a[1])
    bm = to_map(b[0], b[1])
    dm = to_map(downhill[0], downhill[1])
    # Kladný cross = downhill je vlevo od am→bm v mapových souřadnicích.
    cross = (bm[0] - am[0]) * (dm[1] - am[1]) - (bm[1] - am[1]) * (dm[0] - am[0])
    if cross >= 0:
        return pts
    return list(reversed(pts))


class _DemElev:
    """Vzorkování výšky z GeoTIFF (GDAL)."""

    def __init__(self, path: Path):
        from osgeo import gdal

        self._ds = gdal.Open(str(path))
        if self._ds is None:
            raise RuntimeError(f"Nelze otevřít DEM: {path}")
        self._gt = self._ds.GetGeoTransform()
        self._band = self._ds.GetRasterBand(1)
        self._nodata = self._band.GetNoDataValue()
        self._w = self._ds.RasterXSize
        self._h = self._ds.RasterYSize
        # Měření výšky srázů dělá desetitisíce vzorků; po pixelech přes GDAL by
        # to bylo o řád pomalejší než držet celý rastr v paměti.
        self._grid = None
        # 16M pixelů = 64 MB ve float32, což pokryje 4×4 km po metru. Větší
        # rastr radši číst po pixelech než si sáhnout na paměť kontejneru.
        if self._w * self._h <= 16_000_000:
            try:
                self._grid = self._band.ReadAsArray().astype("float32")
            except Exception:
                self._grid = None

    def __call__(self, x: float, y: float) -> float | None:
        gt = self._gt
        # Affine inverse for north-up / south-up without rotation.
        det = gt[1] * gt[5] - gt[2] * gt[4]
        if abs(det) < 1e-18:
            return None
        px = (gt[5] * (x - gt[0]) - gt[2] * (y - gt[3])) / det
        py = (-gt[4] * (x - gt[0]) + gt[1] * (y - gt[3])) / det
        col, row = int(px), int(py)
        if col < 0 or row < 0 or col >= self._w or row >= self._h:
            return None
        if self._grid is not None:
            val = float(self._grid[row, col])
        else:
            val = float(self._band.ReadAsArray(col, row, 1, 1)[0][0])
        if self._nodata is not None and abs(val - float(self._nodata)) < 1e-6:
            return None
        if math.isnan(val):
            return None
        return val


_dem_cache: dict[Path, "_DemElev | None"] = {}
_dem_cache_dir: Path | None = None


def _first_dem(work_dir: Path, names: tuple[str, ...]):
    """Rastr se drží celý v paměti, tak stejný soubor otevírat jen jednou.

    Cache platí pro jeden job – při přechodu jinam se zahodí, jinak by ve worker
    procesu zůstaly viset desítky MB po každé zpracované mapě.
    """
    global _dem_cache_dir
    if _dem_cache_dir != work_dir:
        _dem_cache.clear()
        _dem_cache_dir = work_dir
    for name in names:
        dem = work_dir / "contours" / name
        if not dem.is_file():
            continue
        if dem not in _dem_cache:
            try:
                _dem_cache[dem] = _DemElev(dem)
            except Exception:
                _dem_cache[dem] = None
        if _dem_cache[dem] is not None:
            return _dem_cache[dem]
    return None


def _load_dem_elev(work_dir: Path):
    return _first_dem(work_dir, ("dem_smooth.tif", "dem_filled.tif"))


def _load_cliff_dem(work_dir: Path):
    """Na měření srázu je potřeba nehlazený model – smooth schod rozmázne."""
    return _first_dem(work_dir, ("dem_filled.tif", "dem_raw.tif", "dem_smooth.tif"))


def _wkb_read_points(buf: bytes, offset: int, fmt: str, n: int) -> tuple[list[tuple[float, float]], int]:
    pts: list[tuple[float, float]] = []
    for _ in range(n):
        x, y = struct.unpack_from(fmt + "dd", buf, offset)
        offset += 16
        pts.append((x, y))
    return pts, offset


def _wkb_parts(buf: bytes, offset: int = 0) -> tuple[list[_WkbPart], int]:
    byte_order = buf[offset]
    fmt = "<" if byte_order == 1 else ">"
    offset += 1
    geom_type, = struct.unpack_from(fmt + "I", buf, offset)
    offset += 4
    base_type = geom_type % 1000
    parts: list[_WkbPart] = []

    if base_type == 1:
        x, y = struct.unpack_from(fmt + "dd", buf, offset)
        parts.append(("point", x, y))
        offset += 16
    elif base_type == 2:
        n, = struct.unpack_from(fmt + "I", buf, offset)
        offset += 4
        pts, offset = _wkb_read_points(buf, offset, fmt, n)
        parts.append(("line", pts, False))
    elif base_type == 3:
        nr, = struct.unpack_from(fmt + "I", buf, offset)
        offset += 4
        for ri in range(nr):
            n, = struct.unpack_from(fmt + "I", buf, offset)
            offset += 4
            pts, offset = _wkb_read_points(buf, offset, fmt, n)
            # Vnitřní prstence jdou hned za obrysem – ZABAGED tak vyřezává
            # třeba les z louky a bez díry by plocha les přikryla.
            parts.append(("line", pts, True) if ri == 0 else ("hole", pts))
    elif base_type == 5:
        ng, = struct.unpack_from(fmt + "I", buf, offset)
        offset += 4
        for _ in range(ng):
            sub, offset = _wkb_parts(buf, offset)
            parts.extend(sub)
    elif base_type == 6:
        ng, = struct.unpack_from(fmt + "I", buf, offset)
        offset += 4
        for _ in range(ng):
            sub, offset = _wkb_parts(buf, offset)
            for part in sub:
                if part[0] == "hole" or (part[0] == "line" and part[2]):
                    parts.append(part)
    return parts, offset


def _geom_parts_to_objects(
    parts: list[_WkbPart],
    symbol_index: int,
    *,
    ref_x: float,
    ref_y: float,
    scale: int,
    grivation_deg: float,
    as_area: bool = False,
    elev_at=None,
    clip_bounds: Bounds | None = None,
) -> list[str]:
    def to_map(x: float, y: float) -> tuple[int, int]:
        return projected_to_map_coord(
            x,
            y,
            ref_x=ref_x,
            ref_y=ref_y,
            scale=scale,
            grivation_deg=grivation_deg,
        )

    out: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        index += 1
        if part[0] == "hole":
            # Díra bez obrysu před sebou nedává smysl – zahodit.
            continue
        if part[0] == "point":
            _, x, y = part
            if clip_bounds is not None and not point_inside(
                float(x), float(y), clip_bounds
            ):
                continue
            mx, my = to_map(float(x), float(y))
            out.append(_point_object(symbol_index, mx, my))
            continue
        if part[0] != "line":
            continue

        _, pts, close = part
        proj = [(float(x), float(y)) for x, y in pts]  # type: ignore[union-attr]
        closed_shape = as_area or close
        holes: list[list[tuple[float, float]]] = []
        while index < len(parts) and parts[index][0] == "hole":
            if closed_shape:
                holes.append(
                    [(float(x), float(y)) for x, y in parts[index][1]]  # type: ignore[misc]
                )
            index += 1

        if clip_bounds is None:
            pieces = [proj]
        elif closed_shape:
            ring = clip_ring(proj, clip_bounds)
            pieces = [ring] if len(ring) >= 3 else []
            holes = [
                clipped
                for clipped in (clip_ring(hole, clip_bounds) for hole in holes)
                if len(clipped) >= 3
            ]
        else:
            pieces = clip_polyline(proj, clip_bounds)

        for piece in pieces:
            if elev_at is not None and not close and len(piece) >= 2:
                piece = orient_polyline_tags_downhill(
                    piece, elev_at=elev_at, to_map=to_map
                )
            coords = [to_map(x, y) for x, y in piece]
            if closed_shape:
                rings = [coords] + [
                    [to_map(x, y) for x, y in hole] for hole in holes
                ]
                obj = _area_object_with_holes(symbol_index, rings)
            else:
                obj = _path_object(symbol_index, coords)
            if obj:
                out.append(obj)
    return out


def _pyogrio_layer_rows(path: Path | str, *, layer: str | None = None, force_2d: bool = True):
    """Iteruje (props, wkb) přes pyogrio.raw.read (API 0.13+: meta, fids, geoms, fields)."""
    import pyogrio.raw as pyogrio_raw

    meta, _fids, geoms, field_arrays = pyogrio_raw.read(
        str(path), layer=layer, force_2d=force_2d
    )
    if geoms is None:
        return
    raw_fields = meta.get("fields")
    field_names = [str(n) for n in raw_fields] if raw_fields is not None else []
    arrays = list(field_arrays) if field_arrays is not None else []
    n = len(geoms)
    for i in range(n):
        props = {
            name: arrays[j][i]
            for j, name in enumerate(field_names)
            if j < len(arrays)
        }
        yield props, geoms[i]


@dataclass
class OomObjectPart:
    name: str
    objects_xml: str
    count: int


def _fmt(x: int, y: int, flags: int = 0) -> str:
    if flags:
        return f"{x} {y} {flags}"
    return f"{x} {y}"


def _object_xml(symbol_index: int, pts: list[str]) -> str:
    body = ";".join(pts) + ";"
    return (
        f'            <object type="1" symbol="{symbol_index}">\n'
        f'                <coords count="{len(pts)}">{body}</coords>\n'
        f'                <pattern rotation="0"><coord x="0" y="0"/></pattern>\n'
        f"            </object>"
    )


def _path_object(symbol_index: int, coords: list[tuple[int, int]]) -> str:
    if len(coords) < 2:
        return ""
    return _object_xml(symbol_index, [_fmt(x, y) for x, y in coords])


def _area_object_with_holes(
    symbol_index: int, rings: list[list[tuple[int, int]]]
) -> str:
    """Plocha v OOM = PathObject (type=1), prstence oddělené bodem s flagem 18.

    Díra se v Mapperu vykreslí průhledně, takže se pod ní objeví bílé pozadí.
    Les se proto z okolní plochy musí vyříznout – kreslit na něj vlastní
    symbol nejde, bílá v ISOM žádný symbol nemá.
    """
    pts: list[str] = []
    for index, coords in enumerate(rings):
        # Duplicitní uzavírací bod z polygonize pryč – doplníme si ho s flagem.
        ring = list(coords)
        if len(ring) >= 2 and ring[0] == ring[-1]:
            ring = ring[:-1]
        if len(ring) < 3:
            if index == 0:
                return ""
            continue
        pts.extend(_fmt(x, y) for x, y in ring)
        pts.append(_fmt(ring[0][0], ring[0][1], 18))
    if not pts:
        return ""
    return _object_xml(symbol_index, pts)


def _point_object(symbol_index: int, x: int, y: int) -> str:
    return (
        f'            <object type="0" symbol="{symbol_index}">\n'
        f'                <coords count="1">{_fmt(x, y)};</coords>\n'
        f"            </object>"
    )


def _polygon_parts(poly, ring_pts) -> list[_WkbPart]:
    """Obrys + díry jednoho OGR polygonu, ve stejném pořadí jako z WKB."""
    parts: list[_WkbPart] = []
    for i in range(poly.GetGeometryCount()):
        ring = poly.GetGeometryRef(i)
        if not ring:
            continue
        pts = ring_pts(ring)
        parts.append(("line", pts, True) if i == 0 else ("hole", pts))
    return parts


def _geom_objects(
    geom,
    symbol_index: int,
    *,
    ref_x: float,
    ref_y: float,
    scale: int,
    grivation_deg: float,
    ogr,
    elev_at=None,
    clip_bounds: Bounds | None = None,
) -> list[str]:
    """Rozloží OGR geometrii na díly a předá je společné cestě do OOM."""
    from osgeo import ogr as ogr_mod

    gtype = geom.GetGeometryType()
    parts: list[_WkbPart] = []

    def ring_pts(line) -> list[tuple[float, float]]:
        return [(line.GetX(i), line.GetY(i)) for i in range(line.GetPointCount())]

    if gtype in (ogr_mod.wkbPoint, ogr_mod.wkbPoint25D):
        parts.append(("point", geom.GetX(), geom.GetY()))
    elif gtype in (ogr_mod.wkbLineString, ogr_mod.wkbLineString25D):
        parts.append(("line", ring_pts(geom), False))
    elif gtype in (ogr_mod.wkbMultiLineString, ogr_mod.wkbMultiLineString25D):
        for i in range(geom.GetGeometryCount()):
            sub = geom.GetGeometryRef(i)
            if sub:
                parts.append(("line", ring_pts(sub), False))
    elif gtype in (ogr_mod.wkbPolygon, ogr_mod.wkbPolygon25D):
        parts.extend(_polygon_parts(geom, ring_pts))
    elif gtype in (ogr_mod.wkbMultiPolygon, ogr_mod.wkbMultiPolygon25D):
        for i in range(geom.GetGeometryCount()):
            poly = geom.GetGeometryRef(i)
            if poly:
                parts.extend(_polygon_parts(poly, ring_pts))

    return _geom_parts_to_objects(
        parts,
        symbol_index,
        ref_x=ref_x,
        ref_y=ref_y,
        scale=scale,
        grivation_deg=grivation_deg,
        elev_at=elev_at,
        clip_bounds=clip_bounds,
    )


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


def _ogr_line_parts_5514(geom) -> list[list[tuple[float, float]]]:
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
            out.extend(_ogr_line_parts_5514(geom.GetGeometryRef(i)))
    return out


def _wkb_line_parts_5514(wkb: bytes) -> list[list[tuple[float, float]]]:
    parts, _ = _wkb_parts(wkb)
    out: list[list[tuple[float, float]]] = []
    for part in parts:
        if part[0] == "line":
            pts = [(float(x), float(y)) for x, y in part[1]]  # type: ignore[misc]
            if len(pts) >= 2:
                out.append(pts)
    return out


def _hole_rings_as_area_objects(
    parts: list[_WkbPart],
    symbol_index: int,
    *,
    ref_x: float,
    ref_y: float,
    scale: int,
    grivation_deg: float,
    clip_bounds: Bounds | None = None,
) -> list[str]:
    """Každou díru polygonu vykreslí jako samostatnou plnou plochu (např. oliva 520)."""

    def to_map(x: float, y: float) -> tuple[int, int]:
        return projected_to_map_coord(
            x,
            y,
            ref_x=ref_x,
            ref_y=ref_y,
            scale=scale,
            grivation_deg=grivation_deg,
        )

    out: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        index += 1
        if part[0] != "line" or not part[2]:
            continue
        holes: list[list[tuple[float, float]]] = []
        while index < len(parts) and parts[index][0] == "hole":
            holes.append(
                [(float(x), float(y)) for x, y in parts[index][1]]  # type: ignore[misc]
            )
            index += 1
        for hole in holes:
            ring = clip_ring(hole, clip_bounds) if clip_bounds is not None else hole
            if len(ring) < 3:
                continue
            coords = [to_map(x, y) for x, y in ring]
            obj = _area_object_with_holes(symbol_index, [coords])
            if obj:
                out.append(obj)
    return out


def build_zabaged_object_parts(
    zabaged_clean: Path,
    *,
    vectorconf_name: str,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    work_dir: Path,
    clip_bounds: Bounds | None = None,
    courtyard_olive: bool = False,
    prefer_osm_path_lines: list[list[tuple[float, float]]] | None = None,
    omit_path_layers: bool = False,
) -> list[OomObjectPart]:
    use_ogr = True
    try:
        from osgeo import ogr
    except ImportError:
        use_ogr = False
        try:
            import pyogrio.raw  # noqa: F401
        except ImportError:
            return []

    rules = load_vectorconf(vectorconf_name)
    stage = work_dir / "_oom_objects"
    if stage.exists():
        import shutil

        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    # Cesta (širší) má přednost před Pesina na stejné střednici.
    from app.pipeline.osm_paths import (
        COVER_DROP,
        MATCH_M,
        MIN_LENGTH_M,
        ZABAGED_OMIT_PATH_LAYERS,
        ZABAGED_PATH_LAYERS_OSM_FIRST,
        _SegmentIndex,
        centerline_cover_fraction,
        filter_lines_against_centerlines,
    )

    fill_courtyards = bool(courtyard_olive) and preset_id.startswith("sprint")
    olive_index = (
        symbol_index_for_code(preset_id, scale, "520") if fill_courtyards else None
    )
    courtyard_objects: list[str] = []

    # Sprint OOM: ZABAGED Pesina/Cesta ustoupí OSM (PNG beze změny).
    osm_first = bool(prefer_osm_path_lines) and preset_id.startswith("sprint")
    osm_blockers = prefer_osm_path_lines if osm_first else None

    wider_paths = _SegmentIndex()
    parts: list[OomObjectPart] = []
    with zipfile.ZipFile(zabaged_clean) as zf:
        shp_names = sorted(
            Path(n).name for n in zf.namelist() if n.lower().endswith(".shp")
        )

    def _emit_line_objects(
        line_parts: list[list[tuple[float, float]]],
        symbol_index: int,
    ) -> list[str]:
        return _geom_parts_to_objects(
            [("line", pts, False) for pts in line_parts],
            symbol_index,
            ref_x=ref_x,
            ref_y=ref_y,
            scale=scale,
            grivation_deg=grivation_deg,
            clip_bounds=clip_bounds,
        )

    def _filter_vs_osm(
        line_parts: list[list[tuple[float, float]]],
        layer_name: str,
    ) -> list[list[tuple[float, float]]]:
        if not osm_blockers or layer_name not in ZABAGED_PATH_LAYERS_OSM_FIRST:
            return line_parts
        kept, _dropped = filter_lines_against_centerlines(
            line_parts,
            osm_blockers,
            near_m=MATCH_M,
            overlap_drop=COVER_DROP,
            min_length_m=MIN_LENGTH_M,
        )
        return kept

    for shp_name in shp_names:
        layer_name = Path(shp_name).stem
        # Metro (ZABAGED an012) a stanice metra do orienťáckého podkladu nepatří.
        if layer_name in {"Metro", "StaniceMetra"}:
            continue
        if omit_path_layers and layer_name in ZABAGED_OMIT_PATH_LAYERS:
            continue
        shp_path = _extract_shp_from_zip(zabaged_clean, shp_name, stage / layer_name)
        if not shp_path:
            continue
        objects: list[str] = []
        if use_ogr:
            ds = ogr.Open(str(shp_path))
            if not ds:
                continue
            layer = ds.GetLayer()
            if not layer:
                continue
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
                line_parts = _ogr_line_parts_5514(geom)
                line_parts = _filter_vs_osm(line_parts, layer_name)
                if layer_name == "Pesina" and line_parts:
                    if all(
                        centerline_cover_fraction(pts, wider_paths, match_m=MATCH_M)
                        >= COVER_DROP
                        for pts in line_parts
                    ):
                        continue
                if (
                    osm_first
                    and layer_name in ZABAGED_PATH_LAYERS_OSM_FIRST
                ):
                    if not line_parts:
                        continue
                    if layer_name == "Cesta":
                        for pts in line_parts:
                            wider_paths.add_line(pts)
                    objects.extend(_emit_line_objects(line_parts, symbol_index))
                else:
                    if layer_name == "Cesta":
                        for pts in line_parts:
                            wider_paths.add_line(pts)
                    objects.extend(
                        _geom_objects(
                            geom,
                            symbol_index,
                            ref_x=ref_x,
                            ref_y=ref_y,
                            scale=scale,
                            grivation_deg=grivation_deg,
                            clip_bounds=clip_bounds,
                            ogr=ogr,
                        )
                    )
                if fill_courtyards and olive_index is not None and code == "521":
                    # Budova s dírou = nepřístupný dvůr → plná oliva přes detaily uvnitř.
                    courtyard_objects.extend(
                        _hole_rings_as_area_objects(
                            _geom_to_parts(geom),
                            olive_index,
                            ref_x=ref_x,
                            ref_y=ref_y,
                            scale=scale,
                            grivation_deg=grivation_deg,
                            clip_bounds=clip_bounds,
                        )
                    )
        else:
            for props, wkb in _pyogrio_layer_rows(shp_path):
                if "vrstva" not in props:
                    props["vrstva"] = layer_name
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
                line_parts = _wkb_line_parts_5514(wkb)
                line_parts = _filter_vs_osm(line_parts, layer_name)
                if layer_name == "Pesina" and line_parts:
                    if all(
                        centerline_cover_fraction(pts, wider_paths, match_m=MATCH_M)
                        >= COVER_DROP
                        for pts in line_parts
                    ):
                        continue
                if (
                    osm_first
                    and layer_name in ZABAGED_PATH_LAYERS_OSM_FIRST
                ):
                    if not line_parts:
                        continue
                    if layer_name == "Cesta":
                        for pts in line_parts:
                            wider_paths.add_line(pts)
                    objects.extend(_emit_line_objects(line_parts, symbol_index))
                else:
                    if layer_name == "Cesta":
                        for pts in line_parts:
                            wider_paths.add_line(pts)
                    geom_parts, _ = _wkb_parts(wkb)
                    objects.extend(
                        _geom_parts_to_objects(
                            geom_parts,
                            symbol_index,
                            ref_x=ref_x,
                            ref_y=ref_y,
                            scale=scale,
                            grivation_deg=grivation_deg,
                            clip_bounds=clip_bounds,
                        )
                    )
                if fill_courtyards and olive_index is not None and code == "521":
                    geom_parts, _ = _wkb_parts(wkb)
                    courtyard_objects.extend(
                        _hole_rings_as_area_objects(
                            geom_parts,
                            olive_index,
                            ref_x=ref_x,
                            ref_y=ref_y,
                            scale=scale,
                            grivation_deg=grivation_deg,
                            clip_bounds=clip_bounds,
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
    if courtyard_objects:
        parts.append(
            OomObjectPart(
                name="ZABAGED – dvory (oliva 520)",
                objects_xml="\n".join(courtyard_objects),
                count=len(courtyard_objects),
            )
        )
    return parts


def _geom_to_parts(geom) -> list[_WkbPart]:
    """OGR geometrie → stejné parts jako WKB (obrys + díry)."""
    gtype = geom.GetGeometryType() % 1000
    parts: list[_WkbPart] = []

    def ring_pts(ring) -> list[tuple[float, float]]:
        n = ring.GetPointCount()
        return [(ring.GetX(i), ring.GetY(i)) for i in range(n)]

    if gtype == 3:
        parts.extend(_polygon_parts(geom, ring_pts))
    elif gtype == 6:
        for i in range(geom.GetGeometryCount()):
            poly = geom.GetGeometryRef(i)
            if poly is not None:
                parts.extend(_polygon_parts(poly, ring_pts))
    return parts


def _collect_dxf_line_parts(path: Path, *, use_ogr: bool) -> list[list[tuple[float, float]]]:
    """Všechny LINESTRING z DXF (KP srázy = spousta 2bodových úseček)."""
    out: list[list[tuple[float, float]]] = []
    if use_ogr:
        from osgeo import ogr

        ds = ogr.Open(str(path))
        if not ds:
            return out
        for i in range(ds.GetLayerCount()):
            layer = ds.GetLayerByIndex(i)
            if not layer:
                continue
            for feature in layer:
                geom = feature.GetGeometryRef()
                if geom is None:
                    continue
                out.extend(_ogr_line_parts_5514(geom))
        return out
    import pyogrio

    for layer_name, _layer_type in pyogrio.list_layers(path):
        for _props, wkb in _pyogrio_layer_rows(path, layer=layer_name):
            out.extend(_wkb_line_parts_5514(wkb))
    return out


def build_dxf_object_part(
    kp_cwd: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    cliff_symbol: str = "earth_bank",
    clip_bounds: Bounds | None = None,
) -> OomObjectPart | None:
    use_ogr = True
    try:
        from osgeo import ogr
    except ImportError:
        use_ogr = False
        try:
            import pyogrio.raw  # noqa: F401
        except ImportError:
            return None

    temp = kp_cwd / "temp"
    if not temp.is_dir():
        return None
    include_cliffs = cliff_symbol != KP_CLIFF_OFF
    dxf_map = collect_dxf_for_zip(temp, include_cliffs=include_cliffs)
    if not dxf_map:
        return None

    objects: list[str] = []
    elev_at = _load_dem_elev(kp_cwd)
    cliff_ticks: list[tuple[tuple[float, float], tuple[float, float]]] = []
    cliff_line_code: str | None = None
    had_dense_polys = False
    drop_stats: dict[str, int] = {}
    for zip_name, path in sorted(dxf_map.items()):
        code = oom_code_for_dxf(
            zip_name, preset_id=preset_id, cliff_symbol=cliff_symbol
        )
        if not code:
            continue
        symbol_index = symbol_index_for_code(preset_id, scale, code)
        if symbol_index is None:
            continue
        if zip_name in _CLIFF_DXF_NAMES:
            cliff_line_code = code
            for pts in _collect_dxf_line_parts(path, use_ogr=use_ogr):
                if len(pts) == 2:
                    cliff_ticks.append((pts[0], pts[1]))
                elif len(pts) > 2:
                    # Už slepená linie – nechat jako řetěz 2bodových úseků.
                    for i in range(1, len(pts)):
                        cliff_ticks.append((pts[i - 1], pts[i]))
            continue
        if use_ogr:
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
                            grivation_deg=grivation_deg,
                            clip_bounds=clip_bounds,
                            ogr=ogr,
                        )
                    )
        else:
            import pyogrio

            for layer_name, _layer_type in pyogrio.list_layers(path):
                for _props, wkb in _pyogrio_layer_rows(path, layer=layer_name):
                    geom_parts, _ = _wkb_parts(wkb)
                    objects.extend(
                        _geom_parts_to_objects(
                            geom_parts,
                            symbol_index,
                            ref_x=ref_x,
                            ref_y=ref_y,
                            scale=scale,
                            grivation_deg=grivation_deg,
                            clip_bounds=clip_bounds,
                        )
                    )

    if cliff_ticks and cliff_line_code and cliff_symbol != KP_CLIFF_OFF:
        as_polygons = cliff_symbol in (
            KP_CLIFF_ROCK_FACE,
            KP_CLIFF_SYMBOL_206,
        )
        merged = merge_cliff_ticks(
            cliff_ticks,
            as_polygons=as_polygons,
            min_line_m=min_line_length_m(scale),
        )
        # KP výšku srázu nezapisuje, tak si ji doměříme z DEM a nízké schody
        # zahodíme. Bez DEM projde všechno – není podle čeho rozhodovat.
        cliff_lines, drop_stats = filter_by_drop(
            merged.lines, _load_cliff_dem(kp_cwd)
        )
        if cliff_symbol == KP_CLIFF_SYMBOL_206:
            poly_index = symbol_index_for_code(preset_id, scale, KP_CLIFF_206_CODE)
            if poly_index is not None:
                poly_rings = list(merged.polygons)
                for pts in cliff_lines:
                    ring = polyline_to_strip_ring(pts)
                    if ring:
                        poly_rings.append(ring)
                if poly_rings:
                    had_dense_polys = True
                    poly_parts = [("line", ring, True) for ring in poly_rings]
                    objects.extend(
                        _geom_parts_to_objects(
                            poly_parts,
                            poly_index,
                            ref_x=ref_x,
                            ref_y=ref_y,
                            scale=scale,
                            grivation_deg=grivation_deg,
                            clip_bounds=clip_bounds,
                            as_area=True,
                        )
                    )
        else:
            line_index = symbol_index_for_code(preset_id, scale, cliff_line_code)
            if line_index is not None:
                line_parts = [("line", pts, False) for pts in cliff_lines]
                objects.extend(
                    _geom_parts_to_objects(
                        line_parts,
                        line_index,
                        ref_x=ref_x,
                        ref_y=ref_y,
                        scale=scale,
                        grivation_deg=grivation_deg,
                        clip_bounds=clip_bounds,
                        elev_at=elev_at,
                    )
                )
            if merged.polygons:
                poly_index = symbol_index_for_code(
                    preset_id, scale, KP_CLIFF_DENSE_CODE
                )
                if poly_index is not None:
                    had_dense_polys = True
                    poly_parts = [("line", ring, True) for ring in merged.polygons]
                    objects.extend(
                        _geom_parts_to_objects(
                            poly_parts,
                            poly_index,
                            ref_x=ref_x,
                            ref_y=ref_y,
                            scale=scale,
                            grivation_deg=grivation_deg,
                            clip_bounds=clip_bounds,
                            as_area=True,
                        )
                    )

    if not objects:
        return None
    if cliff_symbol == KP_CLIFF_SYMBOL_206:
        cliff_label = "skály (206 plocha)"
    elif cliff_symbol == KP_CLIFF_ROCK_FACE:
        cliff_label = (
            "skály (201 + kamenitý povrch 210)"
            if had_dense_polys
            else "skály (201)"
        )
    elif cliff_symbol == KP_CLIFF_EARTH_BANK:
        cliff_label = "zemní srázy (104)"
    else:
        cliff_label = "Karttapullautin"
    if drop_stats.get("zahozeno"):
        cliff_label += f", {drop_stats['zahozeno']} nízkých zahozeno dle DEM"
    elif drop_stats.get("nezmereno"):
        cliff_label += ", výška nezměřena (chybí DEM)"
    return OomObjectPart(
        name=f"Karttapullautin – vektory ({cliff_label})",
        objects_xml="\n".join(objects),
        count=len(objects),
    )
