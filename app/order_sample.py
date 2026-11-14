"""Ukázkový výřez hotové mapy pro objednávku placené domény (4 × 4 cm papíru).

Z ``.omap`` v hotovém ZIPu se vyřízne čtverec 40 × 40 mm mapy (les 1:10 000 = 400 m, sprint
1:4 000 = 160 m): body a texty uvnitř zůstanou, čáry a plochy se ořežou na hranu čtverce (křivky
se před ořezem převedou na lomené čáry). Šablony (referenční PNG) se zahodí, symboly a georeference
zůstanou, takže výřez sedí na místě a jde otevřít v OpenOrienteering Mapperu.

Místo je náhodné, ale pro stejné území vždy stejné (seed z výřezu a presetu), a z hustší části mapy:
z několika náhodných kandidátů vyhraje ten s nejvíc vrcholy kresby.
"""

from __future__ import annotations

import hashlib
import io
import logging
import random
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

logger = logging.getLogger("podkladarna.order_sample")

OMAP_NS = "http://openorienteering.org/apps/mapper/xml/v2"
SAMPLE_SIDE_UM = 40_000  # 40 mm papíru v jednotkách OOM (µm)
CANDIDATES = 24
SAMPLE_ZIP_NAME = "sample-4x4cm.zip"

_FLAG_CURVE = 1
_FLAG_CLOSE = 2
_FLAG_HOLE = 16
_RING_END = _FLAG_CLOSE | _FLAG_HOLE  # konec prstence plochy (oom_import._area_object_with_holes)
_BEZIER_STEPS = 8

_SYMBOL_AREA = 4
_SYMBOL_COMBINED = 16


def _q(tag: str) -> str:
    return f"{{{OMAP_NS}}}{tag}"


def sample_seed(options: dict[str, Any], preset_id: str = "") -> str:
    """Stejné území a preset → stejné místo ukázky."""
    bbox = options.get("bbox_wgs84") or []
    key = "|".join(
        [",".join(f"{float(v):.5f}" for v in bbox), str(preset_id), str(options.get("map_scale") or "")]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# --- Souřadnice OOM ------------------------------------------------------------------------------


def _parse_coords(text: str) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    for item in (text or "").strip().strip(";").split(";"):
        parts = item.split()
        if len(parts) >= 2:
            out.append((int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0))
    return out


def _split_parts(coords: list[tuple[int, int, int]]) -> list[list[tuple[int, int, int]]]:
    parts: list[list[tuple[int, int, int]]] = []
    cur: list[tuple[int, int, int]] = []
    for c in coords:
        cur.append(c)
        if c[2] & _FLAG_HOLE:
            parts.append(cur)
            cur = []
    if cur:
        parts.append(cur)
    return parts


def _flatten(part: list[tuple[int, int, int]]) -> list[tuple[float, float]]:
    """Kubické Bézierovy úseky (flag 1 + 2 řídicí body) → lomená čára."""
    pts: list[tuple[float, float]] = []
    i, n = 0, len(part)
    while i < n:
        x, y, flags = part[i]
        if not pts or pts[-1] != (x, y):
            pts.append((float(x), float(y)))
        if flags & _FLAG_CURVE and i + 3 < n:
            p0, c1, c2, p3 = part[i], part[i + 1], part[i + 2], part[i + 3]
            for step in range(1, _BEZIER_STEPS + 1):
                t = step / _BEZIER_STEPS
                u = 1 - t
                bx = u**3 * p0[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t**3 * p3[0]
                by = u**3 * p0[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t**3 * p3[1]
                pts.append((bx, by))
            i += 3
            continue
        i += 1
    return pts


def _is_closed(part: list[tuple[int, int, int]]) -> bool:
    return bool(part) and (bool(part[-1][2] & _FLAG_CLOSE) or (len(part) > 2 and part[0][:2] == part[-1][:2]))


def _fmt_ring(ring: list[tuple[float, float]]) -> list[str]:
    pts = [(int(round(x)), int(round(y))) for x, y in ring]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    out = [f"{x} {y}" for x, y in pts]
    out.append(f"{pts[0][0]} {pts[0][1]} {_RING_END}")
    return out


def _fmt_line(line: list[tuple[float, float]]) -> list[str]:
    return [f"{int(round(x))} {int(round(y))}" for x, y in line]


# --- Výběr místa ---------------------------------------------------------------------------------


def _object_vertices(objects_el: ET.Element) -> list[tuple[int, int]]:
    pts: list[tuple[int, int]] = []
    for obj in objects_el.iter(_q("object")):
        coords = obj.find(_q("coords"))
        if coords is not None and coords.text:
            pts.extend((x, y) for x, y, _f in _parse_coords(coords.text))
    return pts


def choose_square(
    vertices: list[tuple[int, int]], seed: str, side: int = SAMPLE_SIDE_UM
) -> tuple[int, int, int, int] | None:
    """(minx, miny, maxx, maxy) čtverce ukázky v jednotkách mapy; None = prázdná mapa."""
    if not vertices:
        return None
    import numpy as np

    arr = np.asarray(vertices, dtype=np.float64)
    lo = np.percentile(arr, 1, axis=0)
    hi = np.percentile(arr, 99, axis=0)
    half = side / 2
    rng = random.Random(seed)

    def axis_centers(a: float, b: float) -> tuple[float, float]:
        return (a + half, b - half) if b - a > side else ((a + b) / 2, (a + b) / 2)

    (x0, x1), (y0, y1) = axis_centers(lo[0], hi[0]), axis_centers(lo[1], hi[1])
    best: tuple[int, float, float] | None = None
    for _ in range(CANDIDATES):
        cx, cy = rng.uniform(x0, x1), rng.uniform(y0, y1)
        inside = (np.abs(arr[:, 0] - cx) <= half) & (np.abs(arr[:, 1] - cy) <= half)
        score = int(inside.sum())
        if best is None or score > best[0]:
            best = (score, cx, cy)
    assert best is not None
    _score, cx, cy = best
    return (int(cx - half), int(cy - half), int(cx + half), int(cy + half))


# --- Ořez ----------------------------------------------------------------------------------------


def _symbol_types(root: ET.Element) -> dict[str, int]:
    out: dict[str, int] = {}
    symbols = root.find(f".//{_q('symbols')}")
    if symbols is None:
        return out
    for sym in symbols.findall(_q("symbol")):
        try:
            out[str(sym.get("id"))] = int(sym.get("type") or 0)
        except ValueError:
            continue
    return out


def _clip_path(coords_el: ET.Element, box, as_area: bool) -> list[list[str]]:
    from shapely.geometry import LineString, Polygon
    from shapely.validation import make_valid

    parts = _split_parts(_parse_coords(coords_el.text or ""))
    if not parts:
        return []
    out: list[list[str]] = []
    if as_area:
        rings = [_flatten(p) for p in parts]
        rings = [r for r in rings if len(r) >= 3]
        if not rings:
            return []
        geom = make_valid(Polygon(rings[0], rings[1:])).intersection(box)
        polys = [g for g in getattr(geom, "geoms", [geom]) if g.geom_type == "Polygon" and not g.is_empty]
        for poly in polys:
            pts = _fmt_ring(list(poly.exterior.coords))
            for hole in poly.interiors:
                if len(hole.coords) >= 4:
                    pts.extend(_fmt_ring(list(hole.coords)))
            out.append(pts)
        return out
    for part in parts:
        line = _flatten(part)
        if len(line) < 2:
            continue
        geom = LineString(line).intersection(box)
        for g in getattr(geom, "geoms", [geom]):
            if g.geom_type == "LineString" and not g.is_empty and len(g.coords) >= 2:
                out.append(_fmt_line(list(g.coords)))
    return out


def crop_omap(xml_bytes: bytes, seed: str, *, side: int = SAMPLE_SIDE_UM) -> bytes | None:
    """Vyřízne ukázkový čtverec z celé mapy; None = mapa bez objektů."""
    from shapely.geometry import box as shapely_box

    ET.register_namespace("", OMAP_NS)
    root = ET.fromstring(xml_bytes)
    parts_el = root.find(f".//{_q('parts')}")
    if parts_el is None:
        return None
    vertices: list[tuple[int, int]] = []
    for objects_el in parts_el.iter(_q("objects")):
        vertices.extend(_object_vertices(objects_el))
    square = choose_square(vertices, seed, side)
    if square is None:
        return None
    minx, miny, maxx, maxy = square
    box = shapely_box(minx, miny, maxx, maxy)
    types = _symbol_types(root)

    def inside(x: int, y: int) -> bool:
        return minx <= x <= maxx and miny <= y <= maxy

    for objects_el in parts_el.iter(_q("objects")):
        kept: list[ET.Element] = []
        for obj in list(objects_el.findall(_q("object"))):
            coords_el = obj.find(_q("coords"))
            coords = _parse_coords(coords_el.text or "") if coords_el is not None else []
            if not coords:
                continue
            if obj.get("type") != "1":  # bod, text: celý uvnitř, nebo pryč
                if inside(coords[0][0], coords[0][1]):
                    kept.append(obj)
                continue
            if all(inside(x, y) for x, y, _f in coords):
                kept.append(obj)
                continue
            sym_type = types.get(str(obj.get("symbol")), 0)
            parts = _split_parts(coords)
            as_area = sym_type == _SYMBOL_AREA or (
                sym_type == _SYMBOL_COMBINED and all(_is_closed(p) for p in parts)
            )
            try:
                pieces = _clip_path(coords_el, box, as_area)
            except Exception:  # noqa: BLE001 – vadný objekt ukázku nezastaví
                logger.debug("Sample clip failed for object", exc_info=True)
                continue
            for pts in pieces:
                piece = ET.fromstring(ET.tostring(obj))
                piece_coords = piece.find(_q("coords"))
                piece_coords.text = ";".join(pts) + ";"
                piece_coords.set("count", str(len(pts)))
                kept.append(piece)
        for child in list(objects_el):
            objects_el.remove(child)
        objects_el.extend(kept)
        objects_el.set("count", str(len(kept)))
        if not kept:
            objects_el.text = None
    # Šablony (referenční PNG z celého ZIPu) do ukázky nepatří.
    for templates in root.iter(_q("templates")):
        for child in list(templates):
            if child.tag == _q("template"):
                templates.remove(child)
        templates.set("count", "0")
        if "first_front_template" in templates.attrib:
            templates.set("first_front_template", "0")
    notes = root.find(_q("notes"))
    if notes is not None:
        notes.text = "Podkladárna – ukázka 4 × 4 cm / sample 4 × 4 cm"
    buf = io.BytesIO()
    ET.ElementTree(root).write(buf, encoding="UTF-8", xml_declaration=True)
    return buf.getvalue()


# --- Ze ZIPu jobu --------------------------------------------------------------------------------


def _read_omap_bytes(data: bytes) -> bytes:
    """Obsah .omap ze ZIPu (prostý XML, případně gzip / zlib jako ``oom_preview._read_omap_bytes``)."""
    if data.startswith(b"\x1f\x8b"):
        import gzip

        return gzip.decompress(data)
    stripped = data.lstrip()
    if stripped.startswith(b"<"):
        return data
    import zlib

    try:
        return zlib.decompress(data[4:])
    except zlib.error:
        return zlib.decompress(data)


def _pick_omap_name(names: list[str]) -> str | None:
    omaps = sorted(n for n in names if n.lower().endswith(".omap") and "/." not in n)
    if not omaps:
        return None
    sprint = [n for n in omaps if "sprint" in Path(n).name.lower()]
    return (sprint or omaps)[0]


def build_sample_zip(output_zip: Path, dest: Path, seed: str) -> Path | None:
    """Z hotového ZIPu jobu udělá malý ZIP s ukázkou (``sample-<mapa>.omap``)."""
    try:
        with zipfile.ZipFile(output_zip) as zf:
            name = _pick_omap_name(zf.namelist())
            if name is None:
                return None
            xml = _read_omap_bytes(zf.read(name))
        cropped = crop_omap(xml, seed)
        if cropped is None:
            return None
        stem = re.sub(r"[^\w.-]+", "-", Path(name).stem, flags=re.UNICODE).strip("-") or "mapa"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
            out.writestr(f"sample-{stem}.omap", cropped)
        tmp.replace(dest)
        return dest
    except Exception:  # noqa: BLE001 – ukázka je doplněk, objednávku nezastaví
        logger.exception("Order sample failed for %s", output_zip)
        return None
