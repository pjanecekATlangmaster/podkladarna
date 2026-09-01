"""S-JTSK / Krovak pro OOM a GDAL (stejný PROJ.4 jako OOM issue #542)."""

from __future__ import annotations

# OOM používá vlastní +towgs84 pro EPSG:5514. Holé „EPSG:5514“ vede
# k jiné transformaci než u GDAL/ČÚZK a posunu vektorů vůči georeferencovaným PNG.
CRS_PROJ4 = (
    "+proj=krovak +lat_0=49.5 +lon_0=24.83333333333333 "
    "+alpha=30.28813972222222 +k=0.9999 +x_0=0 +y_0=0 +ellps=bessel "
    "+towgs84=542.5,89.2,456.9,5.517,2.275,5.516,6.96 +pm=greenwich +units=m +no_defs"
)
CRS_LABEL = "EPSG:5514"
GEOGRAPHIC_CRS_PROJ4 = "+proj=latlong +datum=WGS84"


def projected_to_wgs84(x: float, y: float) -> tuple[float, float]:
    from pyproj import Transformer

    transformer = Transformer.from_crs(
        CRS_PROJ4,
        GEOGRAPHIC_CRS_PROJ4,
        always_xy=True,
    )
    lon, lat = transformer.transform(x, y)
    return float(lat), float(lon)
