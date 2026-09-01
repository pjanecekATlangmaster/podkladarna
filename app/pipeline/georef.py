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
