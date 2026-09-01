from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from app import settings
from app.download_cache import (
    is_fresh,
    lidar_sheet_dir,
    read_meta,
    write_meta,
)

KLAD_QUERY_URL = (
    "https://ags.cuzk.gov.cz/arcgis/rest/services/"
    "KladyMapovychListu/MapServer/24/query"
)
OPENZU_DMR = "https://openzu.cuzk.gov.cz/opendata/DMR5G/epsg-5514/{mapnom}.zip"
OPENZU_DMPOK = "https://openzu.cuzk.gov.cz/opendata/DMPOK-LAZ/epsg-5514/{mapnom}.zip"
OPENZU_DMP1G = "https://openzu.cuzk.gov.cz/opendata/DMP1G/epsg-5514/{mapnom}.zip"
USER_AGENT = "Podkladarna/1.2 (https://github.com/pjanecekATlangmaster/podkladarna)"
MAX_SHEETS = 8
MAX_BBOX_KM = 5.0
CROP_BUFFER_M = 30.0
QUERY_TIMEOUT_S = 30
DOWNLOAD_TIMEOUT_S = 180

# Obdélník Česka + cca 15 km (listy SM5 u hranic).
CZ_WEST = 11.85
CZ_SOUTH = 48.35
CZ_EAST = 19.10
CZ_NORTH = 51.25


class FetchError(RuntimeError):
    pass


def parse_bbox(raw: str) -> tuple[float, float, float, float]:
    """Očekává west,south,east,north (WGS84)."""
    parts = [p.strip() for p in (raw or "").split(",")]
    if len(parts) != 4:
        raise FetchError("Bbox musí mít 4 čísla: west,south,east,north")
    try:
        west, south, east, north = (float(p) for p in parts)
    except ValueError as exc:
        raise FetchError("Bbox obsahuje nečíselnou hodnotu") from exc
    if west >= east or south >= north:
        raise FetchError("Bbox je invertedý – west < east a south < north")
    if (
        west < CZ_WEST
        or east > CZ_EAST
        or south < CZ_SOUTH
        or north > CZ_NORTH
    ):
        raise FetchError("Bbox je mimo Česko")
    return west, south, east, north


def bbox_size_km(
    west: float, south: float, east: float, north: float
) -> tuple[float, float]:
    """Šířka a výška výřezu v km (S-JTSK, bez crop bufferu)."""
    xmin, ymin, xmax, ymax = crop_bounds_5514(west, south, east, north, buffer_m=0)
    return (xmax - xmin) / 1000.0, (ymax - ymin) / 1000.0


def bbox_exceeds_limit(west: float, south: float, east: float, north: float) -> bool:
    width_km, height_km = bbox_size_km(west, south, east, north)
    return width_km > MAX_BBOX_KM or height_km > MAX_BBOX_KM


def estimate_minutes(sheet_count: int) -> int:
    """Hrubý odhad. Listy SM5 jsou v cache, Karttapullautin u sprintu běží jednotky minut."""
    n = max(sheet_count, 1)
    return max(2, 1 + n)


def query_sm5_sheets(west: float, south: float, east: float, north: float) -> list[dict]:
    params = {
        "geometry": f"{west},{south},{east},{north}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "MAPNOM,MAPNAME",
        "returnGeometry": "false",
        "f": "json",
    }
    url = KLAD_QUERY_URL + "?" + urllib.parse.urlencode(params)
    data = _http_json(url, timeout=QUERY_TIMEOUT_S)
    if data.get("error"):
        raise FetchError(f"ArcGIS klad SM5: {data['error']}")
    sheets: list[dict] = []
    seen: set[str] = set()
    for feat in data.get("features") or []:
        attrs = feat.get("attributes") or {}
        mapnom = (attrs.get("MAPNOM") or "").strip().upper()
        if not mapnom or mapnom in seen:
            continue
        seen.add(mapnom)
        sheets.append({"mapnom": mapnom, "name": (attrs.get("MAPNAME") or mapnom).strip()})
    sheets.sort(key=lambda s: s["mapnom"])
    return sheets


def wgs84_to_5514(lon: float, lat: float) -> tuple[float, float]:
    from pyproj import Transformer

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:5514", always_xy=True)
    x, y = transformer.transform(lon, lat)
    return float(x), float(y)


def crop_bounds_5514(
    west: float, south: float, east: float, north: float, buffer_m: float = CROP_BUFFER_M
) -> tuple[float, float, float, float]:
    corners = [
        wgs84_to_5514(west, south),
        wgs84_to_5514(east, south),
        wgs84_to_5514(east, north),
        wgs84_to_5514(west, north),
    ]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return (
        min(xs) - buffer_m,
        min(ys) - buffer_m,
        max(xs) + buffer_m,
        max(ys) + buffer_m,
    )


def fetch_lidar_for_bbox(
    bbox: tuple[float, float, float, float],
    log: callable | None = None,
) -> tuple[list[Path], list[Path], list[str]]:
    """Vrátí cesty k LAZ v sdílené cache (stáhne jen chybějící / zastaralé listy)."""
    west, south, east, north = bbox
    if bbox_exceeds_limit(west, south, east, north):
        width_km, height_km = bbox_size_km(west, south, east, north)
        raise FetchError(
            f"Výřez je moc velký ({width_km:.1f} × {height_km:.1f} km, "
            f"max {MAX_BBOX_KM:.0f} × {MAX_BBOX_KM:.0f} km)."
        )
    sheets = query_sm5_sheets(west, south, east, north)
    if not sheets:
        raise FetchError("Výřez neprotíná žádný list SM5")
    if len(sheets) > MAX_SHEETS:
        names = ", ".join(s["mapnom"] for s in sheets)
        raise FetchError(
            f"Výřez je moc velký ({len(sheets)} listů SM5, max {MAX_SHEETS}): {names}"
        )

    names = [s["mapnom"] for s in sheets]
    if log:
        log(f"Protíná listy: {', '.join(names)} ({len(names)})")

    dmr_paths: list[Path] = []
    dmp_paths: list[Path] = []
    for mapnom in names:
        dmr = _cached_laz(mapnom, "DMR5G", OPENZU_DMR.format(mapnom=mapnom), log)
        dmp = _cached_dmp_laz(mapnom, log)
        dmr_paths.append(dmr)
        dmp_paths.append(dmp)
    return dmr_paths, dmp_paths, names


def _cached_dmp_laz(mapnom: str, log: callable | None) -> Path:
    """Model povrchu: primárně DMP OK (obrazová korelace), záloha DMP 1G."""
    folder = lidar_sheet_dir(mapnom)
    dmpok = folder / "DMPOK.laz"
    if is_fresh(folder, dmpok, settings.LIDAR_CACHE_MAX_AGE_DAYS):
        if log:
            meta = read_meta(folder) or {}
            age = meta.get("downloaded_at", "?")[:10]
            log(f"LiDAR cache {mapnom} DMPOK (staženo {age})")
        return dmpok

    try:
        return _cached_laz(
            mapnom, "DMPOK", OPENZU_DMPOK.format(mapnom=mapnom), log
        )
    except FetchError as exc:
        dmp1g = folder / "DMP1G.laz"
        if is_fresh(folder, dmp1g, settings.LIDAR_CACHE_MAX_AGE_DAYS):
            if log:
                log(
                    f"DMP OK {mapnom} nedostupný ({exc}) – používám cache DMP 1G"
                )
            return dmp1g
        if log:
            log(f"DMP OK {mapnom} nedostupný ({exc}) – zkouším DMP 1G …")
        return _cached_laz(
            mapnom, "DMP1G", OPENZU_DMP1G.format(mapnom=mapnom), log
        )


def _cached_laz(mapnom: str, kind: str, url: str, log: callable | None) -> Path:
    folder = lidar_sheet_dir(mapnom)
    folder.mkdir(parents=True, exist_ok=True)
    laz = folder / f"{kind}.laz"
    if is_fresh(folder, laz, settings.LIDAR_CACHE_MAX_AGE_DAYS):
        if log:
            meta = read_meta(folder) or {}
            age = meta.get("downloaded_at", "?")[:10]
            log(f"LiDAR cache {mapnom} {kind} (staženo {age})")
        return laz

    zpath = folder / f"{kind}.zip"
    if log:
        log(f"Stahuji {kind} {mapnom} …")
    _http_download(url, zpath)
    _extract_laz(zpath, laz)
    write_meta(folder, kind=kind, mapnom=mapnom, url=url, source="openzu")
    if log:
        log(f"OK {laz.name} ({laz.stat().st_size / 1e6:.1f} MB)")
    return laz


def _extract_laz(zpath: Path, dest_laz: Path) -> None:
    with zipfile.ZipFile(zpath) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith((".laz", ".las"))]
        if not names:
            raise FetchError(f"ZIP {zpath.name} neobsahuje LAZ/LAS")
        names.sort(key=lambda n: (0 if n.lower().endswith(".laz") else 1, len(n)))
        with zf.open(names[0]) as src, dest_laz.open("wb") as out:
            shutil.copyfileobj(src, out)
    if dest_laz.stat().st_size < 1000:
        dest_laz.unlink(missing_ok=True)
        raise FetchError(f"Rozbalený {dest_laz.name} je podezřele malý")


def _request(url: str, timeout: int) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def _http_json(url: str, timeout: int) -> dict:
    try:
        with urllib.request.urlopen(_request(url, timeout), timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} při dotazu na klad SM5") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"Síťová chyba (klad SM5): {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FetchError("ArcGIS vrátil neplatný JSON") from exc


def _http_download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(_request(url, DOWNLOAD_TIMEOUT_S), timeout=DOWNLOAD_TIMEOUT_S) as resp:
            if getattr(resp, "status", 200) >= 400:
                raise FetchError(f"Stažení selhalo HTTP {resp.status}: {url}")
            with tmp.open("wb") as out:
                shutil.copyfileobj(resp, out)
    except urllib.error.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        raise FetchError(f"HTTP {exc.code} při stahování {url}") from exc
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        raise FetchError(f"Síťová chyba při stahování: {exc.reason}") from exc
    tmp.replace(dest)
    if dest.stat().st_size < 1000:
        dest.unlink(missing_ok=True)
        raise FetchError(f"Stažený soubor je podezřele malý: {url}")
