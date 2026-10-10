from __future__ import annotations

import math
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.pipeline.cliff_height import (
    filter_by_drop,
    likely_closed_depression,
    rock_ring_is_closed_depression,
)
from app.pipeline.fetch_zabaged import (
    MAX_OSTATNI_PLOCHA_M2,
    ostatni_plocha_too_large,
)
from app.pipeline.geom_clip import Bounds, clip_polyline, clip_ring, point_inside
from app.pipeline.cliff_merge import (
    filter_by_dense_contours,
    filter_cliffs_crossing_buildings,
    filter_earth_bank_lines,
    filter_overlapping_earth_banks,
    filter_rocks_overlapping_blockers,
    merge_cliff_ticks,
    min_line_length_m,
    polyline_is_simple_bank,
    resolve_rock_scarp_overlaps,
    shapely_available,
)
from app.pipeline.dxf_products import collect_dxf_for_zip
from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_symbol_map import (
    KP_CLIFF_206_CODE,
    KP_CLIFF_DENSE_CODE,
    KP_CLIFF_AUTO,
    KP_CLIFF_EARTH_BANK,
    KP_CLIFF_OFF,
    KP_CLIFF_SYMBOL_206,
    oom_code_for_dxf,
    oom_code_for_vectorconf_rule,
    resolve_rock_area_code,
    symbol_index_for_code,
)
from app.pipeline.oom_vectorconf import load_vectorconf, match_feature

# ('point', x, y) | ('line', [(x, y), ...], close)
_WkbPart = tuple[str, object]

_CLIFF_DXF_NAMES = frozenset(
    {"cliffs_small.dxf", "cliffs_large.dxf", "cliffs_rock.dxf"}
)
_TAG_SIDE_OFFSET_M = 2.0

# ZABAGED vrstvy, které skálu NEpřebíjejí (zelená / bílá / vegetační pozadí).
# Vrstevnice sem nepatří – nejsou v ZABAGED a do blockerů se vůbec nenačítají.
_ROCK_OCCUPANCY_EXCEPTION_LAYERS = frozenset(
    {
        "TrvalyTravniPorost",
        "UdrzovanaZelen",
        "LiniovaVegetace",
        "VyznamnyStromLesik",
    }
)
# Plošné ZABAGED objekty, které skálu potlačí (budovy, voda, zpevněné, …).
_ROCK_OCCUPANCY_POLY_LAYERS = frozenset(
    {
        "BudovaJednotlivaNeboBlokBudov",
        "KulnaSklenikFoliovnikPristresek",
        "ArealUceloveZastavby",
        "Hrad",
        "Zamek",
        "RozvalinaZricenina",
        "VezovitaStavba",
        "StavebniObjektZakryty",
        "Tribuna",
        "VodniPlocha",
        "Hrbitov",
        "ParkovisteOdpocivka",
        "OstatniPlochaVSidlech",
        "ArealZeleznicniStanice",
        "Koleiste",
        "OvocnySadZahrada",
        "SkupinaBalvanu",
    }
)
# Liniové ZABAGED objekty (cesty, hydro, srázy, zdi, kolej…); střednice + buffer.
_ROCK_OCCUPANCY_LINE_LAYERS = frozenset(
    {
        "VodniTok",
        "SilniceDalnice",
        "Cesta",
        "Most",
        "Ulice",
        "Pesina",
        "Lavka",
        "Tunel",
        "Podjezd",
        "TramvajovaDraha",
        "ZeleznicniTrat",
        "ZeleznicniVlecka",
        "LanovaDrahaLyzarskyVlek",
        "StupenSraz",
        "Zed",
        "HradbaVal",
        "ElektrickeVedeni",
        "Zabrana",
    }
)

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
        # 100M pixelů = 400 MB ve float32: výřez 36 km² (i 12×3 km, pootočený
        # v S-JTSK) má po metru ~50–65M px. Docker má 12 GB; po pixelech až
        # extrémně protáhlé pásy.
        if self._w * self._h <= 100_000_000:
            try:
                self._grid = self._band.ReadAsArray().astype("float32", copy=False)
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


def _first_dem(
    work_dir: Path,
    names: tuple[str, ...],
    *,
    dirs: tuple[str, ...] = ("contours",),
):
    """Rastr se drží celý v paměti, tak stejný soubor otevírat jen jednou.

    Cache platí pro jeden job – při přechodu jinam se zahodí, jinak by ve worker
    procesu zůstaly viset stovky MB po každé zpracované mapě.

    ``dirs`` pořadí složek (typicky ``dem`` před ``contours`` pro srázy –
    shared ``dem/dem_filled.tif`` je nehlazený; v ``contours/`` bývá jen
    ``dem_smooth.tif``, který rozmazává sklon a dense-contour pak nestřílí).
    """
    global _dem_cache_dir
    if _dem_cache_dir != work_dir:
        _dem_cache.clear()
        _dem_cache_dir = work_dir
    for dem_dir in dirs:
        for name in names:
            dem = work_dir / dem_dir / name
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
    # Side-of-slope u vrstevnic: smooth v contours/ stačí; fallback dem/.
    return _first_dem(
        work_dir,
        ("dem_smooth.tif", "dem_filled.tif"),
        dirs=("contours", "dem"),
    )


def _load_cliff_dem(work_dir: Path):
    """Nehlazený DEM – smooth schod / dense-contour grade rozmázne."""
    return _first_dem(
        work_dir,
        ("dem_filled.tif", "dem_raw.tif", "dem_smooth.tif"),
        dirs=("dem", "contours"),
    )


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
    as_curves: bool = False,
    elev_at=None,
    clip_bounds: Bounds | None = None,
    max_area_vertices: int | None = None,
    max_area_m2: float | None = None,
    area_smooth_level: int = 0,
    area_smooth_barriers=None,
    area_as_curves: bool = False,
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

    def _smoothed(ring, holes):
        if not area_smooth_level:
            return [(ring, holes)]
        from app.pipeline.veg_smooth import smooth_area

        return smooth_area(ring, holes, area_smooth_level, area_smooth_barriers)

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
            # Křivky jen když volající zapne as_curves (vrstevnice: po DP simplify).
            # Plochy s dírami necháme polygonální – srázy/cesty as_curves nezapínají.
            if as_curves and not holes:
                mapped = convert_polyline_to_curves(
                    coords, closed=bool(closed_shape)
                )
                obj = _path_object(symbol_index, mapped)
            elif closed_shape and max_area_vertices:
                for sm_ring, sm_holes in _smoothed(piece, holes):
                    for ring_part, hole_parts in split_area_by_vertices(
                        sm_ring, sm_holes, max_area_vertices, max_area=max_area_m2
                    ):
                        rings = [[to_map(x, y) for x, y in ring_part]] + [
                            [to_map(x, y) for x, y in hole] for hole in hole_parts
                        ]
                        obj = _area_object_with_holes(
                            symbol_index, rings, as_curves=area_as_curves
                        )
                        if obj:
                            out.append(obj)
                continue
            elif closed_shape:
                for sm_ring, sm_holes in _smoothed(piece, holes):
                    rings = [[to_map(x, y) for x, y in sm_ring]] + [
                        [to_map(x, y) for x, y in hole] for hole in sm_holes
                    ]
                    obj = _area_object_with_holes(
                        symbol_index, rings, as_curves=area_as_curves
                    )
                    if obj:
                        out.append(obj)
                continue
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


# OpenOrienteering MapCoord flags (map_coord.h) – hodnoty se nesmí měnit.
MAP_COORD_CURVE_START = 1  # první ze 4 bodů kubické Bézier
MAP_COORD_CLOSE_POINT = 2
MAP_COORD_HOLE_POINT = 16
# OpenOrienteering MapCoord::DashPoint – fousy sloupů na vedení / dash symbol.
MAP_COORD_DASH_POINT = 32
# util.h: BEZIER_KAPPA / sqrt(2) – délka handle při „Převést na křivky“.
_BEZIER_HANDLE_DISTANCE = 0.390524291729


def _normalize_xy(dx: float, dy: float) -> tuple[float, float]:
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return 0.0, 0.0
    return dx / length, dy / length


def convert_polyline_to_curves(
    coords: list[tuple[int, int]],
    *,
    closed: bool = False,
) -> list[tuple[int, int] | tuple[int, int, int]]:
    """Stejný algoritmus jako Mapper ``PathObject::convertRangeToCurves``.

    Polygonální úseky → kubické Bézier: bod se flagem CurveStart (1), dva
    handly, koncový bod. Uzavřená linie dostane na konci ClosePoint|HolePoint
    (18), stejně jako Mapper u uzavřených PathPart.
    """
    if len(coords) < 2:
        return [(int(x), int(y)) for x, y in coords]

    pts: list[list[int]] = [[int(x), int(y), 0] for x, y in coords]
    if closed:
        if pts[0][0] != pts[-1][0] or pts[0][1] != pts[-1][1]:
            pts.append([pts[0][0], pts[0][1], 0])
        if len(pts) < 3:
            pts[-1][2] = MAP_COORD_CLOSE_POINT | MAP_COORD_HOLE_POINT
            return [
                (p[0], p[1], p[2]) if p[2] else (p[0], p[1]) for p in pts
            ]

    start_index = 0
    end_index = len(pts) - 1
    pts[start_index][2] |= MAP_COORD_CURVE_START

    if closed:
        dx = float(pts[start_index + 1][0] - pts[end_index - 1][0])
        dy = float(pts[start_index + 1][1] - pts[end_index - 1][1])
    else:
        dx = float(pts[end_index][0] - pts[end_index - 1][0])
        dy = float(pts[end_index][1] - pts[end_index - 1][1])
    tx, ty = _normalize_xy(dx, dy)
    baseline = (
        math.hypot(
            pts[end_index][0] - pts[end_index - 1][0],
            pts[end_index][1] - pts[end_index - 1][1],
        )
        * _BEZIER_HANDLE_DISTANCE
    )
    end_handle = [
        round(pts[end_index][0] - tx * baseline),
        round(pts[end_index][1] - ty * baseline),
        0,
    ]

    if closed:
        dx = float(pts[start_index + 1][0] - pts[end_index - 1][0])
        dy = float(pts[start_index + 1][1] - pts[end_index - 1][1])
    else:
        dx = float(pts[start_index + 1][0] - pts[start_index][0])
        dy = float(pts[start_index + 1][1] - pts[start_index][1])
    tx, ty = _normalize_xy(dx, dy)
    baseline = (
        math.hypot(
            pts[start_index + 1][0] - pts[start_index][0],
            pts[start_index + 1][1] - pts[start_index][1],
        )
        * _BEZIER_HANDLE_DISTANCE
    )
    pts.insert(
        start_index + 1,
        [
            round(pts[start_index][0] + tx * baseline),
            round(pts[start_index][1] + ty * baseline),
            0,
        ],
    )
    end_index += 1

    c = start_index + 2
    while c < end_index:
        dx = float(pts[c + 1][0] - pts[c - 2][0])
        dy = float(pts[c + 1][1] - pts[c - 2][1])
        tx, ty = _normalize_xy(dx, dy)

        baseline = (
            math.hypot(pts[c][0] - pts[c - 2][0], pts[c][1] - pts[c - 2][1])
            * _BEZIER_HANDLE_DISTANCE
        )
        pts.insert(
            c,
            [
                round(pts[c][0] - tx * baseline),
                round(pts[c][1] - ty * baseline),
                0,
            ],
        )
        c += 1
        end_index += 1

        pts[c][2] |= MAP_COORD_CURVE_START

        baseline = (
            math.hypot(pts[c + 1][0] - pts[c][0], pts[c + 1][1] - pts[c][1])
            * _BEZIER_HANDLE_DISTANCE
        )
        pts.insert(
            c + 1,
            [
                round(pts[c][0] + tx * baseline),
                round(pts[c][1] + ty * baseline),
                0,
            ],
        )
        c += 1
        end_index += 1
        c += 1  # for-loop ++c v Mapperu

    pts.insert(end_index, end_handle)
    end_index += 1

    if closed:
        # Uzavírací bod (shodný s prvním) – ClosePoint|HolePoint jako u ploch.
        pts[end_index][2] |= MAP_COORD_CLOSE_POINT | MAP_COORD_HOLE_POINT

    out: list[tuple[int, int] | tuple[int, int, int]] = []
    for x, y, flags in pts:
        if flags:
            out.append((x, y, flags))
        else:
            out.append((x, y))
    return out


def _object_xml(symbol_index: int, pts: list[str]) -> str:
    body = ";".join(pts) + ";"
    return (
        f'            <object type="1" symbol="{symbol_index}">\n'
        f'                <coords count="{len(pts)}">{body}</coords>\n'
        f'                <pattern rotation="0"><coord x="0" y="0"/></pattern>\n'
        f"            </object>"
    )


def _path_object(
    symbol_index: int,
    coords: list[tuple[int, int] | tuple[int, int, int]],
) -> str:
    """LineString v OOM. Volitelný 3. prvek souřadnice = MapCoord flags (např. DashPoint 32)."""
    if len(coords) < 2:
        return ""
    pts: list[str] = []
    for c in coords:
        if len(c) >= 3:
            pts.append(_fmt(int(c[0]), int(c[1]), int(c[2])))
        else:
            pts.append(_fmt(int(c[0]), int(c[1])))
    return _object_xml(symbol_index, pts)


def _area_object_with_holes(
    symbol_index: int,
    rings: list[list[tuple[int, int]]],
    *,
    as_curves: bool = False,
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
        if as_curves and len(ring) >= 3:
            # „Převést na křivky“ z Mapperu; konec prstence nese flag 18 sám.
            pts.extend(
                _fmt(*c) for c in convert_polyline_to_curves(ring, closed=True)
            )
            continue
        pts.extend(_fmt(x, y) for x, y in ring)
        pts.append(_fmt(ring[0][0], ring[0][1], 18))
    if not pts:
        return ""
    return _object_xml(symbol_index, pts)


_Ring = list[tuple[float, float]]


def split_area_by_vertices(
    ring: _Ring,
    holes: list[_Ring],
    max_vertices: int,
    *,
    max_area: float | None = None,
) -> list[tuple[_Ring, list[_Ring]]]:
    """Plochu nad limitem vrcholů / plochy (m²) rozřízne – přednostně v hrdlech.

    OCAD při výběru / posunu obrazovky přepočítává celý objekt bod po bodu –
    louka přes celou mapu s tisíci vrcholy ho brzdí o sekundy. Jen pro výplně
    bez obrysu (vegetace), jinak by byly řezy vidět. Viz ``area_split``.
    """
    from app.pipeline.area_split import split_area

    return split_area(ring, holes, max_vertices, max_area=max_area)


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


def _wkb_area_m2(wkb: bytes) -> float | None:
    """Plocha polygonů z WKB (shoelace); None když nejde spočítat."""
    try:
        parts, _ = _wkb_parts(wkb)
    except Exception:
        return None
    total = 0.0
    found = False
    for part in parts:
        if part[0] != "line" or not part[2]:
            continue
        pts = part[1]
        if not isinstance(pts, list) or len(pts) < 3:
            continue
        a = 0.0
        for i in range(len(pts) - 1):
            x1, y1 = float(pts[i][0]), float(pts[i][1])
            x2, y2 = float(pts[i + 1][0]), float(pts[i + 1][1])
            a += x1 * y2 - x2 * y1
        total += abs(a) * 0.5
        found = True
    return total if found else None


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
    omit_layers: frozenset[str] | set[str] | None = None,
    max_ostatni_m2: float | None = MAX_OSTATNI_PLOCHA_M2,
    ostatni_as_403: bool = False,
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
        centerline_index,
        filter_lines_against_centerlines,
    )

    fill_courtyards = bool(courtyard_olive)
    olive_code = "527" if preset_id.startswith("mtbo") else "520"
    olive_index = (
        symbol_index_for_code(preset_id, scale, olive_code) if fill_courtyards else None
    )
    courtyard_objects: list[str] = []
    building_codes = frozenset({"521", "526"})

    # OSM pěšiny/cesty mají přednost před ZABAGED na stejné střednici (všechny disciplíny).
    osm_first = bool(prefer_osm_path_lines)
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

    osm_index = None

    def _filter_vs_osm(
        line_parts: list[list[tuple[float, float]]],
        layer_name: str,
    ) -> list[list[tuple[float, float]]]:
        nonlocal osm_index
        if not osm_blockers or layer_name not in ZABAGED_PATH_LAYERS_OSM_FIRST:
            return line_parts
        if osm_index is None:
            osm_index = centerline_index(osm_blockers)
        kept, _dropped = filter_lines_against_centerlines(
            line_parts,
            osm_blockers,
            near_m=MATCH_M,
            overlap_drop=COVER_DROP,
            min_length_m=MIN_LENGTH_M,
            index=osm_index,
        )
        return kept

    skip_layers = frozenset(omit_layers or ())
    for shp_name in shp_names:
        layer_name = Path(shp_name).stem
        # Metro (ZABAGED an012) a stanice metra do orienťáckého podkladu nepatří.
        if layer_name in {"Metro", "StaniceMetra"}:
            continue
        if layer_name in skip_layers:
            continue
        if omit_path_layers and layer_name in ZABAGED_OMIT_PATH_LAYERS:
            continue
        if layer_name == "OstatniPlochaVSidlech" and max_ostatni_m2 is None:
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
                if layer_name == "OstatniPlochaVSidlech":
                    geom_probe = feature.GetGeometryRef()
                    geom_area = (
                        float(geom_probe.GetArea())
                        if geom_probe is not None
                        else None
                    )
                    if ostatni_plocha_too_large(
                        props,
                        geom_area_m2=geom_area,
                        max_area_m2=max_ostatni_m2
                        if max_ostatni_m2 is not None
                        else MAX_OSTATNI_PLOCHA_M2,
                    ):
                        continue
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
                if (
                    layer_name == "OstatniPlochaVSidlech"
                    and ostatni_as_403
                    and code
                ):
                    code = "403"
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
                # StupenSraz → 104: stejný min-length + zamotané jako DEM (~50 m @ 10k).
                if layer_name == "StupenSraz":
                    line_parts = filter_earth_bank_lines(line_parts, scale=scale)
                    if not line_parts:
                        continue
                    objects.extend(_emit_line_objects(line_parts, symbol_index))
                elif (
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
                if fill_courtyards and olive_index is not None and code in building_codes:
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
                if layer_name == "OstatniPlochaVSidlech" and ostatni_plocha_too_large(
                    props,
                    geom_area_m2=_wkb_area_m2(wkb),
                    max_area_m2=max_ostatni_m2
                    if max_ostatni_m2 is not None
                    else MAX_OSTATNI_PLOCHA_M2,
                ):
                    continue
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
                if (
                    layer_name == "OstatniPlochaVSidlech"
                    and ostatni_as_403
                    and code
                ):
                    code = "403"
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
                if layer_name == "StupenSraz":
                    line_parts = filter_earth_bank_lines(line_parts, scale=scale)
                    if not line_parts:
                        continue
                    objects.extend(_emit_line_objects(line_parts, symbol_index))
                elif (
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
                if fill_courtyards and olive_index is not None and code in building_codes:
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
                name="ZABAGED – dvory (oliva)",
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
    """Všechny LINESTRING z DXF (srázy = spousta 2bodových úseček).

    Cliff DXF z ``write_cliff_ticks_dxf`` čteme nativně – GDAL OGR na minimálním
    ASCII DXF (bez TABLES) v Dockeru často vrátí 0 prvků.
    """
    path = Path(path)
    name = path.name.lower()
    if name in {"c_rock.dxf", "c2g.dxf"} or name.startswith(
        "cliffs_"
    ):
        from app.pipeline.cliffs_dem import parse_cliff_ticks_dxf

        ticks = parse_cliff_ticks_dxf(path)
        if ticks:
            return [[a, b] for a, b in ticks]

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


def _load_building_rings_for_cliffs(
    *,
    zabaged_clean: Path | None,
    work_dir: Path | None,
) -> list[list[tuple[float, float]]]:
    """Budovy ZABAGED (+ OSM fallback) v S-JTSK – filtr srázů přes dům."""
    rings: list[list[tuple[float, float]]] = []
    if zabaged_clean is not None and zabaged_clean.is_file():
        try:
            from app.pipeline.osm_paths import _zabaged_polygons
            from app.pipeline.ruian_buildings import ZABAGED_OMIT_BUILDING_LAYERS

            rings.extend(
                _zabaged_polygons(
                    zabaged_clean, layers=frozenset(ZABAGED_OMIT_BUILDING_LAYERS)
                )
            )
        except Exception:
            pass
    if work_dir is not None:
        gj = work_dir / "osm_paths" / "buildings.geojson"
        if gj.is_file():
            try:
                import json

                data = json.loads(gj.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if data:
                for feat in data.get("features") or []:
                    geom = feat.get("geometry") or {}
                    gtype = geom.get("type")
                    coords = geom.get("coordinates") or []
                    if gtype == "Polygon" and coords:
                        ring = [(float(x), float(y)) for x, y in coords[0]]
                        if len(ring) >= 3:
                            rings.append(ring)
                    elif gtype == "MultiPolygon":
                        for poly in coords:
                            if not poly:
                                continue
                            ring = [(float(x), float(y)) for x, y in poly[0]]
                            if len(ring) >= 3:
                                rings.append(ring)
    return rings


def _load_zabaged_line_parts(
    zabaged_clean: Path,
    *,
    layers: frozenset[str],
) -> list[list[tuple[float, float]]]:
    """Liniové ZABAGED geometrie (S-JTSK) pro occupancy filtr skal."""
    if not layers or not zabaged_clean.is_file():
        return []
    try:
        from app.pipeline.osm_paths import (
            _iter_line_parts_from_shp,
            _zabaged_shp_members,
        )
    except Exception:
        return []
    members = _zabaged_shp_members(zabaged_clean, layers=layers)
    if not members:
        return []
    import shutil
    import tempfile
    from zipfile import ZipFile

    lines: list[list[tuple[float, float]]] = []
    stage = Path(tempfile.mkdtemp(prefix="rock_occ_ln_"))
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
                lines.extend(_iter_line_parts_from_shp(shp))
            except Exception:
                continue
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return lines


def _load_rock_occupancy_blockers(
    *,
    zabaged_clean: Path | None,
    work_dir: Path | None,
) -> tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]]]:
    """Polygony + linie, které skálu potlačí (bez zeleně/bílé/vrstevnic)."""
    polys: list[list[tuple[float, float]]] = []
    lines: list[list[tuple[float, float]]] = []
    assert not (
        _ROCK_OCCUPANCY_POLY_LAYERS & _ROCK_OCCUPANCY_EXCEPTION_LAYERS
    ), "poly blockers must not include vegetation exceptions"
    assert not (
        _ROCK_OCCUPANCY_LINE_LAYERS & _ROCK_OCCUPANCY_EXCEPTION_LAYERS
    ), "line blockers must not include vegetation exceptions"

    if zabaged_clean is not None and zabaged_clean.is_file():
        try:
            from app.pipeline.osm_paths import _zabaged_polygons

            polys.extend(
                _zabaged_polygons(
                    zabaged_clean, layers=_ROCK_OCCUPANCY_POLY_LAYERS
                )
            )
        except Exception:
            pass
        try:
            lines.extend(
                _load_zabaged_line_parts(
                    zabaged_clean, layers=_ROCK_OCCUPANCY_LINE_LAYERS
                )
            )
        except Exception:
            pass

    if work_dir is not None:
        try:
            from app.pipeline.osm_paths import load_osm_path_lines

            lines.extend(load_osm_path_lines(work_dir))
        except Exception:
            pass
        # OSM vodní plochy / zpevněné z features.geojson (pokud jsou).
        feat_gj = work_dir / "osm_paths" / "features.geojson"
        if feat_gj.is_file():
            try:
                import json

                data = json.loads(feat_gj.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if data:
                blocker_kinds = frozenset(
                    {
                        "water_body",
                        "wetland",
                        "parking",
                        "playground",
                        "pitch",
                        "pedestrian_area",
                        "platform",
                        "building",
                        "water_well_building",
                    }
                )
                line_kinds = frozenset(
                    {"fence", "wall", "power_line", "power_line_major"}
                )
                # hedge = vegetace → výjimka (neblokuje)
                for feat in data.get("features") or []:
                    props = feat.get("properties") or {}
                    kind = str(props.get("kind") or "").strip().lower()
                    geom = feat.get("geometry") or {}
                    gtype = geom.get("type")
                    coords = geom.get("coordinates") or []
                    if kind in blocker_kinds and gtype == "Polygon" and coords:
                        ring = [(float(x), float(y)) for x, y in coords[0]]
                        if len(ring) >= 3:
                            polys.append(ring)
                    elif kind in blocker_kinds and gtype == "MultiPolygon":
                        for poly in coords:
                            if not poly:
                                continue
                            ring = [(float(x), float(y)) for x, y in poly[0]]
                            if len(ring) >= 3:
                                polys.append(ring)
                    elif kind in line_kinds and gtype == "LineString" and len(coords) >= 2:
                        lines.append([(float(x), float(y)) for x, y in coords])
    return polys, lines


def build_dxf_object_part(
    kp_cwd: Path,
    *,
    preset_id: str,
    scale: int,
    ref_x: float,
    ref_y: float,
    grivation_deg: float,
    cliff_symbol: str = "auto",
    clip_bounds: Bounds | None = None,
    zabaged_clean: Path | None = None,
    building_rings: list[list[tuple[float, float]]] | None = None,
    contour_interval_m: float | None = None,
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
    cliff_dem = _load_cliff_dem(kp_cwd)
    cliff_groups: dict[str, list[tuple[tuple[float, float], tuple[float, float]]]] = {}
    had_dense_polys = False
    drop_dropped = 0
    drop_unmeasured = 0
    earth_tangled = 0
    earth_pit_skip = 0
    rock_depression_skip = 0
    rock_polys_n = 0
    rock_ticks_n = 0
    earth_lines_n = 0
    overlap_drop = 0
    earth_overlap_drop = 0
    building_drop = 0
    occupancy_drop = 0
    dense_contour_drop = 0
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
            group = cliff_groups.setdefault(code, [])
            for pts in _collect_dxf_line_parts(path, use_ogr=use_ogr):
                if len(pts) == 2:
                    group.append((pts[0], pts[1]))
                elif len(pts) > 2:
                    for i in range(1, len(pts)):
                        group.append((pts[i - 1], pts[i]))
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

    # Nejdřív sloučit po kódech, pak společné filtry (budovy, překryv skála×104).
    pending_earth: list[list[tuple[float, float]]] = []
    pending_rocks: list[list[tuple[float, float]]] = []
    discarded_earth: list[tuple[list[tuple[float, float]], str]] = []
    discarded_rocks: list[tuple[list[tuple[float, float]], str]] = []
    rock_area_code = resolve_rock_area_code(preset_id, scale) or KP_CLIFF_DENSE_CODE
    if cliff_symbol == KP_CLIFF_SYMBOL_206:
        rock_area_code = KP_CLIFF_206_CODE

    from app.pipeline.uzitecne_vectors import (
        dropped_by_identity,
        write_cliff_inspection_vectors,
    )

    for cliff_line_code, cliff_ticks in cliff_groups.items():
        if not cliff_ticks or cliff_symbol == KP_CLIFF_OFF:
            continue
        # Zem (104) jen linie. Skála: hustý shluk → plocha 201.2/206; zbytek 201
        # (i dlouhé stěny) se zahazuje — viz merge_cliff_ticks(as_polygons=True).
        is_earth = cliff_line_code == "104"
        if not is_earth:
            rock_ticks_n += len(cliff_ticks)
        as_polygons = (
            cliff_symbol == KP_CLIFF_SYMBOL_206 or cliff_line_code == "201"
        )
        # Zamotané nejdřív necháme v merge projít (reject_tangled=False), ať
        # uzitecne/vyhozene dostane duvod=zamotany. Min-délka zůstává v merge.
        merged = merge_cliff_ticks(
            cliff_ticks,
            as_polygons=as_polygons,
            min_line_m=min_line_length_m(scale, earth=is_earth),
            reject_tangled=False,
        )
        raw_lines = merged.lines
        if is_earth:
            clean_lines = [pts for pts in raw_lines if polyline_is_simple_bank(pts)]
            for pts in dropped_by_identity(raw_lines, clean_lines):
                discarded_earth.append((pts, "zamotany"))
            earth_tangled += len(raw_lines) - len(clean_lines)
            raw_lines = clean_lines
        # KP výšku srázu nezapisuje, tak si ji doměříme z DEM a nízké schody
        # zahodíme. Bez DEM projde všechno – není podle čeho rozhodovat.
        before_drop = list(raw_lines)
        cliff_lines, drop_stats = filter_by_drop(raw_lines, cliff_dem)
        if is_earth:
            for pts in dropped_by_identity(before_drop, cliff_lines):
                discarded_earth.append((pts, "nizky_schod"))
        drop_dropped += int(drop_stats.get("zahozeno") or 0)
        drop_unmeasured += int(drop_stats.get("nezmereno") or 0)
        if is_earth and cliff_dem is not None:
            kept_earth: list[list[tuple[float, float]]] = []
            for pts in cliff_lines:
                pit = likely_closed_depression(pts, cliff_dem)
                if pit is True:
                    earth_pit_skip += 1
                    discarded_earth.append((pts, "deprese"))
                    continue
                kept_earth.append(pts)
            cliff_lines = kept_earth
        if is_earth:
            pending_earth.extend(cliff_lines)
        if merged.polygons and as_polygons:
            if cliff_dem is not None:
                kept_rocks: list[list[tuple[float, float]]] = []
                for ring in merged.polygons:
                    pit = rock_ring_is_closed_depression(ring, cliff_dem)
                    if pit is True:
                        rock_depression_skip += 1
                        discarded_rocks.append((ring, "deprese"))
                        continue
                    kept_rocks.append(ring)
                pending_rocks.extend(kept_rocks)
            else:
                pending_rocks.extend(merged.polygons)

    bldg = building_rings
    if bldg is None:
        bldg = _load_building_rings_for_cliffs(
            zabaged_clean=zabaged_clean, work_dir=kp_cwd
        )
    if bldg:
        before_e, before_r = list(pending_earth), list(pending_rocks)
        pending_earth, pending_rocks, building_drop = filter_cliffs_crossing_buildings(
            pending_earth, pending_rocks, bldg
        )
        for pts in dropped_by_identity(before_e, pending_earth):
            discarded_earth.append((pts, "budova"))
        for ring in dropped_by_identity(before_r, pending_rocks):
            discarded_rocks.append((ring, "budova"))
    if pending_rocks and pending_earth:
        before_r = list(pending_rocks)
        pending_rocks, pending_earth, overlap_drop = resolve_rock_scarp_overlaps(
            pending_rocks, pending_earth
        )
        for ring in dropped_by_identity(before_r, pending_rocks):
            discarded_rocks.append((ring, "prekryv_104"))
    if pending_rocks:
        blocker_polys, blocker_lines = _load_rock_occupancy_blockers(
            zabaged_clean=zabaged_clean, work_dir=kp_cwd
        )
        if blocker_polys or blocker_lines:
            before_r = list(pending_rocks)
            pending_rocks, occupancy_drop = filter_rocks_overlapping_blockers(
                pending_rocks, blocker_polys, blocker_lines
            )
            for ring in dropped_by_identity(before_r, pending_rocks):
                discarded_rocks.append((ring, "occupancy"))
    # Husté vrstevnice → jen 104 pryč. Skály (201.2/206) ne – detekce skal je
    # právě na strmém schodu; dense-contour by je systematicky vymazal (Sachrův
    # 457/1859 ticků → 0 ploch i po morph). Petr 2026-10-06: 104 neměnit.
    interval = float(contour_interval_m) if contour_interval_m else 5.0
    dense_filter_ran = False
    if cliff_dem is not None and pending_earth:
        before_e = list(pending_earth)
        pending_earth, _ignored_rocks, dense_contour_drop = filter_by_dense_contours(
            pending_earth, [], cliff_dem, interval_m=interval
        )
        for pts in dropped_by_identity(before_e, pending_earth):
            discarded_earth.append((pts, "huste_vrstevnice"))
        dense_filter_ran = True

    if len(pending_earth) >= 2:
        before_e = list(pending_earth)
        pending_earth, earth_overlap_drop = filter_overlapping_earth_banks(pending_earth)
        for pts in dropped_by_identity(before_e, pending_earth):
            discarded_earth.append((pts, "prekryv_104x104"))

    try:
        write_cliff_inspection_vectors(
            kp_cwd,
            used_earth=pending_earth,
            used_rocks=pending_rocks,
            discarded_earth=discarded_earth,
            discarded_rocks=discarded_rocks,
            rock_code=rock_area_code,
        )
    except Exception:
        pass

    if pending_earth:
        line_index = symbol_index_for_code(preset_id, scale, "104")
        if line_index is not None:
            earth_lines_n += len(pending_earth)
            line_parts = [("line", pts, False) for pts in pending_earth]
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

    if pending_rocks:
        poly_index = symbol_index_for_code(preset_id, scale, rock_area_code)
        if poly_index is not None:
            had_dense_polys = True
            rock_polys_n += len(pending_rocks)
            poly_parts = [("line", ring, True) for ring in pending_rocks]
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
    codes = set(cliff_groups)
    if cliff_symbol == KP_CLIFF_SYMBOL_206:
        cliff_label = "skály (206 plocha)"
    elif cliff_symbol == KP_CLIFF_EARTH_BANK:
        cliff_label = "zemní srázy (104)"
    elif cliff_symbol == KP_CLIFF_AUTO and "201" in codes and "104" in codes:
        cliff_label = (
            "skála (plocha 201.2/206) a zem (104)"
            if had_dense_polys
            else "skála (bez plochy) a zem (104)"
        )
    elif "201" in codes and "104" in codes:
        cliff_label = (
            "skála (plocha 201.2/206) a zem (104)"
            if had_dense_polys
            else "skála (bez plochy) a zem (104)"
        )
    elif "201" in codes:
        cliff_label = (
            "skála (plocha 201.2/206)" if had_dense_polys else "skála (bez plochy)"
        )
    else:
        cliff_label = "srázy"
    detail_bits: list[str] = []
    if rock_ticks_n:
        detail_bits.append(f"{rock_ticks_n} skalních ticků")
    if rock_polys_n:
        detail_bits.append(f"skála→{rock_polys_n} ploch 201.2/206")
    elif rock_ticks_n and not shapely_available():
        # Bez shapely morph vrátí 0 ploch a zbytky 201 se zahodí — typický
        # Docker bug před 2.2.21 (shapely chybělo v requirements.txt).
        detail_bits.append(
            "skála→0 ploch (chybí balíček shapely v image – morph neběží)"
        )
    if rock_depression_skip:
        detail_bits.append(
            f"{rock_depression_skip} skála=deprese (přednost vrstevnicím)"
        )
    if earth_lines_n or "104" in codes:
        bit = f"104→{earth_lines_n} linií"
        extras = []
        if earth_tangled:
            extras.append(f"{earth_tangled} zamotaných zahozeno")
        if earth_pit_skip:
            extras.append(f"{earth_pit_skip} možná jáma (nejisté, zahozeno)")
        if extras:
            bit += f" ({', '.join(extras)})"
        detail_bits.append(bit)
    if overlap_drop:
        detail_bits.append(f"překryv skála×104→{overlap_drop} skal pryč (přednost srázu)")
    if earth_overlap_drop:
        detail_bits.append(
            f"překryv 104×104→{earth_overlap_drop} kratších srázů pryč"
        )
    if occupancy_drop:
        detail_bits.append(f"{occupancy_drop} skála přes jiný objekt zahozeno")
    if dense_filter_ran:
        # Vždy logovat (i 0) – ověření, že filtr běží (dřív ticho = bug).
        # Od 2.2.20 jen 104; skály dense-contour neřeší.
        detail_bits.append(
            f"{dense_contour_drop}×104 v hustých vrstevnicích zahozeno"
        )
    if building_drop:
        detail_bits.append(f"{building_drop} přes budovu zahozeno")
    if detail_bits:
        cliff_label += "; " + "; ".join(detail_bits)
    if drop_dropped:
        cliff_label += f", {drop_dropped} nízkých zahozeno dle DEM"
    elif drop_unmeasured:
        cliff_label += ", výška nezměřena (chybí DEM)"
    # Legacy temp/vegetation.pgw = starší rastrový zdroj; jinak kandidáti z DEM.
    from_legacy_raster = (temp / "vegetation.pgw").is_file()
    src_label = "legacy raster" if from_legacy_raster else "DEM kandidáti"
    return OomObjectPart(
        name=f"{src_label} – vektory ({cliff_label})",
        objects_xml="\n".join(objects),
        count=len(objects),
    )
