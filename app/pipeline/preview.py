"""Náhledová PNG / georef šablona bez tvrdé závislosti na KP pullautus.

Po drop KP preferujeme ``preview.png`` (+ ``preview.pgw``); ``pullautus``
zůstává hybridní fallback. Compose náhledu = hillshade (± volitelné overlaye)
na kanonické ``job_grid``.
"""

from __future__ import annotations

import shutil
import struct
import zlib
from pathlib import Path

from app.pipeline.job_grid import JobGrid

PREVIEW_PNG = "preview.png"
PREVIEW_PGW = "preview.pgw"
PULLAUTUS_PNG = "pullautus.png"
PULLAUTUS_PGW = "pullautus.pgw"


def resolve_preview_png(*dirs: Path) -> Path | None:
    """Vrátí existující náhled: ``preview.png`` má prioritu před ``pullautus.png``.

    Bez-KP OOM render také ukládá ``preview/oom_preview.png`` (web i ZIP).
    """
    for directory in dirs:
        if directory is None:
            continue
        root = Path(directory)
        for name in (PREVIEW_PNG, PULLAUTUS_PNG):
            path = root / name
            if path.is_file():
                return path
        oom = root / "preview" / "oom_preview.png"
        if oom.is_file():
            return oom
    return None


def has_preview(*dirs: Path) -> bool:
    return resolve_preview_png(*dirs) is not None


def write_solid_gray_png(path: Path, width: int, height: int, gray: int = 180) -> None:
    """Minimální grayscale PNG (solid) – poslední fallback bez shade."""
    if width < 1 or height < 1:
        raise ValueError(f"Neplatný rozměr PNG: {width}×{height}")
    g = max(0, min(255, int(gray)))
    raw = b"".join(b"\x00" + bytes([g]) * width for _ in range(height))
    compressed = zlib.compress(raw, 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )


def _blend_grayscale_over_rgb(
    base_png: Path,
    overlay_rgba: Path,
    dest: Path,
    *,
    opacity: float = 0.35,
) -> bool:
    """Lehký overlay (např. CHM tint) přes shade – vyžaduje Pillow, jinak False."""
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        base = Image.open(base_png).convert("RGB")
        over = Image.open(overlay_rgba).convert("RGBA")
        if over.size != base.size:
            over = over.resize(base.size, Image.Resampling.BILINEAR)
        alpha = max(0.0, min(1.0, float(opacity)))
        # Škáluj alpha kanál overlaye.
        r, g, b, a = over.split()
        a = a.point(lambda p: int(p * alpha))
        over = Image.merge("RGBA", (r, g, b, a))
        composed = base.convert("RGBA")
        composed.alpha_composite(over)
        dest.parent.mkdir(parents=True, exist_ok=True)
        composed.convert("RGB").save(dest, format="PNG", optimize=True)
        return dest.is_file() and dest.stat().st_size >= 64
    except Exception:
        return False


def compose_job_preview(
    work_dir: Path,
    *,
    force: bool = False,
    prefer_kp_pullautus: bool = False,
    overlay_png: Path | None = None,
    overlay_opacity: float = 0.35,
    bounds_5514: tuple[float, float, float, float] | None = None,
    log=None,
) -> tuple[Path, Path] | None:
    """Složí ``preview.png`` ze shade (± volitelný overlay) na job_grid.

    Při ``prefer_kp_pullautus=True`` a existujícím pullautus nechá KP náhled
    (hybrid zelený); jinak vždy preferuje vlastní shade compose.
    Funguje i když KP PNG chybí. Bez-KP defaultně nevolej (Petr: oželít PNG).
    """
    work_dir = Path(work_dir)
    preview_png = work_dir / PREVIEW_PNG
    preview_pgw = work_dir / PREVIEW_PGW

    if prefer_kp_pullautus:
        kp_png = work_dir / PULLAUTUS_PNG
        kp_pgw = work_dir / PULLAUTUS_PGW
        if kp_png.is_file() and kp_pgw.is_file() and not force:
            if log:
                log("Náhled: hybrid – ponechávám pullautus.png")
            return kp_png, kp_pgw

    if (
        not force
        and preview_png.is_file()
        and preview_pgw.is_file()
        and preview_png.stat().st_size >= 64
    ):
        return preview_png, preview_pgw

    grid = JobGrid.load(work_dir)
    if grid is None:
        if log:
            log("Náhled: chybí job_grid – nelze složit preview")
        return None

    from app.pipeline.shade import build_job_shade, resolve_shade_png

    if log:
        log("=== Fáze: náhled PNG (shade compose) ===")
    shade = resolve_shade_png(work_dir)
    if shade is None:
        shade = build_job_shade(
            work_dir, bounds_5514=bounds_5514, prefer_local=False, log=log
        )

    if shade is not None and shade.is_file():
        if overlay_png is not None and overlay_png.is_file():
            blended = work_dir / "_preview_blend.png"
            if _blend_grayscale_over_rgb(
                shade, overlay_png, blended, opacity=overlay_opacity
            ):
                shutil.copy2(blended, preview_png)
                blended.unlink(missing_ok=True)
            else:
                shutil.copy2(shade, preview_png)
        else:
            shutil.copy2(shade, preview_png)
        shade_pgw = shade.with_suffix(".pgw")
        if shade_pgw.is_file():
            shutil.copy2(shade_pgw, preview_pgw)
        else:
            grid.to_pgw().write(preview_pgw)
        if log:
            log(f"Náhled: {PREVIEW_PNG} ze shade ({grid.width}×{grid.height})")
        return preview_png, preview_pgw

    # Poslední záchrana – solid canvas, ať API/reference mají extent.
    write_solid_gray_png(preview_png, grid.width, grid.height)
    grid.to_pgw().write(preview_pgw)
    if log:
        log(f"Náhled: placeholder {PREVIEW_PNG} (shade nedostupný)")
    return preview_png, preview_pgw


def ensure_georef_template(
    work_dir: Path,
    *,
    prefer_preview: bool = True,
    create_grid_placeholder: bool = True,
) -> tuple[Path, Path] | None:
    """PNG+PGW šablona: preview → pullautus → job_grid placeholder.

    Full shade compose řeší ``compose_job_preview``; zde jen existence souborů
    / solid canvas, ať WMS/reference mají extent.
    """
    work_dir = Path(work_dir)
    candidates: list[tuple[str, str]] = []
    if prefer_preview:
        candidates.append((PREVIEW_PNG, PREVIEW_PGW))
    candidates.append((PULLAUTUS_PNG, PULLAUTUS_PGW))

    for png_name, pgw_name in candidates:
        png = work_dir / png_name
        pgw = work_dir / pgw_name
        if png.is_file() and pgw.is_file():
            return png, pgw

    if not create_grid_placeholder:
        return None

    grid = JobGrid.load(work_dir)
    if grid is None:
        return None

    pgw = work_dir / "job.pgw"
    if not pgw.is_file():
        grid.to_pgw().write(pgw)

    # Existující rastr bez PGW – doplň job.pgw (jen pokud rozměry sedí / jsou čitelné).
    for png_name, _ in candidates:
        png = work_dir / png_name
        if not png.is_file():
            continue
        try:
            from app.pipeline.georef import png_pixel_size

            w, h = png_pixel_size(png)
            if w == grid.width and h == grid.height:
                return png, pgw
        except ValueError:
            pass
        return png, pgw

    # Placeholder georef canvas – záměrně NE preview.png (web by to bral jako náhled jobu).
    png = work_dir / "job.png"
    write_solid_gray_png(png, grid.width, grid.height)
    grid.to_pgw().write(work_dir / "job.pgw")
    return png, work_dir / "job.pgw"
