"""Webový náhled PNG z hotového ``.omap`` (bez-KP).

OpenOrienteering Mapper 0.9.6 (``Mapper.exe``) nemá headless export: ``main()``
soubor jen otevře v okně a ``PrintWidget::exportToImage()`` chce dialog.
Proto:

* je-li nastavené ``PODKLADARNA_MAPPER_EXPORT``, zavolá se ten příkaz
  (nainstalovaný Mapper nebo jiný exportér);
* jinak se mapa vykreslí vestavěným náhledem z XML (souřadnice 1/1000 mm,
  barvy podle priority jako v Mapperu).

ČÚZK WMS reference se tu nemění. Zapnuto jen když ``use_kp`` je false,
pokud job nepošle ``oom_preview`` nebo env ``PODKLADARNA_OOM_PREVIEW``.
"""

from __future__ import annotations

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


@dataclass
class _DrawOp:
    kind: str
    color: int
    rgb: tuple[int, int, int]
    width: float
    radius: float
    paths: list[list[tuple[float, float]]]


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


def _symbol_styles(symbols_el: ET.Element) -> dict[int, _SymbolStyle]:
    styles: dict[int, _SymbolStyle] = {}
    combined: dict[int, int] = {}
    for sym in symbols_el:
        if _local(sym.tag) != "symbol":
            continue
        try:
            sym_id = int(sym.attrib["id"])
        except (KeyError, ValueError):
            continue
        kind_attr = sym.attrib.get("type", "")
        area = _direct_child(sym, "area_symbol")
        line = _direct_child(sym, "line_symbol")
        point = _direct_child(sym, "point_symbol")
        if area is not None or kind_attr == "4":
            target = area if area is not None else sym
            styles[sym_id] = _SymbolStyle("area", _positive_color(target), 0.0, 0.0)
        elif line is not None or kind_attr == "2":
            target = line if line is not None else sym
            width = float(target.attrib.get("line_width", "0") or 0)
            styles[sym_id] = _SymbolStyle("line", _positive_color(target), width, 0.0)
        elif point is not None or kind_attr == "1":
            target = point if point is not None else sym
            radius = float(target.attrib.get("inner_radius", "0") or 0)
            if radius <= 0:
                radius = 600.0
            styles[sym_id] = _SymbolStyle("point", _positive_color(target), 0.0, radius)
        elif kind_attr == "16":
            part = None
            for node in sym.iter():
                if _local(node.tag) == "part" and node.attrib.get("symbol"):
                    part = node
                    break
            if part is not None:
                try:
                    combined[sym_id] = int(part.attrib["symbol"])
                except ValueError:
                    pass
    for sym_id, ref in combined.items():
        style = styles.get(ref)
        if style is not None:
            styles[sym_id] = style
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


def _collect_ops(root: ET.Element) -> list[_DrawOp]:
    colors = _colors(root)
    styles: dict[int, _SymbolStyle] = {}
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
            style = styles.get(sym_id)
            if style is None or style.kind not in _KIND_RANK or style.color < 0:
                continue
            rgb = colors.get(style.color)
            if rgb is None:
                continue
            coords_el = _direct_child(obj, "coords")
            coords = _parse_coord_text(coords_el.text if coords_el is not None else None)
            if style.kind == "point":
                if not coords:
                    continue
                ops.append(
                    _DrawOp(
                        "point",
                        style.color,
                        rgb,
                        0.0,
                        style.radius,
                        [[(coords[0][0], coords[0][1])]],
                    )
                )
                continue
            paths = _split_paths(coords)
            if not paths:
                continue
            ops.append(
                _DrawOp(style.kind, style.color, rgb, style.width, style.radius, paths)
            )
    ops.sort(key=lambda op: (-op.color, _KIND_RANK[op.kind]))
    return ops


def render_omap_xml_png(
    omap: Path,
    dest: Path,
    *,
    max_side: int = 1280,
) -> tuple[int, int, int]:
    """Vykreslí objekty mapy do PNG. Vrátí (šířka, výška, počet operací)."""
    from PIL import Image, ImageDraw

    root = ET.fromstring(_read_omap_bytes(Path(omap)))
    ops = _collect_ops(root)
    if not ops:
        raise ValueError(f"{omap.name}: .omap nemá vykreslitelné objekty")

    xs: list[float] = []
    ys: list[float] = []
    for op in ops:
        for path in op.paths:
            for x, y in path:
                xs.append(x)
                ys.append(y)
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    spanx = max(maxx - minx, 1.0)
    spany = max(maxy - miny, 1.0)
    padx = spanx * 0.03
    pady = spany * 0.03
    world_w = spanx + 2 * padx
    world_h = spany + 2 * pady
    scale = max(1, int(max_side)) / max(world_w, world_h)
    width = max(1, int(round(world_w * scale)))
    height = max(1, int(round(world_h * scale)))
    origin_x = minx - padx
    origin_y = maxy + pady

    def to_px(x: float, y: float) -> tuple[float, float]:
        return (x - origin_x) * scale, (origin_y - y) * scale

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
            stroke = max(1, int(round(op.width * scale)))
            for path in op.paths:
                pts = [to_px(x, y) for x, y in path]
                if len(pts) < 2:
                    continue
                draw.line(pts, fill=op.rgb, width=stroke)
        else:
            radius = max(1.0, op.radius * scale)
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
    max_side: int = 1280,
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
