"""Webový náhled PNG z hotového ``.omap`` (bez-KP).

Výchozí cesta je čistě Python (Pillow + XML) – bez instalace OpenOrienteering
Mapperu, vhodné i pro Docker. Stock Mapper 0.9.6 nemá headless export
(``main()`` jen otevře okno); volitelný CLI export jen když je nastavené
``PODKLADARNA_MAPPER_EXPORT`` (vlastní build / jiný exportér).

Vestavěný render čte objekty z XML (souřadnice 1/1000 mm) a kreslí
plochy/linie/body barvami symbolů – ne plná symbolika Mapperu.

Orientace: OOM mapové souřadnice už mají ``scale(s, −s)``
(``projected_to_map_coord`` → geografický sever = nižší map Y). PNG proto
dává nižší map Y nahoru (jako pohled v Mapperu / Qt), ne další převrácení Y.

Webový náhled je **srovnání bez deklinace**: grivace z georef se při
kreslení odrotuje (objekty v ``.omap`` ji mají zapečenou), takže sever
sítě/mapy sedí rovně jako v Mapperu bez magnetického natočení. Deklinace
se do PNG neaplikuje.

Ořez: výchozí výřez je fialový AOI rám (ISOM/ISSprOM 708, MTBO 705) –
přesahy cest za rám zůstanou v Mapperu, webový náhled je ořízne.

ČÚZK WMS reference se tu nemění. Zapnuto jen když ``use_kp`` je false,
pokud job nepošle ``oom_preview`` nebo env ``PODKLADARNA_OOM_PREVIEW``.
"""

from __future__ import annotations

import math
import os
import shlex
import shutil
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

# MapCoord::Flag – hodnoty jsou součást formátu .omap (neměnit).
_CURVE = 1 << 0
_CLOSE = 1 << 1
_HOLE = 1 << 4

_KIND_RANK = {"area": 0, "line": 1, "point": 2}


def oom_preview_enabled(options: dict | None) -> bool:
    """Default zapnuto jen na bez-KP. Explicitní volba / env má přednost."""
    options = options or {}
    if "oom_preview" in options and options["oom_preview"] is not None:
        return bool(options["oom_preview"])
    env = os.environ.get("PODKLADARNA_OOM_PREVIEW", "").strip().lower()
    if env in _FALSE:
        return False
    if env in _TRUE:
        return True
    return not bool(options.get("use_kp", True))


def find_mapper_exe() -> Path | None:
    """Nainstalovaný Mapper, pokud na stroji je. Nespouští ho."""
    for key in ("PODKLADARNA_MAPPER", "MAPPER"):
        raw = os.environ.get(key, "").strip().strip('"')
        if raw and Path(raw).is_file():
            return Path(raw)
    for name in ("Mapper", "Mapper.exe", "oomapper", "oomapper.exe"):
        found = shutil.which(name)
        if found:
            return Path(found)
    if os.name != "nt":
        return None
    roots = [
        os.environ.get("ProgramFiles") or r"C:\Program Files",
        os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)",
    ]
    found_paths: list[Path] = []
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for folder in base.glob("OpenOrienteering Mapper*"):
            exe = folder / "Mapper.exe"
            if exe.is_file():
                found_paths.append(exe)
    if not found_paths:
        return None
    return sorted(found_paths, key=lambda p: p.parent.name)[-1]


def _argv_from_template(template: str, **values: str) -> list[str]:
    """Rozdělí příkaz; ``{placeholders}`` zůstanou jeden argument i se mezerami."""
    tokens: dict[str, str] = {}
    line = template
    for key, value in values.items():
        token = f"__PODKLADARNA_{key.upper()}__"
        tokens[token] = value
        line = line.replace("{" + key + "}", token)
    parts = shlex.split(line, posix=True)
    return [tokens.get(part, part) for part in parts]


def mapper_export_argv(omap: Path, png: Path) -> list[str] | None:
    """Argv headless exportu, jen když je ``PODKLADARNA_MAPPER_EXPORT``.

    Šablona: ``"{mapper}" --export "{png}" "{omap}"``.
    Stock Mapper 0.9.x ten přepínač nemá – bez šablony se nespouští (otevřel by GUI).
    """
    template = os.environ.get("PODKLADARNA_MAPPER_EXPORT", "").strip()
    if not template:
        return None
    mapper = find_mapper_exe()
    return _argv_from_template(
        template,
        mapper=str(mapper or ""),
        omap=str(omap),
        png=str(png),
    )


def _is_png(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 24:
        return False
    return path.read_bytes()[:8] == _PNG_MAGIC


def _read_omap_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if data.startswith(b"\x1f\x8b"):
        import gzip

        return gzip.decompress(data)
    stripped = data.lstrip()
    if stripped.startswith(b"<") or stripped.startswith(b"<?xml"):
        return data
    try:
        return zlib.decompress(data[4:])
    except zlib.error:
        return zlib.decompress(data)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _unit_channel(raw: str) -> int:
    value = float(raw)
    if value > 1.0:
        value = value / 255.0
    return max(0, min(255, int(round(value * 255))))


def _cmyk_rgb(c: float, m: float, y: float, k: float) -> tuple[int, int, int]:
    return (
        _unit_channel(str((1.0 - c) * (1.0 - k))),
        _unit_channel(str((1.0 - m) * (1.0 - k))),
        _unit_channel(str((1.0 - y) * (1.0 - k))),
    )


def _color_rgb(el: ET.Element) -> tuple[int, int, int]:
    for child in el:
        if _local(child.tag) == "rgb" and "r" in child.attrib:
            return (
                _unit_channel(child.attrib["r"]),
                _unit_channel(child.attrib.get("g", "0")),
                _unit_channel(child.attrib.get("b", "0")),
            )
    return _cmyk_rgb(
        float(el.attrib.get("c", "0") or 0),
        float(el.attrib.get("m", "0") or 0),
        float(el.attrib.get("y", "0") or 0),
        float(el.attrib.get("k", "0") or 0),
    )


@dataclass
class _SymbolStyle:
    kind: str
    color: int
    width: float
    radius: float
    dashed: bool = False
    dash_length: float = 0.0
    break_length: float = 0.0
    border_color: int = -1
    border_width: float = 0.0
    mid_mode: str = ""  # "", "tick", "dot"
    segment_length: float = 0.0
    tick_length: float = 0.0


@dataclass
class _DrawOp:
    kind: str
    color: int
    rgb: tuple[int, int, int]
    width: float
    radius: float
    paths: list[list[tuple[float, float]]]
    dashed: bool = False
    dash_length: float = 0.0
    break_length: float = 0.0
    border_rgb: tuple[int, int, int] | None = None
    border_width: float = 0.0
    mid_mode: str = ""
    segment_length: float = 0.0
    tick_length: float = 0.0
    tick_rgb: tuple[int, int, int] | None = None


def _parse_coord_text(text: str | None) -> list[tuple[float, float, int]]:
    if not text:
        return []
    out: list[tuple[float, float, int]] = []
    for chunk in text.split(";"):
        parts = chunk.split()
        if len(parts) < 2:
            continue
        flags = int(float(parts[2])) if len(parts) > 2 else 0
        out.append((float(parts[0]), float(parts[1]), flags))
    return out


def _cubic(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    steps: int = 6,
) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for step in range(steps + 1):
        t = step / steps
        u = 1.0 - t
        x = (
            u * u * u * p0[0]
            + 3 * u * u * t * p1[0]
            + 3 * u * t * t * p2[0]
            + t * t * t * p3[0]
        )
        y = (
            u * u * u * p0[1]
            + 3 * u * u * t * p1[1]
            + 3 * u * t * t * p2[1]
            + t * t * t * p3[1]
        )
        pts.append((x, y))
    return pts


def _split_paths(
    coords: list[tuple[float, float, int]],
) -> list[list[tuple[float, float]]]:
    """Rozdělí cestu na úseky. Konec úseku = ClosePoint nebo HolePoint (flag 18)."""
    paths: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    index = 0
    count = len(coords)
    while index < count:
        x, y, flags = coords[index]
        if flags & _CURVE and index + 3 < count:
            samples = _cubic(
                (x, y),
                coords[index + 1][:2],
                coords[index + 2][:2],
                coords[index + 3][:2],
            )
            if current:
                samples = samples[1:]
            current.extend(samples)
            end_flags = coords[index + 3][2]
            index += 3
            if end_flags & (_HOLE | _CLOSE) and not (end_flags & _CURVE):
                if len(current) >= 2:
                    paths.append(current)
                current = []
                index += 1
            continue
        current.append((x, y))
        index += 1
        if flags & (_HOLE | _CLOSE):
            if len(current) >= 2:
                paths.append(current)
            current = []
    if len(current) >= 2:
        paths.append(current)
    return paths


def _direct_child(el: ET.Element, name: str) -> ET.Element | None:
    for child in el:
        if _local(child.tag) == name:
            return child
    return None


def _positive_color(el: ET.Element) -> int:
    for node in el.iter():
        for key in ("inner_color", "color", "outer_color"):
            raw = node.attrib.get(key)
            if raw is None:
                continue
            try:
                value = int(raw)
            except ValueError:
                continue
            if value >= 0:
                return value
    return -1


def _attr_float(el: ET.Element, key: str, default: float = 0.0) -> float:
    try:
        return float(el.attrib.get(key, str(default)) or default)
    except ValueError:
        return default


def _line_mid_mode(line_el: ET.Element) -> tuple[str, float]:
    """Detekce fousů / teček na linii (srázy, zdi, hranice vegetace)."""
    tick_len = 0.0
    has_tick = False
    has_dot = False
    for node in line_el.iter():
        tag = _local(node.tag)
        if tag not in {"start_symbol", "mid_symbol", "end_symbol"}:
            continue
        for child in node.iter():
            if _local(child.tag) == "line_symbol":
                has_tick = True
                tick_len = max(tick_len, _attr_float(child, "line_width", 0.0) * 4.0)
            elif _local(child.tag) == "point_symbol":
                has_dot = True
                tick_len = max(tick_len, _attr_float(child, "inner_radius", 0.0))
    if has_tick:
        return "tick", max(tick_len, 500.0)
    if has_dot:
        return "dot", max(tick_len, 250.0)
    return "", 0.0


def _style_from_area(target: ET.Element) -> _SymbolStyle | None:
    color = _positive_color(target)
    if color < 0:
        return None
    return _SymbolStyle("area", color, 0.0, 0.0)


def _style_from_line(target: ET.Element) -> _SymbolStyle | None:
    color = int(target.attrib.get("color", "-1") or -1)
    if color < 0:
        color = _positive_color(target)
    width = _attr_float(target, "line_width", 0.0)
    dashed = (target.attrib.get("dashed", "false") or "false").lower() == "true"
    dash_length = _attr_float(target, "dash_length", 0.0)
    break_length = _attr_float(target, "break_length", 0.0)
    border_color = -1
    border_width = 0.0
    borders = _direct_child(target, "borders")
    if borders is not None:
        for border in borders:
            if _local(border.tag) != "border":
                continue
            try:
                border_color = int(border.attrib.get("color", "-1") or -1)
            except ValueError:
                border_color = -1
            border_width = _attr_float(border, "width", 0.0)
            shift = _attr_float(border, "shift", 0.0)
            # Dvě souběžné linky ≈ středová šířka + 2*shift.
            if shift > 0:
                width = max(width, 2.0 * shift + border_width)
            break
    mid_mode, tick_length = _line_mid_mode(target)
    segment_length = _attr_float(target, "segment_length", 0.0)
    if color < 0 and mid_mode:
        color = _positive_color(target)
    if color < 0 and border_color >= 0:
        color = border_color
    if color < 0 and width <= 0 and not mid_mode:
        return None
    if color < 0:
        return None
    if width <= 0 and mid_mode:
        width = max(border_width, 120.0)
    return _SymbolStyle(
        "line",
        color,
        width,
        0.0,
        dashed=dashed,
        dash_length=dash_length,
        break_length=break_length,
        border_color=border_color,
        border_width=border_width,
        mid_mode=mid_mode,
        segment_length=segment_length,
        tick_length=tick_length,
    )


def _style_from_point(target: ET.Element) -> _SymbolStyle | None:
    color = _positive_color(target)
    if color < 0:
        return None
    radius = _attr_float(target, "inner_radius", 0.0)
    if radius <= 0:
        radius = 600.0
    return _SymbolStyle("point", color, 0.0, radius)


def _styles_from_symbol_el(sym: ET.Element) -> list[_SymbolStyle]:
    """Jedna nebo více vrstev stylu (combined = více částí)."""
    kind_attr = sym.attrib.get("type", "")
    area = _direct_child(sym, "area_symbol")
    line = _direct_child(sym, "line_symbol")
    point = _direct_child(sym, "point_symbol")
    if area is not None or kind_attr == "4":
        style = _style_from_area(area if area is not None else sym)
        return [style] if style else []
    if line is not None or kind_attr == "2":
        style = _style_from_line(line if line is not None else sym)
        return [style] if style else []
    if point is not None or kind_attr == "1":
        style = _style_from_point(point if point is not None else sym)
        return [style] if style else []
    if kind_attr != "16":
        return []
    out: list[_SymbolStyle] = []
    combined = _direct_child(sym, "combined_symbol")
    parts_parent = combined if combined is not None else sym
    for part in parts_parent:
        if _local(part.tag) != "part":
            continue
        nested = _direct_child(part, "symbol")
        if nested is not None:
            out.extend(_styles_from_symbol_el(nested))
            continue
        raw = part.attrib.get("symbol")
        if raw is None:
            continue
        # Referenční part – vyřeší se později přes id.
        try:
            out.append(_SymbolStyle("__ref__", int(raw), 0.0, 0.0))
        except ValueError:
            pass
    return out


def _symbol_styles(symbols_el: ET.Element) -> dict[int, list[_SymbolStyle]]:
    styles: dict[int, list[_SymbolStyle]] = {}
    for sym in symbols_el:
        if _local(sym.tag) != "symbol":
            continue
        try:
            sym_id = int(sym.attrib["id"])
        except (KeyError, ValueError):
            continue
        layers = _styles_from_symbol_el(sym)
        if layers:
            styles[sym_id] = layers
    # Dohraj referenční části combined symbolů.
    for sym_id, layers in list(styles.items()):
        resolved: list[_SymbolStyle] = []
        for layer in layers:
            if layer.kind == "__ref__":
                ref_layers = styles.get(layer.color)
                if ref_layers:
                    resolved.extend(ref_layers)
            else:
                resolved.append(layer)
        styles[sym_id] = resolved
    return styles


def _colors(root: ET.Element) -> dict[int, tuple[int, int, int]]:
    found: dict[int, tuple[int, int, int]] = {}
    for el in root.iter():
        if _local(el.tag) != "colors":
            continue
        for index, color in enumerate(child for child in el if _local(child.tag) == "color"):
            try:
                priority = int(color.attrib.get("priority", str(index)))
            except ValueError:
                priority = index
            found[priority] = _color_rgb(color)
        break
    return found


def _current_part(root: ET.Element) -> ET.Element | None:
    for el in root.iter():
        if _local(el.tag) != "parts":
            continue
        parts = [child for child in el if _local(child.tag) == "part"]
        if not parts:
            return None
        try:
            current = int(el.attrib.get("current", "0"))
        except ValueError:
            current = 0
        return parts[min(max(current, 0), len(parts) - 1)]
    return None


# Fialový AOI obdélník z package_oom.build_aoi_boundary_part.
_AOI_FRAME_CODES = frozenset({"708", "705"})


def _symbol_ids_for_codes(root: ET.Element, codes: frozenset[str]) -> set[int]:
    found: set[int] = set()
    for el in root.iter():
        if _local(el.tag) != "symbols":
            continue
        for sym in el:
            if _local(sym.tag) != "symbol":
                continue
            if sym.attrib.get("code") not in codes:
                continue
            try:
                found.add(int(sym.attrib["id"]))
            except (KeyError, ValueError):
                continue
        break
    return found


def _path_bbox(
    path: list[tuple[float, float]],
) -> tuple[float, float, float, float] | None:
    if len(path) < 2:
        return None
    xs = [p[0] for p in path]
    ys = [p[1] for p in path]
    return min(xs), min(ys), max(xs), max(ys)


def aoi_frame_bbox(
    root: ET.Element,
    *,
    grivation_deg: float | None = None,
) -> tuple[float, float, float, float] | None:
    """BBox fialového AOI rámu (708 / 705) – největší uzavřená cesta těchto symbolů."""
    if grivation_deg is None:
        grivation_deg = map_grivation_deg(root)
    symbol_ids = _symbol_ids_for_codes(root, _AOI_FRAME_CODES)
    if not symbol_ids:
        return None
    part = _current_part(root)
    if part is None:
        return None
    best: tuple[float, tuple[float, float, float, float]] | None = None
    for objects in part:
        if _local(objects.tag) != "objects":
            continue
        for obj in objects:
            if _local(obj.tag) != "object":
                continue
            try:
                sym_id = int(obj.attrib.get("symbol", "-1"))
            except ValueError:
                continue
            if sym_id not in symbol_ids:
                continue
            coords_el = _direct_child(obj, "coords")
            coords = _parse_coord_text(coords_el.text if coords_el is not None else None)
            for path in _split_paths(coords):
                path = undo_grivation_path(path, grivation_deg)
                box = _path_bbox(path)
                if box is None:
                    continue
                minx, miny, maxx, maxy = box
                area = max(maxx - minx, 0.0) * max(maxy - miny, 0.0)
                if area <= 0:
                    continue
                if best is None or area > best[0]:
                    best = (area, box)
    return best[1] if best else None


def map_grivation_deg(root: ET.Element) -> float:
    """Grivace z ``<georeferencing>`` – pro webový náhled ji odrotujeme."""
    for el in root.iter():
        if _local(el.tag) != "georeferencing":
            continue
        try:
            return float(el.attrib.get("grivation", "0") or 0)
        except ValueError:
            return 0.0
    return 0.0


def undo_grivation_xy(x: float, y: float, grivation_deg: float) -> tuple[float, float]:
    """Mapové souřadnice → srovnání bez deklinace (odrotovat +grivation).

    ``projected_to_map_coord`` točí o +grivation před ``scale(s, −s)``. Inverze
    při zachování konvence (sever = nižší Y):
    ``x' = x cos g − y sin g``, ``y' = x sin g + y cos g``.
    """
    if abs(grivation_deg) < 1e-9:
        return x, y
    g = math.radians(grivation_deg)
    cos_g = math.cos(g)
    sin_g = math.sin(g)
    return x * cos_g - y * sin_g, x * sin_g + y * cos_g


def undo_grivation_path(
    path: list[tuple[float, float]], grivation_deg: float
) -> list[tuple[float, float]]:
    if abs(grivation_deg) < 1e-9:
        return path
    return [undo_grivation_xy(x, y, grivation_deg) for x, y in path]


def _ops_bbox(ops: list[_DrawOp]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for op in ops:
        for path in op.paths:
            for x, y in path:
                xs.append(x)
                ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def _op_from_style(
    style: _SymbolStyle,
    colors: dict[int, tuple[int, int, int]],
    paths: list[list[tuple[float, float]]],
) -> _DrawOp | None:
    if style.kind not in _KIND_RANK or style.color < 0:
        return None
    rgb = colors.get(style.color)
    if rgb is None:
        return None
    border_rgb = colors.get(style.border_color) if style.border_color >= 0 else None
    tick_rgb = rgb
    return _DrawOp(
        style.kind,
        style.color,
        rgb,
        style.width,
        style.radius,
        paths,
        dashed=style.dashed,
        dash_length=style.dash_length,
        break_length=style.break_length,
        border_rgb=border_rgb,
        border_width=style.border_width,
        mid_mode=style.mid_mode,
        segment_length=style.segment_length,
        tick_length=style.tick_length,
        tick_rgb=tick_rgb,
    )


def _collect_ops(root: ET.Element, *, grivation_deg: float | None = None) -> list[_DrawOp]:
    if grivation_deg is None:
        grivation_deg = map_grivation_deg(root)
    colors = _colors(root)
    styles: dict[int, list[_SymbolStyle]] = {}
    for el in root.iter():
        if _local(el.tag) == "symbols":
            styles = _symbol_styles(el)
            break
    part = _current_part(root)
    if part is None:
        return []
    ops: list[_DrawOp] = []
    for objects in part:
        if _local(objects.tag) != "objects":
            continue
        for obj in objects:
            if _local(obj.tag) != "object":
                continue
            try:
                sym_id = int(obj.attrib.get("symbol", "-1"))
            except ValueError:
                continue
            layers = styles.get(sym_id) or []
            if not layers:
                continue
            coords_el = _direct_child(obj, "coords")
            coords = _parse_coord_text(coords_el.text if coords_el is not None else None)
            for style in layers:
                if style.kind == "point":
                    if not coords:
                        continue
                    pt = undo_grivation_xy(coords[0][0], coords[0][1], grivation_deg)
                    op = _op_from_style(style, colors, [[pt]])
                    if op is not None:
                        ops.append(op)
                    continue
                paths = [
                    undo_grivation_path(path, grivation_deg)
                    for path in _split_paths(coords)
                ]
                paths = [path for path in paths if len(path) >= 2]
                if not paths:
                    continue
                op = _op_from_style(style, colors, paths)
                if op is not None:
                    ops.append(op)
    ops.sort(key=lambda op: (-op.color, _KIND_RANK[op.kind]))
    return ops


def _path_length(path: list[tuple[float, float]]) -> float:
    total = 0.0
    for (x0, y0), (x1, y1) in zip(path, path[1:]):
        total += math.hypot(x1 - x0, y1 - y0)
    return total


def _point_at_length(
    path: list[tuple[float, float]], distance: float
) -> tuple[float, float, float, float] | None:
    """Bod a jednotkový směr na cestě v dané vzdálenosti od začátku."""
    if len(path) < 2 or distance < 0:
        return None
    remaining = distance
    for (x0, y0), (x1, y1) in zip(path, path[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg <= 1e-9:
            continue
        if remaining <= seg:
            t = remaining / seg
            dx, dy = (x1 - x0) / seg, (y1 - y0) / seg
            return x0 + t * (x1 - x0), y0 + t * (y1 - y0), dx, dy
        remaining -= seg
    x0, y0 = path[-2]
    x1, y1 = path[-1]
    seg = math.hypot(x1 - x0, y1 - y0)
    if seg <= 1e-9:
        return None
    return x1, y1, (x1 - x0) / seg, (y1 - y0) / seg


def _dashed_polylines(
    path: list[tuple[float, float]],
    dash_length: float,
    break_length: float,
) -> list[list[tuple[float, float]]]:
    if dash_length <= 0 or break_length < 0 or len(path) < 2:
        return [path]
    total = _path_length(path)
    if total <= 0:
        return [path]
    out: list[list[tuple[float, float]]] = []
    pos = 0.0
    draw = True
    while pos < total - 1e-6:
        span = dash_length if draw else break_length
        if span <= 0:
            span = total
        end = min(pos + span, total)
        if draw:
            samples: list[tuple[float, float]] = []
            step = max(span / 8.0, 40.0)
            t = pos
            while t <= end + 1e-6:
                hit = _point_at_length(path, min(t, end))
                if hit is not None:
                    samples.append((hit[0], hit[1]))
                t += step
            hit = _point_at_length(path, end)
            if hit is not None:
                samples.append((hit[0], hit[1]))
            # unikátní body
            cleaned: list[tuple[float, float]] = []
            for pt in samples:
                if not cleaned or math.hypot(pt[0] - cleaned[-1][0], pt[1] - cleaned[-1][1]) > 1e-3:
                    cleaned.append(pt)
            if len(cleaned) >= 2:
                out.append(cleaned)
        pos = end
        draw = not draw
    return out or [path]


def _mid_marks(
    path: list[tuple[float, float]],
    *,
    spacing: float,
    tick_length: float,
    mode: str,
) -> list[tuple[float, float, float, float]]:
    """Vzorky (x, y, dx, dy) pro fousy/tečky podél linie."""
    if mode not in {"tick", "dot"} or len(path) < 2:
        return []
    total = _path_length(path)
    if total <= 0:
        return []
    step = spacing if spacing > 0 else max(tick_length * 1.5, 750.0)
    marks: list[tuple[float, float, float, float]] = []
    pos = min(step * 0.5, total * 0.5)
    while pos < total - step * 0.15:
        hit = _point_at_length(path, pos)
        if hit is not None:
            marks.append(hit)
        pos += step
    if not marks:
        hit = _point_at_length(path, total * 0.5)
        if hit is not None:
            marks.append(hit)
    return marks


def render_omap_xml_png(
    omap: Path,
    dest: Path,
    *,
    max_side: int = 1600,
) -> tuple[int, int, int]:
    """Vykreslí objekty mapy do PNG. Vrátí (šířka, výška, počet operací)."""
    from PIL import Image, ImageDraw

    root = ET.fromstring(_read_omap_bytes(Path(omap)))
    grivation = map_grivation_deg(root)
    ops = _collect_ops(root, grivation_deg=grivation)
    if not ops:
        raise ValueError(f"{omap.name}: .omap nemá vykreslitelné objekty")

    frame = aoi_frame_bbox(root, grivation_deg=grivation)
    tick_pad = max((op.tick_length for op in ops if op.mid_mode), default=0.0)
    if frame is not None:
        minx, miny, maxx, maxy = frame
        # Tenký okraj kolem fialové linky (šířka symbolu ~1 mm papíru).
        spanx = max(maxx - minx, 1.0)
        spany = max(maxy - miny, 1.0)
        padx = max(spanx * 0.015, 800.0, tick_pad)
        pady = max(spany * 0.015, 800.0, tick_pad)
    else:
        minx, miny, maxx, maxy = _ops_bbox(ops)
        spanx = max(maxx - minx, 1.0)
        spany = max(maxy - miny, 1.0)
        padx = max(spanx * 0.03, tick_pad * 1.2, 500.0)
        pady = max(spany * 0.03, tick_pad * 1.2, 500.0)
    world_w = spanx + 2 * padx
    world_h = spany + 2 * pady
    scale = max(1, int(max_side)) / max(world_w, world_h)
    width = max(1, int(round(world_w * scale)))
    height = max(1, int(round(world_h * scale)))
    # Map X → right; map Y → down in PNG. OOM already uses scale(s, −s), so
    # geographic north is lower map Y and must land at the top of the image.
    # Grivation is undone above → srovnání bez deklinace (grid/map north).
    origin_x = minx - padx
    origin_y = miny - pady

    def to_px(x: float, y: float) -> tuple[float, float]:
        return (x - origin_x) * scale, (y - origin_y) * scale

    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for op in ops:
        if op.kind == "area":
            mask = Image.new("L", (width, height), 0)
            mask_draw = ImageDraw.Draw(mask)
            for ring_index, path in enumerate(op.paths):
                pts = [to_px(x, y) for x, y in path]
                if len(pts) < 3:
                    continue
                mask_draw.polygon(pts, fill=0 if ring_index else 255)
            color_img = Image.new("RGB", (width, height), op.rgb)
            image.paste(color_img, (0, 0), mask)
        elif op.kind == "line":
            stroke = max(1, int(round(max(op.width, 1.0) * scale)))
            border_stroke = 0
            if op.border_rgb is not None and op.border_width > 0:
                border_stroke = max(
                    stroke + 2, int(round((op.width + 2 * op.border_width) * scale))
                )
            for path in op.paths:
                polylines = (
                    _dashed_polylines(path, op.dash_length, op.break_length)
                    if op.dashed
                    else [path]
                )
                for poly in polylines:
                    pts = [to_px(x, y) for x, y in poly]
                    if len(pts) < 2:
                        continue
                    if border_stroke > 0 and op.border_rgb is not None:
                        draw.line(pts, fill=op.border_rgb, width=border_stroke)
                    draw.line(pts, fill=op.rgb, width=stroke)
                if op.mid_mode:
                    tick_px = max(2.0, op.tick_length * scale)
                    mark_rgb = op.tick_rgb or op.rgb
                    for x, y, dx, dy in _mid_marks(
                        path,
                        spacing=op.segment_length,
                        tick_length=op.tick_length,
                        mode=op.mid_mode,
                    ):
                        px, py = to_px(x, y)
                        if op.mid_mode == "dot":
                            r = max(1.5, tick_px * 0.35)
                            draw.ellipse(
                                (px - r, py - r, px + r, py + r), fill=mark_rgb
                            )
                        else:
                            # Fous kolmo vlevo od směru linie (konvence OOM tagů).
                            nx, ny = -dy, dx
                            x1, y1 = px, py
                            x2, y2 = px + nx * tick_px, py + ny * tick_px
                            draw.line(
                                [(x1, y1), (x2, y2)],
                                fill=mark_rgb,
                                width=max(1, stroke // 2),
                            )
        else:
            radius = max(1.5, op.radius * scale)
            for path in op.paths:
                if not path:
                    continue
                px, py = to_px(path[0][0], path[0][1])
                draw.ellipse(
                    (px - radius, py - radius, px + radius, py + radius),
                    fill=op.rgb,
                )
    dest.parent.mkdir(parents=True, exist_ok=True)
    image.save(dest, format="PNG")
    return width, height, len(ops)


def render_omap_to_png(
    omap: Path,
    dest: Path,
    *,
    log=None,
    max_side: int = 1600,
) -> str:
    """PNG z ``.omap``. Mapper CLI jen při ``PODKLADARNA_MAPPER_EXPORT``, jinak XML."""
    omap = Path(omap)
    dest = Path(dest)
    argv = mapper_export_argv(omap, dest)
    if argv:
        timeout = float(os.environ.get("PODKLADARNA_MAPPER_TIMEOUT", "180"))
        if log:
            log("OOM náhled: spouštím Mapper export")
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            if log:
                log(f"OOM náhled: Mapper CLI selhalo ({exc}) – beru vestavěný render")
        else:
            if proc.returncode == 0 and _is_png(dest):
                if log:
                    log(f"OOM náhled: Mapper CLI → {dest.name}")
                return "mapper-cli"
            if log:
                err = (proc.stderr or b"").decode("utf-8", "replace").strip()
                log(
                    f"OOM náhled: Mapper CLI kód {proc.returncode}"
                    + (f" ({err[:240]})" if err else "")
                    + " – beru vestavěný render"
                )
            if dest.exists() and not _is_png(dest):
                dest.unlink()
    width, height, count = render_omap_xml_png(omap, dest, max_side=max_side)
    if log:
        log(
            f"OOM náhled: vestavěný render {dest.name} "
            f"({width}×{height}, {count} objektů)"
        )
    return f"xml {width}x{height} n={count}"


def pick_preview_omap(paths: list[Path]) -> Path | None:
    existing = [Path(p) for p in paths if Path(p).is_file()]
    if not existing:
        return None
    sprint = [p for p in existing if "sprint" in p.name.lower()]
    return sprint[0] if sprint else existing[0]


def write_job_oom_preview(
    omap_paths: list[Path],
    work_dir: Path,
    output_dir: Path,
    options: dict | None,
    log=None,
) -> Path | None:
    """Po zápisu ``.omap`` uloží ``work/preview.png`` a ``output/preview/oom_preview.png``."""
    if not oom_preview_enabled(options):
        return None
    src = pick_preview_omap(list(omap_paths))
    if src is None:
        if log:
            log("OOM náhled: žádný .omap")
        return None
    work_png = Path(work_dir) / "preview.png"
    summary = render_omap_to_png(src, work_png, log=log)
    named = Path(output_dir) / "preview" / "oom_preview.png"
    named.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(work_png, named)
    if log:
        log(f"OOM náhled: {src.name} → preview.png ({summary})")
    return work_png
