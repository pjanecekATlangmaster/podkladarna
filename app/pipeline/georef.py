from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PgwGeoref:
    pixel_x: float
    rot_row: float
    rot_col: float
    pixel_y: float
    origin_x: float
    origin_y: float

    def write(self, path: Path) -> None:
        path.write_text(
            "\n".join(
                [
                    f"{self.pixel_x}",
                    f"{self.rot_row}",
                    f"{self.rot_col}",
                    f"{self.pixel_y}",
                    f"{self.origin_x}",
                    f"{self.origin_y}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )


def read_pgw(path: Path) -> PgwGeoref:
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    if len(lines) < 6:
        raise ValueError(f"Neplatný PGW: {path}")
    vals = [float(x) for x in lines[:6]]
    return PgwGeoref(*vals)


def png_pixel_size(path: Path) -> tuple[int, int]:
    from struct import unpack

    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Neplatný PNG: {path}")
    offset = 8
    while offset + 8 <= len(data):
        length = unpack(">I", data[offset : offset + 4])[0]
        chunk = data[offset + 4 : offset + 8]
        if chunk == b"IHDR":
            width, height = unpack(">II", data[offset + 8 : offset + 16])
            return int(width), int(height)
        offset += 12 + length
    raise ValueError(f"PNG bez IHDR: {path}")


def projected_center_from_raster(png: Path, pgw: Path) -> tuple[float, float]:
    """Střed rastru v metrech S-JTSK podle PGW (stejný základ jako šablony v OOM)."""
    georef = read_pgw(pgw)
    width, height = png_pixel_size(png)
    x = georef.origin_x + (width / 2) * georef.pixel_x
    y = georef.origin_y + (height / 2) * georef.pixel_y
    return x, y
