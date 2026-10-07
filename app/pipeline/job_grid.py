"""Kanonická georef mřížka jobu (EPSG:5514).

Všechny DEM-deriváty (vrstevnice, shade, CHM, …) mají sdílet stejný extent
a buňku. Pravda je ``job_grid.json`` + ``job.pgw``.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from app.pipeline.crs_5514 import CRS_LABEL
from app.pipeline.georef import PgwGeoref

GRID_JSON_NAME = "job_grid.json"
GRID_PGW_NAME = "job.pgw"
# Defaultní buňka: shodná s typickým GDAL DEM pro vrstevnice (1 m).
DEFAULT_RESOLUTION_M = 1.0


@dataclass(frozen=True)
class JobGrid:
    """Obdélník v S-JTSK zarovnaný na buňku ``resolution_m``."""

    xmin: float
    ymin: float
    xmax: float
    ymax: float
    resolution_m: float
    width: int
    height: int
    crs: str = CRS_LABEL

    @property
    def origin_x(self) -> float:
        return self.xmin

    @property
    def origin_y(self) -> float:
        return self.ymax

    @property
    def pixel_x(self) -> float:
        return float(self.resolution_m)

    @property
    def pixel_y(self) -> float:
        return -float(self.resolution_m)

    def bounds(self) -> tuple[float, float, float, float]:
        return self.xmin, self.ymin, self.xmax, self.ymax

    def to_pgw(self) -> PgwGeoref:
        return PgwGeoref(
            pixel_x=self.pixel_x,
            rot_row=0.0,
            rot_col=0.0,
            pixel_y=self.pixel_y,
            origin_x=self.origin_x,
            origin_y=self.origin_y,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def write(self, work_dir: Path) -> tuple[Path, Path]:
        work_dir.mkdir(parents=True, exist_ok=True)
        json_path = work_dir / GRID_JSON_NAME
        pgw_path = work_dir / GRID_PGW_NAME
        json_path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.to_pgw().write(pgw_path)
        return json_path, pgw_path

    @classmethod
    def from_dict(cls, data: dict) -> JobGrid:
        return cls(
            xmin=float(data["xmin"]),
            ymin=float(data["ymin"]),
            xmax=float(data["xmax"]),
            ymax=float(data["ymax"]),
            resolution_m=float(data["resolution_m"]),
            width=int(data["width"]),
            height=int(data["height"]),
            crs=str(data.get("crs") or CRS_LABEL),
        )

    @classmethod
    def load(cls, work_dir: Path) -> JobGrid | None:
        path = work_dir / GRID_JSON_NAME
        if not path.is_file():
            return None
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
            return None


def snap_bounds_to_grid(
    bounds: tuple[float, float, float, float],
    resolution_m: float,
) -> tuple[float, float, float, float, int, int]:
    """Zarovná extent na mřížku; vrací (xmin, ymin, xmax, ymax, width, height)."""
    xmin, ymin, xmax, ymax = bounds
    res = float(resolution_m)
    if res <= 0:
        raise ValueError(f"resolution_m musí být > 0, dostal jsem {resolution_m}")
    if xmax <= xmin or ymax <= ymin:
        raise ValueError(f"Neplatný extent: {bounds}")
    snap_xmin = math.floor(xmin / res) * res
    snap_ymin = math.floor(ymin / res) * res
    snap_xmax = math.ceil(xmax / res) * res
    snap_ymax = math.ceil(ymax / res) * res
    # Po floor/ceil může být šířka 0 u degenerovaného vstupu – min. 1 buňka.
    width = max(1, int(round((snap_xmax - snap_xmin) / res)))
    height = max(1, int(round((snap_ymax - snap_ymin) / res)))
    snap_xmax = snap_xmin + width * res
    snap_ymax = snap_ymin + height * res
    return snap_xmin, snap_ymin, snap_xmax, snap_ymax, width, height


def build_job_grid(
    bounds: tuple[float, float, float, float],
    *,
    resolution_m: float = DEFAULT_RESOLUTION_M,
) -> JobGrid:
    """Sestaví kanonickou mřížku z bboxu v EPSG:5514."""
    xmin, ymin, xmax, ymax, width, height = snap_bounds_to_grid(bounds, resolution_m)
    return JobGrid(
        xmin=xmin,
        ymin=ymin,
        xmax=xmax,
        ymax=ymax,
        resolution_m=float(resolution_m),
        width=width,
        height=height,
    )


def write_job_grid(
    work_dir: Path,
    bounds: tuple[float, float, float, float],
    *,
    resolution_m: float = DEFAULT_RESOLUTION_M,
    log=None,
) -> JobGrid:
    grid = build_job_grid(bounds, resolution_m=resolution_m)
    json_path, pgw_path = grid.write(work_dir)
    if log:
        log(
            f"Kanonická mřížka {grid.crs}: "
            f"[{grid.xmin:.1f},{grid.xmax:.1f}]×[{grid.ymin:.1f},{grid.ymax:.1f}] "
            f"{grid.width}×{grid.height} px @ {grid.resolution_m:g} m "
            f"→ {json_path.name}, {pgw_path.name}"
        )
    return grid


def resolve_job_extent(
    work_dir: Path,
    *,
    crop_bounds: tuple[float, float, float, float] | None = None,
) -> tuple[float, float, float, float]:
    """Extent jobu: kanonická job_grid, jinak ``crop_bounds``."""
    grid = JobGrid.load(work_dir)
    if grid is not None:
        return grid.bounds()

    if crop_bounds is not None:
        return crop_bounds

    raise RuntimeError(
        "Chybí extent jobu (job_grid.json nebo crop_bounds)"
    )
