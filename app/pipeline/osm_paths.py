"""OSM pěšiny (path/footway) do OOM, bez duplicit se ZABAGED cestami."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile

from app.pipeline.crs_5514 import wgs84_to_projected
from app.pipeline.fetch_openzu import USER_AGENT
from app.pipeline.oom_coords import projected_to_map_coord
from app.pipeline.oom_import import OomObjectPart, _path_object, _pyogrio_layer_rows, _wkb_parts
from app.pipeline.oom_symbol_map import symbol_index_for_code

OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
QUERY_TIMEOUT_S = 60

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
NEAR_M = 12.0
OVERLAP_DROP = 0.55
SAMPLE_M = 10.0
MIN_LENGTH_M = 20.0
GRID_M = 30.0
SKIP_FOOTWAY = frozenset({"sidewalk", "crossing"})


def _overpass_ql(south: float, west: float, north: float, east: float) -> str:
    bbox = f"{south},{west},{north},{east}"
    return (
        "[out:json][timeout:45];"
        f'('
        f'way["highway"="path"]({bbox});'
        f'way["highway"="footway"]({bbox});'
        f");out geom;"
    )


def fetch_osm_path_elements(
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log=None,
) -> list[dict]:
    west, south, east, north = bbox_wgs84
    body = _overpass_ql(south, west, north, east).encode("utf-8")
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
                log(f"OSM Overpass: {len(elements)} way (path/footway)")
            return elements
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_err = exc
            if log:
                log(f"OSM Overpass {url}: {exc}")
    if log:
        log(f"OSM pěšiny: Overpass selhal ({last_err})")
    return []


def _way_skip_reason(tags: dict) -> str | None:
    if (tags.get("footway") or "").lower() in SKIP_FOOTWAY:
        return "chodník/přejezd"
    if (tags.get("area") or "").lower() == "yes":
        return "area"
    if (tags.get("indoor") or "").lower() == "yes":
        return "indoor"
    hw = (tags.get("highway") or "").lower()
    if hw not in {"path", "footway"}:
        return "highway"
    return None


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

    def nearest(self, x: float, y: float) -> float:
        i = int(math.floor(x / self.cell_m))
        j = int(math.floor(y / self.cell_m))
        best = float("inf")
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
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
    near = sum(1 for x, y in samples if index.nearest(x, y) <= near_m)
    return near / len(samples)


def _zabaged_path_lines(zabaged_clean: Path) -> list[list[tuple[float, float]]]:
    lines: list[list[tuple[float, float]]] = []
    try:
        import pyogrio.raw  # noqa: F401
    except ImportError:
        return lines
    with ZipFile(zabaged_clean) as zf:
        names = zf.namelist()
    import tempfile
    import shutil

    stage = Path(tempfile.mkdtemp(prefix="osm_zab_"))
    try:
        with ZipFile(zabaged_clean) as zf:
            for n in names:
                stem = Path(n).stem
                if stem not in ZABAGED_PATH_LAYERS:
                    continue
                dest = stage / Path(n).name
                dest.write_bytes(zf.read(n))
        for shp in stage.glob("*.shp"):
            if shp.stem not in ZABAGED_PATH_LAYERS:
                continue
            for _props, wkb in _pyogrio_layer_rows(shp):
                parts, _ = _wkb_parts(wkb)
                for part in parts:
                    if part[0] == "line":
                        pts = list(part[1])  # type: ignore[arg-type]
                        if len(pts) >= 2:
                            lines.append([(float(x), float(y)) for x, y in pts])
    finally:
        shutil.rmtree(stage, ignore_errors=True)
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
        if index.nearest(x, y) > near_m:
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
    # overlap_drop: dřív práh pro celou way; pořezání překrytých úseků ho nepotřebuje.
    _ = overlap_drop
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
        parts = [
            p
            for p in unique_polyline_parts(line, index, near_m=near_m)
            if polyline_length(p) >= MIN_LENGTH_M
        ]
        if not parts:
            dropped += 1
            continue
        kept.extend(parts)
    return kept, dropped


def prepare_osm_paths(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    zabaged_clean: Path | None,
    *,
    log=None,
) -> Path | None:
    elements = fetch_osm_path_elements(bbox_wgs84, log=log)
    osm_lines: list[list[tuple[float, float]]] = []
    skipped = 0
    for el in elements:
        pts = osm_way_to_5514(el)
        if pts is None:
            skipped += 1
            continue
        osm_lines.append(pts)
    zabaged_lines: list[list[tuple[float, float]]] = []
    if zabaged_clean and zabaged_clean.is_file():
        zabaged_lines = _zabaged_path_lines(zabaged_clean)
        if log:
            log(f"OSM dedup: {len(zabaged_lines)} ZABAGED linií (cesty/pěšiny)")
    kept, dropped = filter_osm_against_zabaged(osm_lines, zabaged_lines)
    if log:
        log(
            f"OSM pěšiny: {len(kept)} ponecháno, {dropped} duplicit/krátkých, "
            f"{skipped} přeskočeno (tag)"
        )
    dest_dir = work_dir / "osm_paths"
    dest_dir.mkdir(parents=True, exist_ok=True)
    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"source": "osm", "highway": "path"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[x, y] for x, y in line],
                },
            }
            for line in kept
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
    code = "507"
    symbol_index = symbol_index_for_code(preset_id, scale, code)
    if symbol_index is None:
        return []
    data = json.loads(gj_path.read_text(encoding="utf-8"))
    objects: list[str] = []
    for feat in data.get("features") or []:
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") != "LineString" or len(coords) < 2:
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
            name="OSM pěšiny",
            objects_xml="\n".join(objects),
            count=len(objects),
        )
    ]
