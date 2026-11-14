"""Souřadnice jobu pro OOM a GDAL: S-JTSK / Krovak (stejný PROJ.4 jako OOM issue #542), jinde EPSG země.

Zahraniční build: proces jobu dostane souřadnice země výřezu v ``PODKLADARNA_JOB_CRS`` (nastaví
``app.worker`` při spuštění jobu z ``crs`` v ``configs/countries.yaml``) a konstanty níže se spočtou při
importu. Bez proměnné (web, Česko, starší joby) je to S-JTSK s vlastním TOWGS84 jako dosud.
"""

from __future__ import annotations

import os
from pathlib import Path

JOB_CRS_ENV = "PODKLADARNA_JOB_CRS"
KROVAK_LABEL = "EPSG:5514"

# OOM používá vlastní +towgs84 pro EPSG:5514. Holé „EPSG:5514“ vede
# k jiné transformaci než u GDAL/ČÚZK a posunu vektorů vůči georeferencovaným PNG.
KROVAK_PROJ4 = (
    "+proj=krovak +lat_0=49.5 +lon_0=24.83333333333333 "
    "+alpha=30.28813972222222 +k=0.9999 +x_0=0 +y_0=0 +ellps=bessel "
    "+towgs84=542.5,89.2,456.9,5.517,2.275,5.516,6.96 +pm=greenwich +units=m +no_defs"
)
GEOGRAPHIC_CRS_PROJ4 = "+proj=latlong +datum=WGS84"

# .prj pro shapefile. Musí nést stejný TOWGS84 jako CRS_PROJ4, jinak OOM při
# importu převede datum a vektory sedí ~0,3 m vedle georeferencovaného PNG.
# Bez AUTHORITY["EPSG","5514"]: s ním PROJ použije oficiální datum a TOWGS84
# zahodí, takže by se posun vrátil.
KROVAK_WKT = (
    'PROJCS["S-JTSK / Krovak East North",'
    'GEOGCS["S-JTSK",'
    'DATUM["System_Jednotne_Trigonometricke_Site_Katastralni",'
    'SPHEROID["Bessel 1841",6377397.155,299.1528128],'
    "TOWGS84[542.5,89.2,456.9,5.517,2.275,5.516,6.96]],"
    'PRIMEM["Greenwich",0],'
    'UNIT["degree",0.0174532925199433]],'
    'PROJECTION["Krovak"],'
    'PARAMETER["latitude_of_center",49.5],'
    'PARAMETER["longitude_of_center",24.83333333333333],'
    'PARAMETER["azimuth",30.28813972222222],'
    'PARAMETER["pseudo_standard_parallel_1",78.5],'
    'PARAMETER["scale_factor",0.9999],'
    'PARAMETER["false_easting",0],'
    'PARAMETER["false_northing",0],'
    'UNIT["metre",1],'
    'AXIS["Easting",EAST],'
    'AXIS["Northing",NORTH]]'
)


def job_crs_label() -> str:
    """``EPSG:<kód>`` souřadnic jobu z prostředí procesu (výchozí S-JTSK)."""
    label = (os.environ.get(JOB_CRS_ENV) or KROVAK_LABEL).strip().upper()
    return label if label.startswith("EPSG:") and label[5:].isdigit() else KROVAK_LABEL


def crs_definitions(label: str) -> tuple[str, str]:
    """(PROJ pro GDAL / pyproj / OOM, WKT pro .prj). S-JTSK s vlastním TOWGS84, jinde holé EPSG země:
    OOM i GDAL pak použijí stejnou transformaci z PROJ."""
    if label == KROVAK_LABEL:
        return KROVAK_PROJ4, KROVAK_WKT
    from pyproj import CRS

    return label, CRS.from_user_input(label).to_wkt("WKT1_GDAL")


CRS_LABEL = job_crs_label()
CRS_EPSG = int(CRS_LABEL.split(":", 1)[1])
CRS_PROJ4, CRS_WKT = crs_definitions(CRS_LABEL)
IS_KROVAK = CRS_LABEL == KROVAK_LABEL


def write_prj(shp: Path) -> Path:
    """Přepíše .prj shapefilu na definici shodnou s .omap (souřadnice nemění)."""
    prj = shp.with_suffix(".prj")
    prj.write_text(CRS_WKT + "\n", encoding="utf-8")
    return prj


def wgs84_to_projected(lat: float, lon: float) -> tuple[float, float]:
    from pyproj import Transformer

    transformer = Transformer.from_crs(
        GEOGRAPHIC_CRS_PROJ4,
        CRS_PROJ4,
        always_xy=True,
    )
    x, y = transformer.transform(lon, lat)
    return float(x), float(y)


def projected_to_wgs84(x: float, y: float) -> tuple[float, float]:
    from pyproj import Transformer

    transformer = Transformer.from_crs(
        CRS_PROJ4,
        GEOGRAPHIC_CRS_PROJ4,
        always_xy=True,
    )
    lon, lat = transformer.transform(x, y)
    return float(lat), float(lon)
