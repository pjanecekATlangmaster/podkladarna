"""Oprava šablon (podkladů) v ``.ocd`` z Mapper CLI convert.

Mapper ``--cli convert`` načte mapu bez pohledu a v době převodu ještě neleží podklady vedle ``.omap``
(kopírují se do ``output/references`` až po sestavení). Jeho řetězce 8 (šablony) pak mají:

- ``d100`` (průhlednost 100 %, CLI nemá ``MapView`` → ``TemplateVisibility{}`` = opacity 0),
- polohu a měřítko nenačtené šablony (x = y = 0, u = v = 1 – „roztažené mimo“),
- absolutní cestu ze serveru (relativní cestu s ``/`` Mapper do OCD nepíše).

Ruční „Uložit jako .ocd“ v Mapperu má šablony načtené, proto tam podklady sedí (tester 2026-11-11).
Tady se řetězce 8 přepíšou stejně, jak by je zapsal Mapper s načtenou šablonou: poloha z world
file (.jgw / .pgw) a rozměru obrázku, georeference z ``.omap``, viditelnost a průhlednost z pohledu
``.omap``, cesta relativní ``references\\soubor``. Obrázek se nedekóduje (jen hlavička), takže
ani velké dlaždice ortofota nezvednou paměť.
"""

from __future__ import annotations

import math
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

__all__ = ["OcdTemplateFix", "fix_ocd_templates", "ocd_template_string", "read_ocd_strings"]

_NS = "{http://openorienteering.org/apps/mapper/xml/v2}"
_STRING_TYPE_TEMPLATE = 8
_STRING_TYPE_GEOREF = 1039
# FileHeaderV9+ (OCD 9–2018): first_string_block na offsetu 32.
_FIRST_STRING_BLOCK_OFFSET = 32
_INDEX_ENTRIES = 256
_INDEX_ENTRY = struct.Struct("<IIiI")  # pos, size, type, obj_index
_INDEX_BLOCK_SIZE = 4 + _INDEX_ENTRIES * _INDEX_ENTRY.size
_WORLD_SUFFIX = {".jpg": ".jgw", ".jpeg": ".jgw", ".png": ".pgw", ".tif": ".tfw", ".tiff": ".tfw",
                 ".gif": ".gfw", ".bmp": ".bpw"}


@dataclass(frozen=True)
class _Georef:
    scale: int
    grivation_deg: float
    ref_x: float
    ref_y: float
    map_ref_x: float = 0.0
    map_ref_y: float = 0.0
    combined_scale_factor: float = 1.0

    def to_map(self, e: float, n: float) -> tuple[float, float]:
        """S-JTSK (m) → mapové souřadnice Mapperu (mm, y dolů); inverze ``Georeferencing::updateTransformation``."""
        dx, dy = e - self.ref_x, n - self.ref_y
        # to_projected = T(ref) · R(-g) · S(s, -s) · T(-map_ref)  (QTransform, řádkové vektory)
        g = math.radians(self.grivation_deg)
        s = self.combined_scale_factor * self.scale / 1000.0
        # Inverze rotace R(-g) z QTransform::rotate: x' = x·cos + y·sin, y' = -x·sin + y·cos (pro úhel -g).
        c, si = math.cos(g), math.sin(g)
        rx = dx * c - dy * si
        ry = dx * si + dy * c
        return rx / s + self.map_ref_x, -ry / s + self.map_ref_y

    def to_projected(self, x: float, y: float) -> tuple[float, float]:
        s = self.combined_scale_factor * self.scale / 1000.0
        sx, sy = (x - self.map_ref_x) * s, -(y - self.map_ref_y) * s
        g = math.radians(self.grivation_deg)
        c, si = math.cos(g), math.sin(g)
        return self.ref_x + sx * c + sy * si, self.ref_y - sx * si + sy * c


@dataclass(frozen=True)
class _OmapTemplate:
    relpath: str
    georef: bool
    visible: bool
    opacity: float


@dataclass(frozen=True)
class OcdTemplateFix:
    fixed: int
    skipped: tuple[str, ...]


def _parse_omap(omap: Path) -> tuple[_Georef, list[_OmapTemplate]]:
    root = ET.parse(omap).getroot()
    geo = root.find(f"{_NS}georeferencing")
    if geo is None:
        raise ValueError(f"{omap.name}: chybí georeferencing")
    combined = float(geo.get("grid_scale_factor") or geo.get("combined_scale_factor") or 1.0)
    proj_ref = geo.find(f"{_NS}projected_crs/{_NS}ref_point")
    if proj_ref is None:
        raise ValueError(f"{omap.name}: chybí ref_point S-JTSK")
    map_ref = geo.find(f"{_NS}ref_point")
    georef = _Georef(
        scale=int(geo.get("scale")),
        grivation_deg=float(geo.get("grivation") or 0.0),
        ref_x=float(proj_ref.get("x")),
        ref_y=float(proj_ref.get("y")),
        map_ref_x=float(map_ref.get("x")) if map_ref is not None else 0.0,
        map_ref_y=float(map_ref.get("y")) if map_ref is not None else 0.0,
        combined_scale_factor=combined,
    )
    barrier = root.find(f"{_NS}barrier")
    scope = barrier if barrier is not None else root
    elems = scope.findall(f"{_NS}templates/{_NS}template")
    refs = scope.findall(f"{_NS}view/{_NS}map_view/{_NS}templates/{_NS}ref")
    vis: dict[int, tuple[bool, float]] = {}
    for ref in refs:
        try:
            vis[int(ref.get("template"))] = (
                ref.get("visible") == "true",
                float(ref.get("opacity") or 1.0),
            )
        except (TypeError, ValueError):
            continue
    templates = []
    for i, el in enumerate(elems):
        visible, opacity = vis.get(i, (False, 1.0))
        templates.append(
            _OmapTemplate(
                relpath=el.get("relpath") or el.get("path") or "",
                georef=el.get("georef") == "true",
                visible=visible,
                opacity=opacity,
            )
        )
    return georef, templates


def _image_size(path: Path) -> tuple[int, int]:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None  # jen hlavička, nic se nedekóduje
    with Image.open(path) as im:
        return im.size


def _read_world(path: Path) -> tuple[float, float, float, float, float, float]:
    vals = [float(v) for v in path.read_text(encoding="ascii").split()[:6]]
    if len(vals) != 6:
        raise ValueError(f"{path.name}: neplatný world file")
    return tuple(vals)  # type: ignore[return-value]


def _template_transform(
    georef: _Georef, world: tuple[float, ...], width: int, height: int
) -> tuple[float, float, float, float, float]:
    """(x_mm, y_mm, rotace_rad, scale_x, scale_y) jako ``TemplateImage::updatePosFromGeoreferencing``."""
    a, d, b, e, c, f = world  # A D B E C F (pořadí v souboru: A, D, B, E, C, F)
    # Střed levého horního pixelu → jeho roh (WorldFile::operator QTransform).
    c0 = c - (a + b) / 2
    f0 = f - (d + e) / 2

    def pix_to_map(px: float, py: float) -> tuple[float, float]:
        return georef.to_map(c0 + a * px + b * py, f0 + d * px + e * py)

    tl = pix_to_map(0.0, 0.0)
    tr = pix_to_map(float(width), 0.0)
    bl = pix_to_map(0.0, float(height))
    # Afinní obraz centrovaných pixelů: map = A·src + t (sloupcově), QTransform má m11=A00, m12=A10, m21=A01, m22=A11.
    a00 = (tr[0] - tl[0]) / width
    a10 = (tr[1] - tl[1]) / width
    a01 = (bl[0] - tl[0]) / height
    a11 = (bl[1] - tl[1]) / height
    tx = tl[0] + 0.5 * width * a00 + 0.5 * height * a01
    ty = tl[1] + 0.5 * width * a10 + 0.5 * height * a11
    m11, m12, m21, m22 = a00, a10, a01, a11
    rotation = math.atan2(m21 - m12, m11 + m22)
    cr, sr = math.cos(rotation), math.sin(rotation)
    # scaling = qt * rotateRadians(rotation); rotate: [[c, s], [-s, c]] v řádkové konvenci Qt.
    s11 = m11 * cr - m12 * sr
    s22 = m21 * sr + m22 * cr
    return tx, ty, rotation, s11, s22


def ocd_template_string(
    path: str,
    *,
    visible: bool,
    opacity: float,
    x_mm: float,
    y_mm: float,
    rotation_rad: float,
    scale_x: float,
    scale_y: float,
) -> str:
    """Řetězec 8 pro OCD ≥ 11 ve stejném tvaru jako ``OcdFileExport::stringForTemplate``."""
    d = max(0, min(100, round(100 - 100 * opacity)))
    ab = math.degrees(rotation_rad)
    return (
        f"{path}\ts{'1' if visible else '0'}\tr1"
        f"\tu{scale_x:.10g}\tv{scale_y:.10g}"
        f"\tx{x_mm:.6f}\ty{y_mm:.6f}"
        f"\ta{ab:.8g}\tb{ab:.8g}\td{d}"
    )


def _string_entries(data: bytes | bytearray) -> list[tuple[int, int, int, int]]:
    """(pozice záznamu indexu, pos, size, type) všech parametrických řetězců."""
    if len(data) < _FIRST_STRING_BLOCK_OFFSET + 4 or data[:2] != b"\xad\x0c":
        raise ValueError("není soubor OCD")
    version = struct.unpack_from("<H", data, 4)[0]
    if version < 9:
        raise ValueError(f"OCD verze {version} není podporovaná")
    block = struct.unpack_from("<I", data, _FIRST_STRING_BLOCK_OFFSET)[0]
    out = []
    seen = set()
    while block and block not in seen and block + _INDEX_BLOCK_SIZE <= len(data):
        seen.add(block)
        for i in range(_INDEX_ENTRIES):
            at = block + 4 + i * _INDEX_ENTRY.size
            pos, size, stype, _obj = _INDEX_ENTRY.unpack_from(data, at)
            if pos:
                out.append((at, pos, size, stype))
        block = struct.unpack_from("<I", data, block)[0]
    return out


def _decode(data: bytes | bytearray, pos: int, size: int) -> str:
    raw = bytes(data[pos : pos + size]).split(b"\0", 1)[0]
    return raw.decode("utf-8", errors="replace")


def read_ocd_strings(ocd: Path, string_type: int) -> list[str]:
    data = Path(ocd).read_bytes()
    return [_decode(data, pos, size) for _at, pos, size, st in _string_entries(data) if st == string_type]


def _params(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in s.split("\t")[1:]:
        if part:
            out.setdefault(part[0], part[1:])
    return out


def _area_offset(georef: _Georef, georef_string: str | None) -> tuple[float, float]:
    """Posun kreslicí plochy OCD (``OcdFileExport::calculateAreaOffset``) zjištěný z řetězce 1039.

    Bez posunu je počátek OCD = mapový počátek zaokrouhlený na celé metry. Mapper posouvá jen mapy
    mimo ±2 m papíru a to na celé stovky metrů; rozdíl > 1 m proto znamená posun.
    """
    if not georef_string:
        return 0.0, 0.0
    p = _params(georef_string)
    try:
        ox, oy = float(p["x"]), float(p["y"])
    except (KeyError, ValueError):
        return 0.0, 0.0
    base_e, base_n = georef.to_projected(0.0, 0.0)
    if abs(ox - base_e) <= 1.0 and abs(oy - base_n) <= 1.0:
        return 0.0, 0.0
    return georef.to_map(ox, oy)


def _find_image(name: str, omap_dir: Path, relpath: str, search_dirs: list[Path]) -> Path | None:
    for cand in (*(d / name for d in search_dirs), omap_dir / relpath):
        if cand.is_file():
            return cand
    return None


def fix_ocd_templates(
    ocd: Path,
    omap: Path,
    *,
    reference_dirs: list[Path] | tuple[Path, ...] = (),
    log=None,
) -> OcdTemplateFix:
    """Přepíše řetězce 8 v ``ocd`` podle šablon ``omap``. Vrací počet opravených a přeskočené soubory."""
    ocd, omap = Path(ocd), Path(omap)
    georef, templates = _parse_omap(omap)
    by_name = {Path(t.relpath.replace("\\", "/")).name.lower(): t for t in templates if t.relpath}
    data = bytearray(ocd.read_bytes())
    entries = _string_entries(data)
    georef_str = next(
        (_decode(data, pos, size) for _at, pos, size, st in entries if st == _STRING_TYPE_GEOREF),
        None,
    )
    off_x, off_y = _area_offset(georef, georef_str)
    search = [Path(d) for d in reference_dirs if d]
    fixed = 0
    skipped: list[str] = []
    for at, pos, size, stype in entries:
        if stype != _STRING_TYPE_TEMPLATE:
            continue
        old = _decode(data, pos, size)
        old_path = old.split("\t", 1)[0]
        name = old_path.replace("\\", "/").rsplit("/", 1)[-1]
        tmpl = by_name.get(name.lower())
        if tmpl is None:
            skipped.append(name)
            continue
        rel = tmpl.relpath.replace("/", "\\")
        if tmpl.georef:
            img = _find_image(name, omap.parent, tmpl.relpath, search)
            world = img.with_suffix(_WORLD_SUFFIX.get(img.suffix.lower(), ".wld")) if img else None
            if img is None or world is None or not world.is_file():
                skipped.append(name)
                continue
            try:
                w, h = _image_size(img)
                tx, ty, rot, sx, sy = _template_transform(georef, _read_world(world), w, h)
            except (OSError, ValueError) as exc:
                skipped.append(f"{name} ({exc})")
                continue
        else:
            # Negeoreferencovaná šablona: poloha od Mapperu zůstane, opraví se jen cesta a viditelnost.
            p = _params(old)
            try:
                tx = float(p["x"]) + off_x
                ty = -float(p["y"]) + off_y
                rot = math.radians(float(p.get("a", "0")))
                sx, sy = float(p.get("u", "1")), float(p.get("v", "1"))
            except (KeyError, ValueError):
                skipped.append(name)
                continue
        new = ocd_template_string(
            rel,
            visible=tmpl.visible,
            opacity=tmpl.opacity,
            x_mm=tx - off_x,
            y_mm=-(ty - off_y),
            rotation_rad=rot,
            scale_x=sx,
            scale_y=sy,
        )
        raw = new.encode("utf-8") + b"\0"
        if len(raw) <= size:
            data[pos : pos + size] = raw + b"\0" * (size - len(raw))
            new_pos, new_size = pos, size
        else:
            # Delší řetězec na konec souboru (zarovnání na 8 B jako Mapper), starý zůstane nepoužitý.
            data.extend(b"\0" * ((-len(data)) % 8))
            new_pos, new_size = len(data), len(raw)
            data.extend(raw)
        struct.pack_into("<II", data, at, new_pos, new_size)
        fixed += 1
    if fixed:
        tmp = ocd.with_name(ocd.name + ".tmp")
        tmp.write_bytes(bytes(data))
        tmp.replace(ocd)
    if log and (fixed or skipped):
        msg = f"OOM OCD: šablony opraveny {fixed}× ({ocd.name})"
        if skipped:
            msg += f", beze změny: {', '.join(skipped[:5])}" + (" …" if len(skipped) > 5 else "")
        log(msg)
    return OcdTemplateFix(fixed, tuple(skipped))
