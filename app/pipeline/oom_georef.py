"""Georeferencování .omap kompatibilní s OpenOrienteering Mapper."""

from __future__ import annotations

import math

from app.pipeline.crs_5514 import CRS_PROJ4, projected_to_wgs84


def _round_declination(value: float) -> float:
    """Stejné zaokrouhlení jako OOM (2 desetinná místa)."""
    return math.floor(value * 100 + 0.5) / 100


def magnetic_declination_deg(lat: float, lon: float) -> float:
    """Magnetická deklinace (°) dle WMM-2025 – stejný model jako ČSOS kalkulátor."""
    from almanac.geomag import compute

    result = compute(lat, lon, altitude_km=0.0, when=None)
    return float(result["declination_deg"])


def grid_convergence_deg(ref_x: float, ref_y: float, crs_proj4: str = CRS_PROJ4) -> float:
    """Úhel mezi severem sítě a geografickým severem v ref. bodě (algoritmus OOM)."""
    from pyproj import CRS, Transformer

    crs = CRS.from_proj4(crs_proj4)
    wgs84 = CRS.from_epsg(4326)
    to_geo = Transformer.from_crs(crs, wgs84, always_xy=True)
    lon0, lat0 = to_geo.transform(ref_x, ref_y)

    local = CRS.from_proj4(
        f"+proj=sterea +lat_0={lat0} +lon_0={lon0} +ellps=WGS84 +units=m +no_defs"
    )
    local_to_geo = Transformer.from_crs(local, wgs84, always_xy=True)
    geo_to_crs = Transformer.from_crs(wgs84, crs, always_xy=True)

    delta = 1000.0

    def crs_from_local(dx: float, dy: float) -> tuple[float, float]:
        lon, lat = local_to_geo.transform(dx, dy)
        return geo_to_crs.transform(lon, lat)

    ex, ey = crs_from_local(delta / 2, 0)
    wx, wy = crs_from_local(-delta / 2, 0)
    nx, ny = crs_from_local(0, delta / 2)
    sx, sy = crs_from_local(0, -delta / 2)

    d_easting_dx = (ex - wx) / delta
    d_northing_dx = (ey - wy) / delta
    d_easting_dy = (nx - sx) / delta
    d_northing_dy = (ny - sy) / delta

    determinant = d_easting_dx * d_northing_dy - d_northing_dx * d_easting_dy
    if determinant < 1e-11:
        return 0.0
    return math.degrees(
        math.atan2(d_northing_dx - d_easting_dy, d_easting_dx + d_northing_dy)
    )


def oom_north_angles(ref_x: float, ref_y: float) -> tuple[float, float]:
    """Magnetická deklinace a grivace S-JTSK pro .omap.

    PNG z Karttapullautinu je v severu sítě (S-JTSK). OOM ale potřebuje oba úhly:
    declination − grivation = konvergence (viz OOM a kalkulátor ČSOS).
  """
    lat, lon = projected_to_wgs84(ref_x, ref_y)
    declination = _round_declination(magnetic_declination_deg(lat, lon))
    convergence = grid_convergence_deg(ref_x, ref_y)
    grivation = _round_declination(declination - convergence)
    return declination, grivation
