"""Vlastní render obrázku OpenStreetMap (``references/osm.png``) z dat OSM (Petr 2026-11-16).

Dlaždice OSM France / OSMF / terrestris se do ZIPu dávat nesmí (tile policy, jen weby bez zisku, vodoznak),
proto se obrázek kreslí sám z dat Overpass: využití území, voda, budovy, silnice, pěšiny, železnice a názvy
ulic, barvy podle stylu OSM Carto (CC0), písmo DejaVu Sans (volná licence, v image kvůli Mapperu). Výsledek je
odvozené dílo podle ODbL – stačí citace „© OpenStreetMap contributors“ (je i v rohu obrázku).

Kreslí se rovnou do mřížky šablony jobu (S-JTSK, v zahraničí souřadnice země) po vodorovných pruzích, PNG se
zapisuje proudově, takže paměť nezávisí na velikosti obrázku. Bez dat z Overpass nebo bez ``pycairo`` obrázek
nevznikne (log) a job pokračuje.
"""

from __future__ import annotations

import json
import math
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Velké výřezy: odpověď Overpass ve městě ~2,5 MB / km²; nad strop se obrázek nekreslí (paměť, zátěž Overpass).
MAX_AREA_KM2 = 40.0
QUERY_TIMEOUT_S = 120
STRIP_ROWS = 1024
FONT = "DejaVu Sans"
ATTRIBUTION = "© OpenStreetMap contributors"

BACKGROUND = "#f2efe9"
# (podmínka na tagy, výplň, obrys) v pořadí kreslení – barvy OSM Carto
AREA_STYLES: tuple[tuple, ...] = (
    (lambda t: t.get("landuse") == "residential", "#e0dfdf", None),
    (lambda t: t.get("landuse") == "commercial", "#f2dad9", None),
    (lambda t: t.get("landuse") == "retail", "#ffd6d1", None),
    (lambda t: t.get("landuse") in ("industrial", "railway", "construction", "garages"), "#ebdbe8", None),
    (lambda t: t.get("landuse") == "farmland", "#eef0d5", None),
    (lambda t: t.get("landuse") in ("meadow", "grass", "village_green", "recreation_ground")
     or t.get("natural") == "grassland", "#cdebb0", None),
    (lambda t: t.get("natural") == "heath", "#d6d99f", None),
    (lambda t: t.get("landuse") in ("orchard", "vineyard", "plant_nursery"), "#aedfa3", None),
    (lambda t: t.get("landuse") == "allotments", "#c9e1bf", None),
    (lambda t: t.get("landuse") == "cemetery" or t.get("amenity") == "grave_yard", "#aacbaf", None),
    (lambda t: t.get("leisure") in ("park", "garden"), "#c8facc", None),
    (lambda t: t.get("natural") == "scrub", "#c8d7ab", None),
    (lambda t: t.get("landuse") == "forest" or t.get("natural") == "wood", "#9fca8c", None),
    (lambda t: t.get("natural") in ("bare_rock", "scree", "sand", "beach"), "#eee5dc", None),
    (lambda t: t.get("amenity") in ("school", "hospital", "university", "college", "kindergarten"), "#ffffe5",
     "#d4caa8"),
    (lambda t: t.get("leisure") in ("pitch", "track", "stadium", "sports_centre"), "#aae0cb", "#88b9a5"),
    (lambda t: t.get("leisure") == "playground", "#dffce2", "#a5d8ab"),
    (lambda t: t.get("amenity") == "parking", "#eeeeee", "#d6d6d6"),
    (lambda t: t.get("natural") == "wetland", "#d6e8e0", None),
    (lambda t: t.get("natural") == "water" or t.get("landuse") in ("reservoir", "basin")
     or t.get("waterway") == "riverbank" or t.get("leisure") == "swimming_pool", "#aad3df", None),
    (lambda t: t.get("highway") in ("pedestrian", "footway", "service", "living_street")
     and t.get("area") == "yes", "#dddde8", "#bbbbbb"),
    (lambda t: t.get("man_made") == "bridge", "#b8b8b8", None),
)
# silnice: (třídy, šířka v terénu m, výplň, lem m, barva lemu); šířky ×0,75 kvůli čitelnosti jako na dlaždicích z17
ROAD_STYLES: tuple[tuple, ...] = (
    (("motorway", "motorway_link"), 14.0, "#e892a2", 1.5, "#dc2a67"),
    (("trunk", "trunk_link"), 13.0, "#f9b29c", 1.5, "#c84e2f"),
    (("primary", "primary_link"), 12.0, "#fbcb84", 1.4, "#a06b00"),
    (("secondary", "secondary_link"), 11.0, "#f4f29a", 1.3, "#707d05"),
    (("tertiary", "tertiary_link"), 10.0, "#ffffff", 1.2, "#8f8f8f"),
    (("residential", "unclassified", "living_street", "road"), 8.0, "#ffffff", 1.1, "#bbbbbb"),
    (("pedestrian",), 7.0, "#dddde8", 1.0, "#999999"),
    (("service",), 4.5, "#ffffff", 0.9, "#bbbbbb"),
)
ROAD_SCALE = 0.75
# pěšiny: (šířka m, barva, čárka m, mezera m, bílý podklad)
PATH_STYLES = {
    "footway": (1.8, "#fa8072", 3.0, 2.5, True),
    "path": (1.6, "#fa8072", 4.0, 3.0, False),
    "cycleway": (1.8, "#0000ff", 4.0, 2.5, True),
    "bridleway": (1.6, "#008000", 4.0, 3.0, False),
    "track": (2.4, "#996600", 6.0, 3.0, False),
    "steps": (3.5, "#fa8072", 0.8, 0.8, True),
}
WATERWAY_WIDTH_M = {"river": 10.0, "canal": 8.0, "stream": 2.2, "ditch": 1.5, "drain": 1.5}
RAIL_KINDS = ("rail", "tram", "light_rail", "narrow_gauge")
BARRIERS = ("wall", "city_wall", "fence", "hedge")


def overpass_ql(south: float, west: float, north: float, east: float) -> str:
    b = f"{south:.6f},{west:.6f},{north:.6f},{east:.6f}"
    return f"""[out:json][timeout:{QUERY_TIMEOUT_S}];
(
 way["building"]({b}); relation["building"]["type"="multipolygon"]({b});
 way["highway"]({b});
 way["railway"~"^({"|".join(RAIL_KINDS)})$"]({b});
 way["landuse"]({b}); relation["landuse"]["type"="multipolygon"]({b});
 way["natural"~"^(wood|water|scrub|grassland|heath|wetland|bare_rock|scree|sand|beach)$"]({b});
 relation["natural"~"^(wood|water|scrub|grassland|heath|wetland)$"]["type"="multipolygon"]({b});
 way["leisure"~"^(park|garden|pitch|playground|sports_centre|track|stadium|swimming_pool)$"]({b});
 relation["leisure"~"^(park|garden|pitch)$"]["type"="multipolygon"]({b});
 way["amenity"~"^(parking|school|hospital|university|college|kindergarten|grave_yard)$"]({b});
 way["waterway"~"^(river|canal|stream|ditch|drain|riverbank)$"]({b});
 way["barrier"~"^({"|".join(BARRIERS)})$"]({b});
 way["man_made"="bridge"]({b});
);
out geom qt;"""


def fetch_osm_elements(bbox_wgs84: tuple[float, float, float, float], *, log=None) -> list[dict] | None:
    """Prvky OSM pro render (``out geom``) ze zrcadel Overpass; None = data nejsou."""
    from app.pipeline.fetch_openzu import USER_AGENT
    from app.pipeline.osm_paths import _mirror_result, overpass_mirrors

    west, south, east, north = bbox_wgs84
    body = urllib.parse.urlencode({"data": overpass_ql(south, west, north, east)}).encode("utf-8")
    last: Exception | None = None
    for url in overpass_mirrors():
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=QUERY_TIMEOUT_S + 30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                raise OSError("neplatná odpověď (není JSON objekt)")
            remark = str(data.get("remark") or "")
            if "runtime error" in remark.lower():
                raise OSError(remark[:120])
            _mirror_result(url, True)
            return [e for e in (data.get("elements") or []) if isinstance(e, dict)]
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            _mirror_result(url, False)
            last = exc
    if log:
        log(f"OpenStreetMap: data pro obrázek se nepodařilo stáhnout ({last})")
    return None


@dataclass
class _Grid:
    xmin: float
    ymax: float
    mpp_x: float
    mpp_y: float
    width: int
    height: int
    transformer: object = field(repr=False)

    @property
    def px_per_m(self) -> float:
        return 1.0 / self.mpp_x

    def project(self, lonlat: list[tuple[float, float]]) -> np.ndarray:
        arr = np.asarray(lonlat, dtype=float)
        x, y = self.transformer.transform(arr[:, 0], arr[:, 1])
        return np.column_stack(((np.asarray(x) - self.xmin) / self.mpp_x, (self.ymax - np.asarray(y)) / self.mpp_y))


def _way_coords(el: dict) -> list[tuple[float, float]]:
    return [(p["lon"], p["lat"]) for p in el.get("geometry") or () if p]


def _relation_rings(el: dict) -> list[list[tuple[float, float]]]:
    """Kruhy multipolygonu (vnější i vnitřní, kreslí se sudo-liché) z členů relace."""
    from shapely.geometry import LineString
    from shapely.ops import linemerge, polygonize

    lines = []
    for m in el.get("members") or ():
        coords = [(p["lon"], p["lat"]) for p in m.get("geometry") or () if p]
        if len(coords) >= 2:
            lines.append(LineString(coords))
    if not lines:
        return []
    rings = []
    for poly in polygonize(linemerge(lines)):
        rings.append(list(poly.exterior.coords))
    return rings


def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255


def _layer(tags: dict) -> int:
    try:
        return int(tags.get("layer", 0))
    except (TypeError, ValueError):
        return 0


@dataclass
class _Scene:
    """Geometrie v pixelech šablony, připravená jednou a kreslená do každého pruhu."""

    areas: list = field(default_factory=list)  # (výplň, obrys, [kruhy])
    waterways: list = field(default_factory=list)  # (šířka px, body)
    buildings: list = field(default_factory=list)  # [kruhy]
    barriers: list = field(default_factory=list)  # (barva, šířka px, body)
    paths: list = field(default_factory=list)  # (styl, sidewalk, body)
    rails: list = field(default_factory=list)  # (druh, body)
    roads: list = field(default_factory=list)  # (index stylu, tunel, body)
    labels: list = field(default_factory=list)  # (x, y, úhel, text)


def build_scene(elements: list[dict], grid: _Grid) -> _Scene:
    scene = _Scene()
    style_areas: list[list] = [[] for _ in AREA_STYLES]
    lines = []
    for el in elements:
        tags = el.get("tags") or {}
        kind = el.get("type")
        if kind == "relation":
            rings_ll = _relation_rings(el)
        elif kind == "way":
            coords = _way_coords(el)
            if len(coords) < 2:
                continue
            closed = len(coords) > 3 and coords[0] == coords[-1]
            rings_ll = [coords] if closed else []
            if not closed or tags.get("highway") or tags.get("barrier") or tags.get("railway") or (
                tags.get("waterway") and tags.get("waterway") != "riverbank"
            ):
                lines.append((tags, grid.project(coords)))
        else:
            continue
        if not rings_ll:
            continue
        if tags.get("building") and tags.get("building") != "no":
            if tags.get("location") != "underground" and _layer(tags) >= 0:
                scene.buildings.append([grid.project(r) for r in rings_ll])
            continue
        if tags.get("highway") and tags.get("area") != "yes":
            continue
        for i, (test, _fill, _outline) in enumerate(AREA_STYLES):
            if test(tags):
                style_areas[i].append([grid.project(r) for r in rings_ll])
                break
    for (_test, fill, outline), rings_list in zip(AREA_STYLES, style_areas):
        for rings in rings_list:
            scene.areas.append((fill, outline, rings))
    ppm = grid.px_per_m
    lines.sort(key=lambda item: _layer(item[0]))
    named: dict[str, list] = {}
    for tags, pts in lines:
        hw, ww, rw, bar = tags.get("highway"), tags.get("waterway"), tags.get("railway"), tags.get("barrier")
        if ww in WATERWAY_WIDTH_M and tags.get("tunnel") not in ("yes", "culvert"):
            scene.waterways.append((max(1.2, WATERWAY_WIDTH_M[ww] * ppm), pts))
        elif bar in BARRIERS and not tags.get("building"):
            hedge = bar == "hedge"
            scene.barriers.append(("#aed1a0" if hedge else "#888888", max(0.8, (2.5 if hedge else 0.6) * ppm), pts))
        elif rw in RAIL_KINDS and not hw:
            if tags.get("tunnel") in ("yes", "building_passage") or _layer(tags) < 0:
                continue
            scene.rails.append((rw, pts))
        elif hw in PATH_STYLES:
            scene.paths.append((PATH_STYLES[hw], tags.get("footway") in ("sidewalk", "crossing"), pts))
        elif hw:
            idx = next((i for i, s in enumerate(ROAD_STYLES) if hw in s[0]), None)
            if idx is None:
                continue
            scene.roads.append((idx, tags.get("tunnel") in ("yes", "building_passage"), pts))
            if tags.get("name"):
                named.setdefault(str(tags["name"]), []).append(pts)
    scene.labels = _place_labels(named, grid)
    return scene


def _place_labels(named: dict[str, list], grid: _Grid) -> list[tuple[float, float, float, str]]:
    """Názvy ulic na nejdelších rovných úsecích, bez překryvu (obdélníky v pixelech)."""
    try:
        import cairo
    except ImportError:
        return []
    from shapely.geometry import LineString
    from shapely.ops import linemerge

    surf = cairo.ImageSurface(cairo.FORMAT_RGB24, 1, 1)
    ctx = cairo.Context(surf)
    ctx.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    ctx.set_font_size(_font_size(grid))
    placed: list[tuple[float, float, float, float]] = []
    out = []
    for name in sorted(named, key=lambda n: -sum(len(p) for p in named[n])):
        tw = ctx.text_extents(name).x_advance
        merged = linemerge([LineString(p) for p in named[name] if len(p) >= 2])
        parts = list(getattr(merged, "geoms", [merged]))
        cands = []
        for part in parts:
            if part.length < tw + 20:
                continue
            co = list(part.simplify(3).coords)
            for a, b in zip(co, co[1:]):
                seg = math.dist(a, b)
                if seg > tw + 10:
                    cands.append((seg, a, b))
        cands.sort(reverse=True)
        count, limit = 0, 1 + int(cands[0][0] // 600) if cands else 0
        for seg, a, b in cands:
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            if not (tw / 2 < mx < grid.width - tw / 2 and 10 < my < grid.height - 10):
                continue
            ang = math.atan2(b[1] - a[1], b[0] - a[0])
            if ang > math.pi / 2:
                ang -= math.pi
            elif ang < -math.pi / 2:
                ang += math.pi
            r = tw / 2 + 2
            dx, dy = abs(math.cos(ang)) * r + 6, abs(math.sin(ang)) * r + 6
            box = (mx - dx, my - dy, mx + dx, my + dy)
            if any(not (box[2] < p[0] or box[0] > p[2] or box[3] < p[1] or box[1] > p[3]) for p in placed):
                continue
            placed.append(box)
            out.append((mx, my, ang, name))
            count += 1
            if count >= limit:
                break
    return out


def _font_size(grid: _Grid) -> float:
    # 11 px při ~0,77 m/px (dlaždice z17); jemnější mřížka mírně větší, hrubá menší
    return float(min(14.0, max(8.0, 11.0 * 0.77 / grid.mpp_x * 0.6 + 11.0 * 0.4)))


def _draw(ctx, scene: _Scene, grid: _Grid, y0: int, y1: int) -> None:
    import cairo

    ppm = grid.px_per_m
    lo, hi = y0 - 64, y1 + 64

    def visible(pts: np.ndarray) -> bool:
        return pts[:, 1].max() >= lo and pts[:, 1].min() <= hi

    def path(pts: np.ndarray, close: bool = False) -> None:
        ctx.move_to(*pts[0])
        for x, y in pts[1:]:
            ctx.line_to(x, y)
        if close:
            ctx.close_path()

    def color(hex_color: str, alpha: float = 1.0) -> None:
        ctx.set_source_rgba(*_rgb(hex_color), alpha)

    color(BACKGROUND)
    ctx.paint()
    ctx.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    outline_w = max(1.0, 0.6 * ppm)
    for fill, outline, rings in scene.areas:
        if not any(visible(r) for r in rings):
            continue
        for r in rings:
            path(r, close=True)
        color(fill)
        if outline:
            ctx.fill_preserve()
            color(outline)
            ctx.set_line_width(outline_w)
            ctx.stroke()
        else:
            ctx.fill()
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    for width, pts in scene.waterways:
        if visible(pts):
            path(pts)
            color("#aad3df")
            ctx.set_line_width(width)
            ctx.stroke()
    for rings in scene.buildings:
        if not any(visible(r) for r in rings):
            continue
        for r in rings:
            path(r, close=True)
        color("#d9d0c9")
        ctx.fill_preserve()
        color("#c4b6ab")
        ctx.set_line_width(max(0.8, 0.5 * ppm))
        ctx.stroke()
    for col, width, pts in scene.barriers:
        if visible(pts):
            path(pts)
            color(col)
            ctx.set_line_width(width)
            ctx.stroke()
    ctx.set_line_cap(cairo.LINE_CAP_BUTT)
    for (wd, col, dash, gap, under), sidewalk, pts in scene.paths:
        if not visible(pts):
            continue
        lw = max(1.0, wd * ppm * 0.6)
        if sidewalk:
            lw, under = max(0.8, lw * 0.6), False
        if under:
            path(pts)
            color("#ffffff", 0.6)
            ctx.set_line_width(lw + 2)
            ctx.set_dash([])
            ctx.stroke()
        path(pts)
        color(col)
        ctx.set_line_width(lw)
        ctx.set_dash([max(2.0, dash * ppm), max(1.5, gap * ppm)])
        ctx.stroke()
    ctx.set_dash([])
    for kind, pts in scene.rails:
        if not visible(pts):
            continue
        lw = max(1.5, (2.5 if kind == "rail" else 1.6) * ppm)
        path(pts)
        color("#707070")
        ctx.set_line_width(lw)
        ctx.stroke()
        if kind == "rail":
            path(pts)
            color("#ffffff")
            ctx.set_line_width(max(0.8, lw * 0.45))
            ctx.set_dash([max(3.0, 8 * ppm), max(3.0, 8 * ppm)])
            ctx.stroke()
            ctx.set_dash([])
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    order = sorted(range(len(ROAD_STYLES)), reverse=True)  # od nejnižší třídy, dálnice navrch
    for stage in ("casing", "fill"):
        for idx in order:
            _classes, wd, fill, cw, ccol = ROAD_STYLES[idx]
            for ridx, tunnel, pts in scene.roads:
                if ridx != idx or not visible(pts):
                    continue
                path(pts)
                if stage == "casing":
                    color(ccol, 0.5 if tunnel else 1.0)
                    ctx.set_line_width(max(2.0, (wd + 2 * cw) * ppm * ROAD_SCALE))
                    if tunnel:
                        ctx.set_dash([max(2.0, 4 * ppm), max(2.0, 3 * ppm)])
                    ctx.stroke()
                    ctx.set_dash([])
                else:
                    color(fill, 0.6 if tunnel else 1.0)
                    ctx.set_line_width(max(1.0, wd * ppm * ROAD_SCALE))
                    ctx.stroke()
    ctx.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    fs = _font_size(grid)
    ctx.set_font_size(fs)
    for x, y, ang, text in scene.labels:
        if not (lo <= y <= hi):
            continue
        tw = ctx.text_extents(text).x_advance
        ctx.save()
        ctx.translate(x, y)
        ctx.rotate(ang)
        ctx.move_to(-tw / 2, fs * 0.35)
        ctx.text_path(text)
        color("#ffffff", 0.9)
        ctx.set_line_width(2.5)
        ctx.stroke_preserve()
        color("#333333")
        ctx.fill()
        ctx.restore()
    # citace ODbL v pravém dolním rohu
    ctx.set_font_size(max(8.0, fs - 1))
    ext = ctx.text_extents(ATTRIBUTION)
    ax, ay = grid.width - ext.x_advance - 6, grid.height - 6
    if lo <= ay <= hi:
        ctx.rectangle(ax - 3, ay - ext.height - 3, ext.x_advance + 6, ext.height + 6)
        color("#ffffff", 0.75)
        ctx.fill()
        ctx.move_to(ax, ay)
        color("#333333")
        ctx.show_text(ATTRIBUTION)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def render_png(scene: _Scene, grid: _Grid, dest_png: Path, *, strip_rows: int = STRIP_ROWS) -> None:
    """Kreslí po pruzích a zapisuje PNG proudově (RGB 8 bit) – paměť jen na jeden pruh."""
    import cairo

    tmp = dest_png.with_name(dest_png.name + ".part")
    comp = zlib.compressobj(6)
    with open(tmp, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(_png_chunk(b"IHDR", struct.pack(">IIBBBBB", grid.width, grid.height, 8, 2, 0, 0, 0)))
        for y0 in range(0, grid.height, strip_rows):
            rows = min(strip_rows, grid.height - y0)
            surf = cairo.ImageSurface(cairo.FORMAT_RGB24, grid.width, rows)
            ctx = cairo.Context(surf)
            ctx.translate(0, -y0)
            _draw(ctx, scene, grid, y0, y0 + rows)
            surf.flush()
            buf = np.ndarray((rows, surf.get_stride() // 4, 4), dtype=np.uint8, buffer=surf.get_data())
            # cairo RGB24 = BGRX na little-endian
            rgb = buf[:, : grid.width, 2::-1]
            raw = np.empty((rows, grid.width * 3 + 1), dtype=np.uint8)
            raw[:, 0] = 0
            raw[:, 1:] = rgb.reshape(rows, grid.width * 3)
            data = comp.compress(raw.tobytes())
            if data:
                fh.write(_png_chunk(b"IDAT", data))
            del surf, ctx, buf
        fh.write(_png_chunk(b"IDAT", comp.flush()))
        fh.write(_png_chunk(b"IEND", b""))
    tmp.replace(dest_png)


def template_bbox_wgs84(
    xmin: float, ymin: float, xmax: float, ymax: float, fallback: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """Obálka extentu šablony v WGS84 (rohy a středy hran) – v S-JTSK je šablona v lon/lat pootočená
    a výřez jobu by rohy obrázku nepokryl. Nepovedený převod = ``fallback`` (bbox jobu)."""
    from pyproj import Transformer

    from app.pipeline.crs_5514 import CRS_PROJ4

    try:
        tr = Transformer.from_crs(CRS_PROJ4, "EPSG:4326", always_xy=True)
        xs = [xmin, (xmin + xmax) / 2, xmax]
        ys = [ymin, (ymin + ymax) / 2, ymax]
        lon, lat = tr.transform([x for x in xs for _y in ys], [y for _x in xs for y in ys])
        lon, lat = np.asarray(lon, dtype=float), np.asarray(lat, dtype=float)
        if not (np.isfinite(lon).all() and np.isfinite(lat).all()):
            return fallback
        pad = 0.0002  # ~20 m, ať čáry a názvy u okraje nekončí uvnitř obrázku
        return float(lon.min() - pad), float(lat.min() - pad), float(lon.max() + pad), float(lat.max() + pad)
    except Exception:
        return fallback


def render_osm_png(
    bbox_wgs84: tuple[float, float, float, float],
    template_png: Path,
    template_pgw: Path,
    dest_png: Path,
    dest_pgw: Path,
    *,
    log=None,
    elements: list[dict] | None = None,
) -> bool:
    """Nakreslí ``osm.png`` + world file v mřížce šablony jobu (velikost jako dřív stažený obrázek OSM)."""
    try:
        import cairo  # noqa: F401
    except ImportError:
        if log:
            log("OpenStreetMap: obrázek se nekreslí (chybí knihovna pycairo)")
        return False
    from pyproj import Transformer

    from app.pipeline.crs_5514 import CRS_PROJ4
    from app.pipeline.reference_layers import _osm_target_size, _template_extent, _write_pgw_for_extent

    xmin, ymin, xmax, ymax, _w, _h = _template_extent(template_png, template_pgw)
    area_km2 = abs(xmax - xmin) * abs(ymax - ymin) / 1e6
    if area_km2 > MAX_AREA_KM2:
        if log:
            log(f"OpenStreetMap: obrázek se nekreslí (výřez {area_km2:.0f} km² nad {MAX_AREA_KM2:.0f} km²)")
        return False
    tw, th, mpp = _osm_target_size(template_png, template_pgw)
    t0 = time.monotonic()
    if elements is None:
        elements = fetch_osm_elements(template_bbox_wgs84(xmin, ymin, xmax, ymax, bbox_wgs84), log=log)
    if not elements:
        if elements is not None and log:
            log("OpenStreetMap: ve výřezu nejsou data pro obrázek")
        return False
    t_fetch = time.monotonic() - t0
    grid = _Grid(
        xmin=xmin, ymax=ymax, mpp_x=abs(xmax - xmin) / tw, mpp_y=abs(ymax - ymin) / th, width=tw, height=th,
        transformer=Transformer.from_crs("EPSG:4326", CRS_PROJ4, always_xy=True),
    )
    scene = build_scene(elements, grid)
    del elements
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    render_png(scene, grid, dest_png)
    _write_pgw_for_extent(dest_pgw, xmin, ymin, xmax, ymax, tw, th)
    if log:
        log(
            f"OpenStreetMap: vlastní render {tw}×{th} px (~{mpp:.2f} m/px), data {t_fetch:.0f} s, "
            f"kresba {time.monotonic() - t0 - t_fetch:.1f} s"
        )
    return True
