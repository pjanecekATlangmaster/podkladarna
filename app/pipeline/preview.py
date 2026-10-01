"""Náhledová PNG / georef šablona bez tvrdé závislosti na KP pullautus.

Po drop KP preferujeme ``preview.png`` (+ ``preview.pgw``); ``pullautus``
zůstává hybridní fallback. Georef šablona pro reference layers může přijít
z kanonické ``job_grid`` (blank canvas + PGW), aniž by běžel plný shade compose.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from app.pipeline.job_grid import JobGrid

PREVIEW_PNG = "preview.png"
PREVIEW_PGW = "preview.pgw"
PULLAUTUS_PNG = "pullautus.png"
PULLAUTUS_PGW = "pullautus.pgw"


def resolve_preview_png(*dirs: Path) -> Path | None:
    """Vrátí existující náhled: ``preview.png`` má prioritu před ``pullautus.png``."""
    for directory in dirs:
        if directory is None:
            continue
        for name in (PREVIEW_PNG, PULLAUTUS_PNG):
            path = Path(directory) / name
            if path.is_file():
                return path
    return None


def has_preview(*dirs: Path) -> bool:
    return resolve_preview_png(*dirs) is not None


def write_solid_gray_png(path: Path, width: int, height: int, gray: int = 180) -> None:
    """Minimální grayscale PNG (solid) – placeholder místo KP shade compose."""
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


def ensure_georef_template(
    work_dir: Path,
    *,
    prefer_preview: bool = True,
    create_grid_placeholder: bool = True,
) -> tuple[Path, Path] | None:
    """PNG+PGW šablona: preview → pullautus → job_grid placeholder.

    Full shade compose sem nepatří – při chybějícím KP PNG jen solid canvas
    na kanonické mřížce, ať WMS/reference mají extent.
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

    # Placeholder preview na mřížce (ne KP shade).
    png = work_dir / PREVIEW_PNG
    write_solid_gray_png(png, grid.width, grid.height)
    grid.to_pgw().write(work_dir / PREVIEW_PGW)
    return png, work_dir / PREVIEW_PGW
