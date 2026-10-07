"""PNG/JPEG náhledy z hotového ``.omap`` (bez-KP) – dvě produktové cesty.

1. **Web (náhled jobu / klik)** – preferuje **OpenOrienteering Mapper CLI**
   @ nižší DPI (cap ≤50 Mpx IHDR před ``Image.open``; georef @ 600 se
   reuse jen když IHDR vejde) → PNG, pak **ořez AOI (708/705) + undo
   grivace** → **JPEG ≤1600**. Pillow XML fallback pod timeoutem – selhání
   / hang náhledu **neblokuje** ZIP/OCD.

2. **Georef ZIP** (PNG+PGW, volitelně GeoTIFF) – preferuje **Mapper CLI**
   @ **600 DPI** (``--full-map``), **s grivací**, přes
   ``PODKLADARNA_MAPPER`` + ``PODKLADARNA_MAPPER_EXPORT``. Bez CLI job
   spadne na Pillow georef @ 600 DPI-eq. Přímý ``engine="mapper"`` bez CLI
   vyhodí ``MapperExportError``. Georef ZIP zůstává full-map + grivace.

Materiálový OOM ZIP georef PNG **neobsahuje**, dokud uživatel nezapne
``output_georef`` (GUI checkbox, default off) – pak jdou PNG+PGW±GeoTIFF
do hlavního ZIPu i do ``podkladarna_georef_previews.zip`` / API.

Orientace: OOM mapové souřadnice už mají ``scale(s, −s)`` → nižší map Y
nahoru. Webový ořez: fialový AOI rám (708 / MTBO 705), sever sítě nahoru.

Webový náhled default zapnutý; vypnout ``oom_preview=0`` /
``PODKLADARNA_OOM_PREVIEW=0``. Georef: ``output_georef=1``.
"""

from __future__ import annotations

import math
import os
import shlex
import shutil
import signal
import subprocess
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from app.pipeline.crs_5514 import CRS_LABEL, CRS_PROJ4, CRS_WKT
from app.pipeline.georef import PgwGeoref
from app.pipeline.oom_coords import map_to_projected

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
# Disciplíny ve výstupu – stejné tagy jako omap_variant_filename.
GEOREF_PREVIEW_DIR = "preview"
GEOREF_PREVIEWS_ZIP_NAME = "podkladarna_georef_previews.zip"
# Georef Mapper export – fixní DPI (bez GUI volby).
GEOREF_MAPPER_DPI = 600
# Web náhled: Mapper @ nižší DPI (nebo downscale z georef) → JPEG.
WEB_MAPPER_DPI = 150
WEB_PREVIEW_MAX_SIDE = 1600
WEB_JPEG_QUALITY = 82
# Pillow MAX_IMAGE_PIXELS bývá ~89M–179M. Nad tím Image.open → bomb.
# Typicky reuse georef @ 600 DPI na velkém full-map (~494 Mpx u Zvokoli).
# Cap pod oběma limity + RAM (RGB load); georef @ 600 se nemění.
WEB_MAPPER_MAX_PIXELS = 50_000_000
# Celý web náhled (Mapper+crop / Pillow fallback) – po timeoutu job pokračuje
# na ZIP/OCD/mail. Ostrý hang: bomb → Pillow fallback vytuhne → ZIP nedoběhne.
WEB_PREVIEW_TIMEOUT_SEC = 120
# Rezerva na Mapper --full-map okraje oproti bbox objektů.
_WEB_MAPPER_PAPER_MARGIN = 1.2
# Pillow georef fallback: cílové DPI ≈ Mapper; OOM map. j. = 1/1000 mm papíru.
# Floor ≥ dřívější 1600 (malé AOI jinak pod 600 DPI papíru vypadají hůř než web);
# 4800 = 3× starý cap → výrazně ostřejší tip bez Mapper CLI.
GEOREF_PILLOW_MIN_SIDE_FLOOR = 4800
# Cap chrání paměť na velkých AOI (≈10k px ≈ 200 MB RGB).
GEOREF_PILLOW_MAX_SIDE_CAP = 10000
# Výchozí šablona pro PR #2523 CLI (mfbehrens/oo-mapper větev cli).
# Stock Mapper 0.9.6 tuto syntaxi neumí – bez env se nespouští.
DEFAULT_MAPPER_EXPORT_TEMPLATE = (
    '"{mapper}" --cli export --full-map -i "{omap}" -o "{png}" --dpi {dpi}'
)
# .omap → .ocd (OCD12). Bez --output-format OCD12 by registry zvolil default v9.
DEFAULT_MAPPER_CONVERT_TEMPLATE = (
    '"{mapper}" --cli convert -i "{omap}" -o "{ocd}" --output-format OCD12'
)
_OCD_MAGIC = b"\xad\x0c"


def georef_pillow_dpi() -> int:
    """Cílové DPI pro Pillow georef fallback (default = Mapper 600).

    Override: ``PODKLADARNA_GEOREF_PILLOW_DPI`` (kladné int).
    """
    raw = os.environ.get("PODKLADARNA_GEOREF_PILLOW_DPI", "").strip()
    if raw:
        try:
            dpi = int(raw)
            if dpi > 0:
                return dpi
        except ValueError:
            pass
    return GEOREF_MAPPER_DPI


def web_mapper_dpi() -> int:
    """DPI pro webový Mapper náhled (default 150). Override: ``PODKLADARNA_WEB_MAPPER_DPI``."""
    raw = os.environ.get("PODKLADARNA_WEB_MAPPER_DPI", "").strip()
    if raw:
        try:
            dpi = int(raw)
            if dpi > 0:
                return dpi
        except ValueError:
            pass
    return WEB_MAPPER_DPI


def web_mapper_max_pixels() -> int:
    """Max. pixelů zdrojového PNG pro web Mapper→Pillow. Override env."""
    raw = os.environ.get("PODKLADARNA_WEB_MAPPER_MAX_PIXELS", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return WEB_MAPPER_MAX_PIXELS


def web_preview_timeout_sec() -> float:
    """Timeout web náhledu v sekundách (0 = vypnuto). Env override."""
    raw = os.environ.get("PODKLADARNA_WEB_PREVIEW_TIMEOUT", "").strip()
    if raw:
        try:
            value = float(raw)
            if value >= 0:
                return value
        except ValueError:
            pass
    return float(WEB_PREVIEW_TIMEOUT_SEC)


def _is_pixel_bomb_error(exc: BaseException) -> bool:
    """True u Pillow decompression bomb / našeho IHDR capu."""
    msg = str(exc).lower()
    if "decompression bomb" in msg:
        return True
    if "exceeds web preview limit" in msg:
        return True
    if "exceeds limit of" in msg and "pixels" in msg:
        return True
    return False


@contextmanager
def _web_preview_deadline(seconds: float):
    """SIGALRM deadline (Linux job worker = main thread). 0 = bez limitu."""
    if seconds <= 0 or not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        yield
        return

    def _handler(signum, frame):  # noqa: ARG001
        raise TimeoutError(
            f"web preview timeout after {seconds:g}s"
        )

    previous = signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


def png_pixel_count(path: Path) -> int:
    """Šířka×výška PNG z IHDR (bez dekodování pixelů)."""
    width, height = _png_size(path)
    return int(width) * int(height)


def png_within_web_pixel_limit(
    path: Path,
    *,
    max_pixels: int | None = None,
) -> bool:
    """True, pokud IHDR pixel count ≤ limitu webového náhledu."""
    limit = web_mapper_max_pixels() if max_pixels is None else max(1, int(max_pixels))
    return png_pixel_count(path) <= limit


def assert_png_within_web_pixel_limit(
    path: Path,
    *,
    max_pixels: int | None = None,
) -> tuple[int, int]:
    """IHDR kontrola před ``Image.open`` – nikdy neotevírat stovky Mpx.

    Vrací ``(width, height)``. Nad limitem ``ValueError`` (bez ``Image.open``).
    Caller pak zkusí capped Mapper / Pillow XML ≤1600, nebo náhled přeskočí.
    """
    width, height = _png_size(path)
    limit = web_mapper_max_pixels() if max_pixels is None else max(1, int(max_pixels))
    pixels = int(width) * int(height)
    if pixels > limit:
        raise ValueError(
            f"Image size ({pixels} pixels) exceeds web preview limit of "
            f"{limit} pixels ({width}×{height})"
        )
    return width, height


def estimate_omap_fullmap_paper_inches(omap: Path) -> tuple[float, float]:
    """Odhad papíru Mapper ``--full-map`` z bbox objektů (map. j. = 1/1000 mm)."""
    root = ET.fromstring(_read_omap_bytes(Path(omap)))
    ops = _collect_ops(root, grivation_deg=0.0)
    if not ops:
        return (1.0, 1.0)
    minx, miny, maxx, maxy = _ops_bbox(ops)
    span_x = max(maxx - minx, 1.0) * _WEB_MAPPER_PAPER_MARGIN
    span_y = max(maxy - miny, 1.0) * _WEB_MAPPER_PAPER_MARGIN
    # map unit = 0.001 mm → mm = /1000 → inch = /25.4
    width_in = max(span_x / 1000.0 / 25.4, 0.01)
    height_in = max(span_y / 1000.0 / 25.4, 0.01)
    return (width_in, height_in)


def capped_web_mapper_dpi(
    omap: Path,
    *,
    desired_dpi: int | None = None,
    max_pixels: int | None = None,
) -> int:
    """Web Mapper DPI ≤ ``desired``, tak aby odhad full-map px ≤ ``max_pixels``.

    Georef @ 600 DPI se nemění – jen webová cesta, ať Pillow nedostane stovky Mpx.
    """
    dpi = (
        int(desired_dpi)
        if desired_dpi is not None and int(desired_dpi) > 0
        else web_mapper_dpi()
    )
    limit = web_mapper_max_pixels() if max_pixels is None else max(1, int(max_pixels))
    width_in, height_in = estimate_omap_fullmap_paper_inches(omap)
    area = max(width_in * height_in, 1e-9)
    estimated = (width_in * dpi) * (height_in * dpi)
    if estimated <= limit:
        return max(1, dpi)
    capped = int(math.floor(math.sqrt(limit / area)))
    return max(1, min(dpi, capped))


def write_web_preview_jpeg(
    src_png: Path,
    dest_jpg: Path,
    *,
    max_side: int = WEB_PREVIEW_MAX_SIDE,
    quality: int = WEB_JPEG_QUALITY,
) -> tuple[int, int]:
    """Downscale PNG → JPEG (RGB) pro webový náhled. Vrátí (w, h)."""
    from PIL import Image

    src_png = Path(src_png)
    dest_jpg = Path(dest_jpg)
    assert_png_within_web_pixel_limit(src_png)
    with Image.open(src_png) as im:
        rgb = im.convert("RGB")
        w, h = rgb.size
        cap = max(1, int(max_side))
        longest = max(w, h)
        if longest > cap:
            scale = cap / float(longest)
            rgb = rgb.resize(
                (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                Image.Resampling.LANCZOS,
            )
        dest_jpg.parent.mkdir(parents=True, exist_ok=True)
        q = max(40, min(95, int(quality)))
        rgb.save(dest_jpg, format="JPEG", quality=q, optimize=True)
        return rgb.size


class MapperExportError(RuntimeError):
    """Georef PNG: Mapper CLI chybí, selhal, nebo není nakonfigurovaný."""

# MapCoord::Flag – hodnoty jsou součást formátu .omap (neměnit).
_CURVE = 1 << 0
_CLOSE = 1 << 1
_HOLE = 1 << 4

_KIND_RANK = {"area": 0, "line": 1, "point": 2}


def oom_preview_enabled(options: dict | None) -> bool:
    """Webový náhled (Mapper→JPEG / Pillow fallback) – default zapnuto."""
    options = options or {}
    if "oom_preview" in options and options["oom_preview"] is not None:
        return bool(options["oom_preview"])
    env = os.environ.get("PODKLADARNA_OOM_PREVIEW", "").strip().lower()
    if env in _FALSE:
        return False
    if env in _TRUE:
        return True
    return True


def output_georef_enabled(options: dict | None = None) -> bool:
    """Georef PNG/TIFF (Mapper @ 600 / Pillow fallback) – GUI checkbox, default off."""
    options = options or {}
    if "output_georef" in options and options["output_georef"] is not None:
        return bool(options["output_georef"])
    env = os.environ.get("PODKLADARNA_OUTPUT_GEOREF", "").strip().lower()
    if env in _FALSE:
        return False
    if env in _TRUE:
        return True
    return False


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


def mapper_export_argv(
    omap: Path,
    png: Path,
    *,
    dpi: int = GEOREF_MAPPER_DPI,
) -> list[str] | None:
    """Argv headless exportu pro **georef** PNG.

    Vyžaduje ``PODKLADARNA_MAPPER_EXPORT`` (šablona s ``{mapper}``, ``{omap}``,
    ``{png}``, volitelně ``{dpi}``). Stock Mapper 0.9.x bez CLI by otevřel GUI,
    proto bez šablony vrací ``None``.

    Doporučená šablona (PR #2523):
    ``"{mapper}" --cli export --full-map -i "{omap}" -o "{png}" --dpi {dpi}``
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
        dpi=str(int(dpi)),
    )


def mapper_export_configured() -> bool:
    """True, když je nastavená export šablona a existuje Mapper binárka."""
    if not os.environ.get("PODKLADARNA_MAPPER_EXPORT", "").strip():
        return False
    mapper = find_mapper_exe()
    return mapper is not None and mapper.is_file()


def mapper_convert_argv(omap: Path, ocd: Path) -> list[str] | None:
    """Argv ``Mapper --cli convert`` → OCD12.

    Vyžaduje ``PODKLADARNA_MAPPER_CONVERT`` (šablona s ``{mapper}``, ``{omap}``,
    ``{ocd}``). Explicitní ``OCD12`` v šabloně je povinné (bez flagu default v9).
    """
    template = os.environ.get("PODKLADARNA_MAPPER_CONVERT", "").strip()
    if not template:
        return None
    mapper = find_mapper_exe()
    return _argv_from_template(
        template,
        mapper=str(mapper or ""),
        omap=str(omap),
        ocd=str(ocd),
    )


def mapper_convert_configured() -> bool:
    """True, když je nastavená convert šablona a existuje Mapper binárka."""
    if not os.environ.get("PODKLADARNA_MAPPER_CONVERT", "").strip():
        return False
    mapper = find_mapper_exe()
    return mapper is not None and mapper.is_file()


def _is_ocd(path: Path) -> bool:
    """Magic ``0x0CAD`` (little-endian ``ad 0c``) na začátku souboru."""
    if not path.is_file() or path.stat().st_size < 8:
        return False
    return path.read_bytes()[:2] == _OCD_MAGIC


def ocd_version(path: Path) -> int | None:
    """Verze OCD formátu (uint16 LE na offsetu 4), nebo None."""
    import struct

    data = Path(path).read_bytes()[:6]
    if len(data) < 6 or data[:2] != _OCD_MAGIC:
        return None
    return int(struct.unpack_from("<H", data, 4)[0])


class MapperConvertError(RuntimeError):
    """``Mapper --cli convert`` chybí, selhal, nebo není nakonfigurovaný."""


# Opakující se hlášky Qt/fontconfig/PROJ, které zakryly skutečnou chybu
# (dřív se logovalo jen prvních 400 znaků stderr = samý šum).
_MAPPER_NOISE_PREFIXES = (
    "QStandardPaths:",
    "Fontconfig error:",
    "proj_create",
    "pj_obj_create",
    "proj_identify",
)


def _mapper_error_detail(proc: subprocess.CompletedProcess, *, full: bool = False) -> str:
    """Stderr/stdout Mapperu pro log: bez šumu, s koncem výstupu (tam bývá příčina)."""
    text = "\n".join(
        part
        for part in (
            (proc.stderr or b"").decode("utf-8", "replace").strip(),
            (proc.stdout or b"").decode("utf-8", "replace").strip(),
        )
        if part
    )
    if full:
        return text
    lines = [ln for ln in text.splitlines() if ln.strip()]
    signal_lines = [ln for ln in lines if not ln.lstrip().startswith(_MAPPER_NOISE_PREFIXES)]
    noise = len(lines) - len(signal_lines)
    detail = " | ".join(signal_lines)
    if len(detail) > 600:
        detail = "…" + detail[-600:]
    if noise:
        detail = (detail + " " if detail else "") + f"(+{noise} řádků Qt/PROJ šumu)"
    return detail


def run_mapper_convert(
    omap: Path,
    dest: Path | None = None,
    *,
    log=None,
) -> Path:
    """Spustí Mapper CLI convert → ``.ocd`` (OCD12). Při chybě ``MapperConvertError``."""
    omap = Path(omap)
    dest = Path(dest) if dest is not None else omap.with_suffix(".ocd")
    argv = mapper_convert_argv(omap, dest)
    if not argv:
        raise MapperConvertError(
            "Convert .omap→.ocd vyžaduje OpenOrienteering Mapper CLI. "
            "Nastavte PODKLADARNA_MAPPER a PODKLADARNA_MAPPER_CONVERT, např. "
            f"{DEFAULT_MAPPER_CONVERT_TEMPLATE!r}."
        )
    if not find_mapper_exe():
        raise MapperConvertError(
            "PODKLADARNA_MAPPER_CONVERT je nastavené, ale Mapper binárka "
            "nebyla nalezena (PODKLADARNA_MAPPER / PATH / Program Files)."
        )
    timeout = float(os.environ.get("PODKLADARNA_MAPPER_TIMEOUT", "600"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    if log:
        log(f"OOM OCD: Mapper convert OCD12 → {dest.name}")
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise MapperConvertError(
            f"Mapper convert timeout po {timeout:.0f}s ({omap.name})"
        ) from exc
    except OSError as exc:
        raise MapperConvertError(f"Mapper convert spuštění selhalo: {exc}") from exc
    if proc.returncode != 0 or not _is_ocd(dest):
        detail = _mapper_error_detail(proc)
        if dest.exists() and not _is_ocd(dest):
            dest.unlink(missing_ok=True)
        raise MapperConvertError(
            f"Mapper convert selhal (kód {proc.returncode})"
            + (f": {detail}" if detail else "")
        )
    return dest


def convert_omap_to_ocd(omap: Path, *, log=None) -> Path | None:
    """Best-effort ``.omap`` → ``.ocd`` vedle omapu. Bez CLI / při chybě None + log."""
    omap = Path(omap)
    if not omap.is_file():
        return None
    if not mapper_convert_configured():
        if log:
            log(
                f"OOM OCD: přeskočeno ({omap.name}) – "
                "není PODKLADARNA_MAPPER_CONVERT / Mapper CLI"
            )
        return None
    try:
        return run_mapper_convert(omap, log=log)
    except MapperConvertError as exc:
        if log:
            log(f"OOM OCD: přeskočeno ({omap.name}): {exc}")
        return None


def _is_png(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 24:
        return False
    return path.read_bytes()[:8] == _PNG_MAGIC


def _png_size(path: Path) -> tuple[int, int]:
    """Šířka×výška PNG bez plného dekodování (IHDR)."""
    data = Path(path).read_bytes()
    if len(data) < 24 or data[:8] != _PNG_MAGIC:
        raise ValueError(f"{path}: není platný PNG")
    import struct

    return struct.unpack(">II", data[16:24])


def run_mapper_export(
    omap: Path,
    dest: Path,
    *,
    dpi: int = GEOREF_MAPPER_DPI,
    log=None,
    full_stderr: bool = False,
) -> None:
    """Spustí Mapper CLI → PNG. Při chybě ``MapperExportError`` (bez Pillow fallbacku).

    ``full_stderr``: celý výstup Mapperu bez filtrace šumu (smoke test buildu).
    """
    omap = Path(omap)
    dest = Path(dest)
    argv = mapper_export_argv(omap, dest, dpi=dpi)
    if not argv:
        raise MapperExportError(
            "Georef PNG vyžaduje OpenOrienteering Mapper CLI. "
            "Nastavte PODKLADARNA_MAPPER (cesta k Mapper binárce s --cli) a "
            "PODKLADARNA_MAPPER_EXPORT, např. "
            f"{DEFAULT_MAPPER_EXPORT_TEMPLATE!r}. "
            "Stock Mapper 0.9.6 nestačí (otevřel by GUI)."
        )
    if not find_mapper_exe():
        raise MapperExportError(
            "PODKLADARNA_MAPPER_EXPORT je nastavené, ale Mapper binárka "
            "nebyla nalezena (PODKLADARNA_MAPPER / PATH / Program Files)."
        )
    timeout = float(os.environ.get("PODKLADARNA_MAPPER_TIMEOUT", "600"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    env = os.environ.copy()
    # Headless Qt (Linux CLI build); na Windows offscreen často není, nevadí.
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    if log:
        log(f"OOM georef: Mapper CLI @ {dpi} DPI → {dest.name}")
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise MapperExportError(
            f"Mapper CLI timeout po {timeout:.0f}s ({omap.name})"
        ) from exc
    except OSError as exc:
        raise MapperExportError(f"Mapper CLI spuštění selhalo: {exc}") from exc
    if proc.returncode != 0 or not _is_png(dest):
        detail = _mapper_error_detail(proc, full=full_stderr)
        if dest.exists() and not _is_png(dest):
            dest.unlink(missing_ok=True)
        raise MapperExportError(
            f"Mapper CLI selhal (kód {proc.returncode})"
            + (f": {detail}" if detail else "")
        )
    if full_stderr:
        detail = _mapper_error_detail(proc, full=True)
        if detail and log:
            log(f"Mapper výstup: {detail}")


def extent_for_mapper_png(
    omap: Path,
    png: Path,
) -> tuple[PreviewExtent, OmapGeoref]:
    """Odhad mapového výřezu Mapper ``--full-map`` PNG → PGW mřížka.

    Bere bbox objektů v nativních mapových souřadnicích (s grivací) a
    roztáhne ho na skutečné rozměry PNG (čtvercové pixely).
    """
    root = ET.fromstring(_read_omap_bytes(Path(omap)))
    georef = parse_omap_georef(root)
    ops = _collect_ops(root, grivation_deg=0.0)
    if not ops:
        raise ValueError(f"{Path(omap).name}: .omap nemá objekty pro georef extent")
    minx, miny, maxx, maxy = _ops_bbox(ops)
    spanx = max(maxx - minx, 1.0)
    spany = max(maxy - miny, 1.0)
    width, height = _png_size(png)
    map_per_px = max(spanx / width, spany / height)
    world_w = width * map_per_px
    world_h = height * map_per_px
    origin_x = minx - (world_w - spanx) / 2.0
    origin_y = miny - (world_h - spany) / 2.0
    return (
        PreviewExtent(
            origin_x=origin_x,
            origin_y=origin_y,
            map_per_px=map_per_px,
            width=width,
            height=height,
        ),
        georef,
    )


def prepare_mapper_web_preview(
    omap: Path,
    mapper_png: Path,
    dest_png: Path,
    *,
    max_side: int = WEB_PREVIEW_MAX_SIDE,
) -> tuple[int, int]:
    """Mapper ``--full-map`` PNG → webový PNG: ořez AOI + zrušení deklinace.

    Mapper export drží grivaci a celý mapový bbox. Web potřebuje stejný
    výřez jako Pillow: fialový rám 708/705 a sever sítě nahoru. Affine
    warp mapuje cílový (grid-north) pixel → nativní (s grivací) → vzorek
    ze zdrojového PNG.
    """
    from PIL import Image

    omap = Path(omap)
    mapper_png = Path(mapper_png)
    dest_png = Path(dest_png)
    src_extent, georef = extent_for_mapper_png(omap, mapper_png)
    root = ET.fromstring(_read_omap_bytes(omap))
    g = float(georef.grivation_deg)
    g_undo = g  # aoi_frame_bbox / _collect_ops: odrotovat
    ops = _collect_ops(root, grivation_deg=g_undo)
    if not ops:
        raise ValueError(f"{omap.name}: .omap nemá objekty pro webový ořez")
    minx, miny, maxx, maxy, padx, pady = preview_crop_box(
        root, ops, grivation_deg=g_undo
    )
    dest_extent = preview_extent_from_crop(
        minx,
        miny,
        maxx,
        maxy,
        padx,
        pady,
        max_side=max_side,
    )
    # dest (grid-north) → src (grivated): inverze undo = R(-g)
    cos_g = math.cos(math.radians(g))
    sin_g = math.sin(math.radians(g))
    mpp_d = dest_extent.map_per_px
    mpp_s = src_extent.map_per_px
    ox_d, oy_d = dest_extent.origin_x, dest_extent.origin_y
    ox_s, oy_s = src_extent.origin_x, src_extent.origin_y
    # u_src = a*u + b*v + c ; v_src = d*u + e*v + f
    a = (mpp_d * cos_g) / mpp_s
    b = (mpp_d * sin_g) / mpp_s
    c = (ox_d * cos_g + oy_d * sin_g - ox_s) / mpp_s
    d = (-mpp_d * sin_g) / mpp_s
    e = (mpp_d * cos_g) / mpp_s
    f = (-ox_d * sin_g + oy_d * cos_g - oy_s) / mpp_s
    # IHDR cap před Image.open – obří georef @ 600 by jinak shodil Pillow bomb.
    assert_png_within_web_pixel_limit(mapper_png)
    with Image.open(mapper_png) as im:
        rgb = im.convert("RGB")
        out = rgb.transform(
            (dest_extent.width, dest_extent.height),
            Image.Transform.AFFINE,
            (a, b, c, d, e, f),
            resample=Image.Resampling.BILINEAR,
            fillcolor=(255, 255, 255),
        )
        dest_png.parent.mkdir(parents=True, exist_ok=True)
        out.save(dest_png, format="PNG", optimize=True)
        return out.size


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
    # Jen výplň plochy – ne barvy pattern teček/čar uvnitř <pattern>
    # (ISOM 412.1 má inner_color="-1" + černé tečky → dřív solid black přes 412).
    try:
        color = int(target.attrib.get("inner_color", "-1") or -1)
    except ValueError:
        color = -1
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


@dataclass(frozen=True)
class OmapGeoref:
    """Georef z ``<georeferencing>`` v .omap (metr S-JTSK + měřítko)."""

    scale: int
    ref_x: float
    ref_y: float
    grivation_deg: float
    auxiliary_scale_factor: float = 1.0
    map_ref_x: float = 0.0
    map_ref_y: float = 0.0

    @property
    def combined_scale_factor(self) -> float:
        return float(self.auxiliary_scale_factor) if self.auxiliary_scale_factor else 1.0


@dataclass(frozen=True)
class PreviewExtent:
    """Výřez náhledu v mapových jednotkách + PNG mřížka.

    U webového náhledu jsou souřadnice po odrotování grivace; u georef
    zůstávají nativní (s magnetickým natočením).
    """

    origin_x: float  # map X levého horního rohu (okraj pixelu, ne střed)
    origin_y: float  # map Y levého horního rohu
    map_per_px: float  # mapové jednotky na 1 PNG pixel
    width: int
    height: int


def map_grivation_deg(root: ET.Element) -> float:
    """Grivace z ``<georeferencing>`` – web ji odrotuje, georef PNG nechá."""
    return parse_omap_georef(root).grivation_deg


def parse_omap_georef(root: ET.Element) -> OmapGeoref:
    """Načte ref. bod a měřítko z .omap; chybějící hodnoty → bezpečné defaulty."""
    scale = 10000
    aux = 1.0
    grivation = 0.0
    ref_x = 0.0
    ref_y = 0.0
    map_ref_x = 0.0
    map_ref_y = 0.0
    for el in root.iter():
        if _local(el.tag) != "georeferencing":
            continue
        try:
            scale = int(round(float(el.attrib.get("scale", "10000") or 10000)))
        except ValueError:
            scale = 10000
        try:
            aux = float(el.attrib.get("auxiliary_scale_factor", "1") or 1)
        except ValueError:
            aux = 1.0
        try:
            grivation = float(el.attrib.get("grivation", "0") or 0)
        except ValueError:
            grivation = 0.0
        for child in el.iter():
            tag = _local(child.tag)
            if tag == "ref_point" and "x" in child.attrib:
                try:
                    ref_x = float(child.attrib["x"])
                    ref_y = float(child.attrib.get("y", "0") or 0)
                except ValueError:
                    pass
            elif tag == "map_ref_point" and "x" in child.attrib:
                try:
                    map_ref_x = float(child.attrib["x"])
                    map_ref_y = float(child.attrib.get("y", "0") or 0)
                except ValueError:
                    pass
        break
    return OmapGeoref(
        scale=scale,
        ref_x=ref_x,
        ref_y=ref_y,
        grivation_deg=grivation,
        auxiliary_scale_factor=aux,
        map_ref_x=map_ref_x,
        map_ref_y=map_ref_y,
    )


def preview_crop_box(
    root: ET.Element,
    ops: list[_DrawOp],
    *,
    grivation_deg: float | None = None,
) -> tuple[float, float, float, float, float, float]:
    """Vrátí (minx, miny, maxx, maxy, padx, pady) v mapových jednotkách."""
    if grivation_deg is None:
        grivation_deg = map_grivation_deg(root)
    frame = aoi_frame_bbox(root, grivation_deg=grivation_deg)
    tick_pad = max((op.tick_length for op in ops if op.mid_mode), default=0.0)
    if frame is not None:
        minx, miny, maxx, maxy = frame
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
    return minx, miny, maxx, maxy, padx, pady


def preview_extent_from_crop(
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
    padx: float,
    pady: float,
    *,
    max_side: int = WEB_PREVIEW_MAX_SIDE,
    target_dpi: int | None = None,
    max_side_cap: int = GEOREF_PILLOW_MAX_SIDE_CAP,
    min_side_floor: int = GEOREF_PILLOW_MIN_SIDE_FLOOR,
) -> PreviewExtent:
    """PNG mřížka ze stejného ořezu jako ``render_omap_xml_png``.

    ``target_dpi``: georef Pillow – velikost z papírových map. j. (1/1000 mm)
    jako Mapper ``--dpi`` (``map_per_px = 25400 / dpi``), nejméně tak jemné
    jako ``min_side_floor`` (default 4800), nanejvýš ``max_side_cap``.
    Bez ``target_dpi`` jen ``max_side`` (web náhled).
    """
    spanx = max(maxx - minx, 1.0)
    spany = max(maxy - miny, 1.0)
    world_w = spanx + 2 * padx
    world_h = spany + 2 * pady
    longest_world = max(world_w, world_h)
    if target_dpi is not None and int(target_dpi) > 0:
        paper_mpp = 25400.0 / float(int(target_dpi))
        floor = max(1, int(min_side_floor))
        floor_mpp = longest_world / float(floor)
        # Jemnější = menší map_per_px (600 DPI nebo aspoň dřívější 1600 cap).
        map_per_px = min(paper_mpp, floor_mpp)
        width = max(1, int(round(world_w / map_per_px)))
        height = max(1, int(round(world_h / map_per_px)))
        longest = max(width, height)
        cap = max(1, int(max_side_cap))
        if longest > cap:
            shrink = cap / float(longest)
            width = max(1, int(round(width * shrink)))
            height = max(1, int(round(height * shrink)))
            map_per_px = world_w / float(width)
        return PreviewExtent(
            origin_x=minx - padx,
            origin_y=miny - pady,
            map_per_px=map_per_px,
            width=width,
            height=height,
        )
    scale = max(1, int(max_side)) / longest_world
    width = max(1, int(round(world_w * scale)))
    height = max(1, int(round(world_h * scale)))
    return PreviewExtent(
        origin_x=minx - padx,
        origin_y=miny - pady,
        map_per_px=1.0 / scale,
        width=width,
        height=height,
    )


def pgw_for_preview(
    extent: PreviewExtent,
    georef: OmapGeoref,
    *,
    with_grivation: bool,
) -> PgwGeoref:
    """World file EPSG:5514 pro PNG výřez.

    ``with_grivation=True``: PNG má magnetické natočení jako Mapper → PGW
    může mít rotační členy. ``False``: grid north nahoru, rotace ≈ 0.
    Horní řádek PNG = nižší map Y. Střed UL pixelu = origin + 0,5·map_per_px.
    """
    g = georef.grivation_deg if with_grivation else 0.0
    half = 0.5 * extent.map_per_px
    ul_mx = extent.origin_x + half
    ul_my = extent.origin_y + half
    right_mx = ul_mx + extent.map_per_px
    down_my = ul_my + extent.map_per_px

    def _to_proj(mx: float, my: float) -> tuple[float, float]:
        return map_to_projected(
            mx,
            my,
            ref_x=georef.ref_x,
            ref_y=georef.ref_y,
            scale=georef.scale,
            grivation_deg=g,
            combined_scale_factor=georef.combined_scale_factor,
            map_ref_x=georef.map_ref_x,
            map_ref_y=georef.map_ref_y,
        )

    ul_x, ul_y = _to_proj(ul_mx, ul_my)
    r_x, r_y = _to_proj(right_mx, ul_my)
    d_x, d_y = _to_proj(ul_mx, down_my)
    return PgwGeoref(
        pixel_x=r_x - ul_x,
        rot_row=r_y - ul_y,
        rot_col=d_x - ul_x,
        pixel_y=d_y - ul_y,
        origin_x=ul_x,
        origin_y=ul_y,
    )


def write_preview_pgw(
    dest_pgw: Path,
    extent: PreviewExtent,
    georef: OmapGeoref,
    *,
    with_grivation: bool = True,
) -> PgwGeoref:
    """Zapíše ``.pgw`` (+ ``.prj`` se stejným stemem) pro QGIS / GDAL."""
    pgw = pgw_for_preview(extent, georef, with_grivation=with_grivation)
    dest_pgw.parent.mkdir(parents=True, exist_ok=True)
    pgw.write(dest_pgw)
    # Stejný WKT jako SHP – QGIS u PNG+PGW bere sidecare .prj.
    prj = dest_pgw.with_suffix(".prj")
    prj.write_text(CRS_WKT + "\n", encoding="utf-8")
    return pgw


def try_write_geotiff_from_png_pgw(
    png: Path,
    dest_tif: Path | None = None,
    *,
    log=None,
) -> Path | None:
    """Volitelný GeoTIFF přes ``gdal_translate`` (PNG+PGW → GTiff + EPSG:5514).

    Nízká složitost: GDAL si přečte world file sám. Chybí-li nástroj, vrátí None.
    """
    from app.pipeline.prepare_lidar import run_cmd
    from app.tool_env import which_tool

    png = Path(png)
    pgw = png.with_suffix(".pgw")
    if not png.is_file() or not pgw.is_file():
        return None
    translate = which_tool("gdal_translate")
    if not translate:
        if log:
            log("OOM georef: gdal_translate chybí – GeoTIFF přeskočen")
        return None
    dest = Path(dest_tif) if dest_tif else png.with_suffix(".tif")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_cmd(
            [
                translate,
                "-of",
                "GTiff",
                "-a_srs",
                CRS_PROJ4,
                "-co",
                "COMPRESS=DEFLATE",
                "-co",
                "PREDICTOR=2",
                str(png),
                str(dest),
            ],
            log=None,
        )
    except Exception as exc:
        if log:
            log(f"OOM georef: GeoTIFF selhal ({exc})")
        if dest.exists():
            dest.unlink(missing_ok=True)
        return None
    if not dest.is_file() or dest.stat().st_size < 64:
        return None
    if log:
        log(f"OOM georef: GeoTIFF → {dest.name} ({CRS_LABEL})")
    return dest


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
    max_side: int = WEB_PREVIEW_MAX_SIDE,
    target_dpi: int | None = None,
    undo_grivation: bool = True,
) -> tuple[int, int, int, PreviewExtent, OmapGeoref]:
    """Vykreslí objekty mapy do PNG. Vrátí (šířka, výška, počet ops, extent, georef).

    ``undo_grivation=True`` (web): srovnání bez deklinace.
    ``False`` (georef ZIP): magnetické natočení jako v Mapperu.
    ``target_dpi``: georef Pillow ≈ Mapper DPI; jinak škálování ``max_side``.
    """
    from PIL import Image, ImageDraw

    root = ET.fromstring(_read_omap_bytes(Path(omap)))
    georef = parse_omap_georef(root)
    g_undo = georef.grivation_deg if undo_grivation else 0.0
    ops = _collect_ops(root, grivation_deg=g_undo)
    if not ops:
        raise ValueError(f"{omap.name}: .omap nemá vykreslitelné objekty")

    minx, miny, maxx, maxy, padx, pady = preview_crop_box(
        root, ops, grivation_deg=g_undo
    )
    extent = preview_extent_from_crop(
        minx,
        miny,
        maxx,
        maxy,
        padx,
        pady,
        max_side=max_side,
        target_dpi=target_dpi,
    )
    width, height = extent.width, extent.height
    scale = 1.0 / extent.map_per_px
    origin_x = extent.origin_x
    origin_y = extent.origin_y

    def to_px(x: float, y: float) -> tuple[float, float]:
        return (x - origin_x) * scale, (y - origin_y) * scale

    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for op in ops:
        if op.kind == "area":
            rings = [
                [to_px(x, y) for x, y in path] for path in op.paths
            ]
            outer = [pts for pts in rings if len(pts) >= 3]
            if not outer:
                continue
            # Maska jen přes bbox plochy (ne celý obrázek) – dřív 2 plné
            # rastry + paste na každou plochu (~14 min / 10k objektů @ 600 DPI).
            # Celočíselný posun = stejná rasterizace jako na plném plátně.
            xs = [p[0] for pts in outer for p in pts]
            ys = [p[1] for pts in outer for p in pts]
            x0 = max(0, int(math.floor(min(xs))) - 2)
            y0 = max(0, int(math.floor(min(ys))) - 2)
            x1 = min(width, int(math.ceil(max(xs))) + 3)
            y1 = min(height, int(math.ceil(max(ys))) + 3)
            if x1 <= x0 or y1 <= y0:
                continue
            mask = Image.new("L", (x1 - x0, y1 - y0), 0)
            mask_draw = ImageDraw.Draw(mask)
            for ring_index, pts in enumerate(rings):
                if len(pts) < 3:
                    continue
                mask_draw.polygon(
                    [(px - x0, py - y0) for px, py in pts],
                    fill=0 if ring_index else 255,
                )
            image.paste(op.rgb, (x0, y0, x1, y1), mask)
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
    return width, height, len(ops), extent, georef


def render_omap_to_png(
    omap: Path,
    dest: Path,
    *,
    log=None,
    max_side: int = WEB_PREVIEW_MAX_SIDE,
    target_dpi: int | None = None,
    write_pgw: bool = False,
    write_geotiff: bool = False,
    undo_grivation: bool | None = None,
    engine: str | None = None,
) -> str:
    """PNG z ``.omap``.

    ``engine``:
      - ``"pillow"`` – vestavěný XML render (web i fallback testů)
      - ``"mapper"`` – Mapper CLI @ ``GEOREF_MAPPER_DPI`` (georef); bez CLI vyhodí
        ``MapperExportError``
      - ``None`` – ``"mapper"`` při ``write_pgw``, jinak ``"pillow"``

    ``undo_grivation``: default ``False`` při ``write_pgw`` (georef s deklinací),
    jinak ``True`` (web bez deklinace). U Mapper georef se grivace nechá v mapě.
    ``target_dpi``: jen Pillow georef – papírové DPI (default přes caller);
    web nechává ``None`` + ``max_side``.
    """
    omap = Path(omap)
    dest = Path(dest)
    if undo_grivation is None:
        undo_grivation = not write_pgw
    if engine is None:
        engine = "mapper" if write_pgw else "pillow"
    engine = engine.strip().lower()

    if engine == "mapper":
        if undo_grivation:
            raise ValueError(
                "Mapper export zachovává grivaci – undo_grivation musí být False"
            )
        dpi = (
            int(target_dpi)
            if target_dpi is not None and int(target_dpi) > 0
            else GEOREF_MAPPER_DPI
        )
        run_mapper_export(omap, dest, dpi=dpi, log=log)
        extent, georef = extent_for_mapper_png(omap, dest)
        if write_pgw:
            write_preview_pgw(
                dest.with_suffix(".pgw"),
                extent,
                georef,
                with_grivation=True,
            )
            if write_geotiff:
                try_write_geotiff_from_png_pgw(dest, log=log)
        if log:
            kind = "georef" if write_pgw else "web"
            log(
                f"OOM {kind}: Mapper {dest.name} "
                f"({extent.width}×{extent.height} @ {dpi} DPI, s grivací)"
            )
        return f"mapper-cli {extent.width}x{extent.height} dpi={dpi}"

    if engine != "pillow":
        raise ValueError(f"Neznámý render engine: {engine!r}")

    width, height, count, extent, georef = render_omap_xml_png(
        omap,
        dest,
        max_side=max_side,
        target_dpi=target_dpi,
        undo_grivation=undo_grivation,
    )
    if write_pgw:
        write_preview_pgw(
            dest.with_suffix(".pgw"),
            extent,
            georef,
            with_grivation=not undo_grivation,
        )
        if write_geotiff:
            try_write_geotiff_from_png_pgw(dest, log=log)
    if log:
        orient = "bez deklinace" if undo_grivation else "s grivací"
        dpi_note = (
            f", ~{int(target_dpi)} DPI-eq"
            if target_dpi is not None and int(target_dpi) > 0
            else ""
        )
        log(
            f"OOM náhled: Pillow render {dest.name} "
            f"({width}×{height}{dpi_note}, {count} objektů, {orient})"
        )
    if target_dpi is not None and int(target_dpi) > 0:
        return f"xml {width}x{height} n={count} dpi={int(target_dpi)}"
    return f"xml {width}x{height} n={count}"


def pick_preview_omap(paths: list[Path]) -> Path | None:
    existing = [Path(p) for p in paths if Path(p).is_file()]
    if not existing:
        return None
    sprint = [p for p in existing if "sprint" in p.name.lower()]
    return sprint[0] if sprint else existing[0]


def georef_preview_enabled(options: dict | None = None) -> bool:
    """GeoTIFF vedle PNG+PGW – default zapnuto, když je gdal_translate."""
    options = options or {}
    if "oom_geotiff" in options and options["oom_geotiff"] is not None:
        return bool(options["oom_geotiff"])
    env = os.environ.get("PODKLADARNA_OOM_GEOTIFF", "").strip().lower()
    if env in _FALSE:
        return False
    if env in _TRUE:
        return True
    return True


def _georef_engine_summary(mapper_n: int, pillow_n: int, pillow_dpi: int) -> str:
    parts = []
    if mapper_n:
        parts.append(f"{mapper_n}× Mapper @ 600 DPI")
    if pillow_n:
        parts.append(f"{pillow_n}× Pillow fallback @ {pillow_dpi} DPI-eq")
    return ", ".join(parts) or "nic"


def write_job_oom_preview(
    omap_paths: list[Path],
    work_dir: Path,
    output_dir: Path,
    options: dict | None,
    log=None,
) -> Path | None:
    """Po zápisu ``.omap`` uloží webový JPEG náhled (± volitelně georef).

    Web (náhled jobu / klik): preferuje **Mapper CLI** (reuse georef PNG @ 600
    DPI jen pokud IHDR ≤ ``WEB_MAPPER_MAX_PIXELS``, jinak Mapper @ capped
    ``WEB_MAPPER_DPI``) → AOI ořez + zrušení deklinace → ``preview.jpg`` /
    ``oom_preview.jpg``. Bez Mapperu / při chybě **Pillow** fallback → JPEG
    (už s AOI + undo grivation, ``max_side`` ≤1600).

    Georef (opt-in ``output_georef``): plná kvalita ``{stem}.png`` + ``.pgw``
    (+ volitelně GeoTIFF) – Mapper @ 600 DPI s grivací, jinak Pillow @ 600 DPI-eq.
    """
    from app.pipeline.prepare_lidar import log_step

    if not oom_preview_enabled(options) and not output_georef_enabled(options):
        return None
    existing = [Path(p) for p in omap_paths if Path(p).is_file()]
    if not existing:
        if log:
            log("OOM náhled: žádný .omap")
        return None

    preview_dir = Path(output_dir) / GEOREF_PREVIEW_DIR
    preview_dir.mkdir(parents=True, exist_ok=True)
    want_georef = output_georef_enabled(options)
    want_geotiff = want_georef and georef_preview_enabled(options)
    written: list[Path] = []
    # Georef PNG skutečně z Mapperu (full-map rozsah) – jen ty jde znovu použít
    # pro web ořez. Pillow fallback má jiný rozsah → dřív celý oranžový náhled.
    mapper_made: set[str] = set()
    pillow_made: set[str] = set()
    use_mapper = mapper_export_configured()
    georef_engine = "mapper" if use_mapper else "pillow"
    pillow_dpi = georef_pillow_dpi()
    web_dpi = web_mapper_dpi()
    web_px_limit = web_mapper_max_pixels()

    if want_georef:
        if use_mapper:
            log_step(log, "Vykresluji georeferencované náhledy PNG+PGW (Mapper @ 600 DPI)")
        else:
            log_step(
                log,
                "Vykresluji georeferencované náhledy PNG+PGW "
                f"(Pillow fallback @ {pillow_dpi} DPI-eq – Mapper CLI není nakonfigurovaný)",
            )
            if log:
                log(
                    "OOM georef: Mapper CLI chybí "
                    "(PODKLADARNA_MAPPER + PODKLADARNA_MAPPER_EXPORT) – "
                    f"georef ZIP bude Pillow PNG+PGW (±GeoTIFF) @ {pillow_dpi} DPI-eq "
                    f"(min_side≥{GEOREF_PILLOW_MIN_SIDE_FLOOR}, "
                    f"max_side_cap={GEOREF_PILLOW_MAX_SIDE_CAP}), ne Mapper @ 600 DPI."
                )
        for omap in existing:
            dest_png = preview_dir / f"{omap.stem}.png"
            engine_used = georef_engine
            try:
                summary = render_omap_to_png(
                    omap,
                    dest_png,
                    log=log,
                    write_pgw=True,
                    write_geotiff=want_geotiff,
                    undo_grivation=False,
                    engine=georef_engine,
                    target_dpi=None if use_mapper else pillow_dpi,
                )
            except MapperExportError as exc:
                if log:
                    log(
                        f"OOM georef: {omap.name} Mapper selhal ({exc}) – "
                        f"zkouším Pillow @ {pillow_dpi} DPI-eq"
                    )
                engine_used = "pillow"
                try:
                    summary = render_omap_to_png(
                        omap,
                        dest_png,
                        log=log,
                        write_pgw=True,
                        write_geotiff=want_geotiff,
                        undo_grivation=False,
                        engine="pillow",
                        target_dpi=pillow_dpi,
                    )
                except Exception as pillow_exc:
                    if log:
                        log(
                            f"OOM georef: {omap.name} Pillow fallback selhal ({pillow_exc})"
                        )
                    continue
            except Exception as exc:
                if log:
                    log(f"OOM georef: {omap.name} selhal ({exc})")
                continue
            if dest_png.is_file() and dest_png.with_suffix(".pgw").is_file():
                written.append(dest_png)
                (mapper_made if engine_used == "mapper" else pillow_made).add(
                    dest_png.name
                )
                if log:
                    log(f"OOM georef: {omap.name} → {dest_png.name} ({summary})")
    elif log:
        log(
            "OOM georef: přeskočeno (output_georef vypnuto) – "
            "jen webový náhled."
        )

    preferred = pick_preview_omap(existing)
    if preferred is None:
        return None
    if not oom_preview_enabled(options):
        if log and written:
            src = _georef_engine_summary(
                len(mapper_made), len(pillow_made), pillow_dpi
            )
            log(
                f"OOM georef: hotovo {len(written)} variant ({src})"
                + (" + GeoTIFF" if want_geotiff else "")
            )
        return None

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    work_jpg = work_dir / "preview.jpg"
    named_jpg = preview_dir / "oom_preview.jpg"
    work_png = work_dir / "preview.png"
    named_png = preview_dir / "oom_preview.png"
    tmp_mapper = work_dir / "_web_mapper_tmp.png"
    georef_full = preview_dir / f"{preferred.stem}.png"

    def _clear_web_sidecars() -> None:
        for stale in (
            work_dir / "preview.pgw",
            work_dir / "preview.prj",
            preview_dir / "oom_preview.pgw",
            preview_dir / "oom_preview.prj",
            preview_dir / "oom_preview.tif",
        ):
            stale.unlink(missing_ok=True)

    web_out: Path | None = None
    web_timeout = web_preview_timeout_sec()

    def _cleanup_web_temps() -> None:
        tmp_mapper.unlink(missing_ok=True)
        (work_dir / "_web_mapper_aoi.png").unlink(missing_ok=True)
        work_png.unlink(missing_ok=True)

    def _try_mapper_web() -> Path | None:
        src_png: Path | None = None
        src_label = ""
        if (
            use_mapper
            and want_georef
            and georef_full.is_file()
            and georef_full.name in mapper_made
        ):
            # Reuse georef @ 600 jen když IHDR vejde – nikdy Image.open na ~494 Mpx.
            if png_within_web_pixel_limit(georef_full, max_pixels=web_px_limit):
                src_png = georef_full
                gw, gh = _png_size(georef_full)
                src_label = f"georef {georef_full.name} ({gw}×{gh})"
            elif log:
                gp = png_pixel_count(georef_full)
                log(
                    f"OOM náhled (web): georef {georef_full.name} "
                    f"({gp} px) > limit {web_px_limit} – "
                    "nereuse (skip Image.open), Mapper @ capped web DPI"
                )
        if src_png is None and use_mapper:
            dpi = capped_web_mapper_dpi(
                preferred,
                desired_dpi=web_dpi,
                max_pixels=web_px_limit,
            )
            if log and dpi < web_dpi:
                log(
                    f"OOM náhled (web): DPI capped {web_dpi} → {dpi} "
                    f"(max {web_px_limit} px)"
                )
            render_omap_to_png(
                preferred,
                tmp_mapper,
                log=log,
                write_pgw=False,
                undo_grivation=False,
                engine="mapper",
                target_dpi=dpi,
            )
            # IHDR před open – když i capped export přestřelí, neotevírat.
            assert_png_within_web_pixel_limit(tmp_mapper, max_pixels=web_px_limit)
            src_png = tmp_mapper
            src_label = f"Mapper @ {dpi} DPI"
        if src_png is None or not src_png.is_file():
            return None
        cropped = work_dir / "_web_mapper_aoi.png"
        cw, ch = prepare_mapper_web_preview(
            preferred,
            src_png,
            cropped,
            max_side=WEB_PREVIEW_MAX_SIDE,
        )
        w, h = write_web_preview_jpeg(cropped, work_jpg)
        shutil.copy2(work_jpg, named_jpg)
        work_png.unlink(missing_ok=True)
        named_png.unlink(missing_ok=True)
        tmp_mapper.unlink(missing_ok=True)
        cropped.unlink(missing_ok=True)
        _clear_web_sidecars()
        if log:
            log(
                f"OOM náhled (web Mapper JPEG): {preferred.name} → "
                f"preview.jpg ({src_label} → AOI+bez deklinace "
                f"{cw}×{ch} → {w}×{h} q={WEB_JPEG_QUALITY})"
            )
        return work_jpg

    def _try_pillow_web() -> Path | None:
        summary = render_omap_to_png(
            preferred,
            work_png,
            log=log,
            write_pgw=False,
            undo_grivation=True,
            engine="pillow",
            max_side=WEB_PREVIEW_MAX_SIDE,
        )
        w, h = write_web_preview_jpeg(work_png, work_jpg)
        shutil.copy2(work_jpg, named_jpg)
        work_png.unlink(missing_ok=True)
        named_png.unlink(missing_ok=True)
        _clear_web_sidecars()
        if log:
            log(
                f"OOM náhled (web Pillow JPEG fallback): "
                f"{preferred.name} → preview.jpg ({summary} → {w}×{h})"
            )
        return work_jpg

    # Celý web náhled pod deadline – hang (bomb→fallback) nesmí blokovat ZIP.
    try:
        with _web_preview_deadline(web_timeout):
            mapper_exc: Exception | None = None
            try:
                web_out = _try_mapper_web()
            except TimeoutError:
                raise  # deadline – nepolykat jako běžný Mapper fail
            except Exception as exc:
                mapper_exc = exc
                _cleanup_web_temps()
                if log:
                    log(
                        f"OOM náhled (web Mapper): {preferred.name} selhal "
                        f"({exc}) – Pillow fallback"
                    )
            if web_out is None:
                try:
                    web_out = _try_pillow_web()
                except TimeoutError:
                    raise
                except Exception as exc:
                    _cleanup_web_temps()
                    if log:
                        log(
                            f"OOM náhled (web): {preferred.name} selhal ({exc})"
                        )
                    if mapper_exc is not None and _is_pixel_bomb_error(mapper_exc):
                        if log:
                            log(
                                "OOM náhled (web): po pixel bomb / IHDR cap "
                                "fallback selhal – pokračuji bez web náhledu "
                                "(ZIP/OCD dál)"
                            )
    except TimeoutError as exc:
        _cleanup_web_temps()
        if log:
            log(
                f"OOM náhled (web): {preferred.name} timeout ({exc}) – "
                "pokračuji bez web náhledu (ZIP/OCD dál)"
            )
        web_out = None
    except Exception as exc:
        # Obrana: nic z web náhledu nesmí shodit zbytek pipeline.
        _cleanup_web_temps()
        if log:
            log(
                f"OOM náhled (web): {preferred.name} neočekávaně selhal "
                f"({exc}) – pokračuji bez web náhledu"
            )
        web_out = None

    if log and written:
        src = _georef_engine_summary(len(mapper_made), len(pillow_made), pillow_dpi)
        log(
            f"OOM georef: hotovo {len(written)} variant ({src})"
            + (" + GeoTIFF" if want_geotiff else "")
        )
    elif log and want_georef and not written:
        log("OOM georef: žádná varianta nevznikla – tlačítko stažení nebude")
    return web_out if web_out is not None and web_out.is_file() else None



def list_georef_preview_files(preview_dir: Path) -> list[Path]:
    """Soubory georef náhledů (PNG/PGW/PRJ/TIF) mimo webový ``oom_preview*``."""
    preview_dir = Path(preview_dir)
    if not preview_dir.is_dir():
        return []
    out: list[Path] = []
    for png in sorted(preview_dir.glob("*.png")):
        if png.name.lower().startswith("oom_preview"):
            continue
        pgw = png.with_suffix(".pgw")
        if not pgw.is_file():
            continue
        out.append(png)
        out.append(pgw)
        prj = png.with_suffix(".prj")
        if prj.is_file():
            out.append(prj)
        tif = png.with_suffix(".tif")
        if tif.is_file():
            out.append(tif)
    return out


def build_georef_previews_zip(
    output_dir: Path,
    dest_zip: Path | None = None,
    *,
    log=None,
) -> Path | None:
    """Malý ZIP jen s georeferencovanými náhledy (PNG+PGW±TIF)."""
    import zipfile

    from app.pipeline.prepare_lidar import log_step

    preview_dir = Path(output_dir) / GEOREF_PREVIEW_DIR
    files = list_georef_preview_files(preview_dir)
    if not files:
        return None
    dest = Path(dest_zip) if dest_zip else Path(output_dir) / GEOREF_PREVIEWS_ZIP_NAME
    if dest.exists():
        dest.unlink()
    log_step(log, "Balím ZIP jen s georeferencovanými náhledy")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "README.txt",
            (
                "Podkladárna – georeferencované náhledy mapy\n"
                "==========================================\n\n"
                "Každá varianta (les / mtbo / sprint dle měřítka) má:\n"
                "  *.png  – rastr georef náhledu (Mapper CLI @ 600 DPI když je CLI;\n"
                "           jinak Pillow @ 600 DPI-eq) – s grivací / magnetickým natočením\n"
                "  *.pgw  – ESRI world file (metry EPSG:5514 / S-JTSK; může mít rotaci)\n"
                "  *.prj  – WKT souřadnicového systému\n"
                "  *.tif  – volitelný GeoTIFF (když je k dispozici GDAL)\n\n"
                "Otevři PNG+PGW v QGIS (zadej EPSG:5514, pokud se neptá),\n"
                "nebo rovnou GeoTIFF. Není to tisková mapa – jen georef náhled.\n"
                "Webový náhled „Otevřít PNG“ je zvlášť (Pillow, bez deklinace)\n"
                "a do materiálového OOM ZIPu se PNG náhledy nedávají.\n"
            ),
        )
        for path in files:
            zf.write(path, path.name)
    if log:
        log(
            f"OOM georef: ZIP náhledů → {dest.name} "
            f"({dest.stat().st_size / 1e3:.0f} kB, {len(files)} souborů)"
        )
    return dest
