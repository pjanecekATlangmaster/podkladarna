from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from app import settings

KLAD_QUERY_URL = (
    "https://ags.cuzk.gov.cz/arcgis/rest/services/"
    "KladyMapovychListu/MapServer/24/query"
)
OPENZU_DMR = "https://openzu.cuzk.gov.cz/opendata/DMR5G/epsg-5514/{mapnom}.zip"
OPENZU_DMP = "https://openzu.cuzk.gov.cz/opendata/DMP1G/epsg-5514/{mapnom}.zip"
USER_AGENT = "Podkladarna/1.1 (https://github.com/pjanecekATlangmaster/podkladarna)"
MAX_SHEETS = 8
CROP_BUFFER_M = 30.0
QUERY_TIMEOUT_S = 30
DOWNLOAD_TIMEOUT_S = 180


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
    if not (11.0 <= west <= 19.5 and 11.0 <= east <= 19.5):
        raise FetchError("Bbox je mimo Česko (zeměpisná délka)")
    if not (48.0 <= south <= 51.6 and 48.0 <= north <= 51.6):
        raise FetchError("Bbox je mimo Česko (zeměpisná šířka)")
    return west, south, east, north


def estimate_minutes(sheet_count: int) -> int:
    return 5 + max(sheet_count, 1) * 4


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
    dest_dmr: Path,
    dest_dmp: Path,
    log: callable | None = None,
) -> list[str]:
    west, south, east, north = bbox
    sheets = query_sm5_sheets(west, south, east, north)
    if not sheets:
        raise FetchError("Výřez neprotíná žádný list SM5")
    if len(sheets) > MAX_SHEETS:
        names = ", ".join(s["mapnom"] for s in sheets)
        raise FetchError(
            f"Výřez je moc velký ({len(sheets)} listů SM5, max {MAX_SHEETS}): {names}"
        )

    dest_dmr.mkdir(parents=True, exist_ok=True)
    dest_dmp.mkdir(parents=True, exist_ok=True)
    names = [s["mapnom"] for s in sheets]
    if log:
        log(f"Protíná listy: {', '.join(names)} ({len(names)})")

    for mapnom in names:
        dmr = _cached_laz(mapnom, "DMR5G", OPENZU_DMR.format(mapnom=mapnom), log)
        dmp = _cached_laz(mapnom, "DMP1G", OPENZU_DMP.format(mapnom=mapnom), log)
        _link_or_copy(dmr, dest_dmr / f"{mapnom}_dmr.laz")
        _link_or_copy(dmp, dest_dmp / f"{mapnom}_dmp.laz")
    return names


def _cached_laz(mapnom: str, kind: str, url: str, log: callable | None) -> Path:
    folder = settings.CACHE_DIR / "sm5" / mapnom
    folder.mkdir(parents=True, exist_ok=True)
    laz = folder / f"{kind}.laz"
    if laz.exists() and laz.stat().st_size > 1000:
        if log:
            log(f"Cache hit {mapnom} {kind}")
        return laz

    zpath = folder / f"{kind}.zip"
    if log:
        log(f"Stahuji {kind} {mapnom} …")
    _http_download(url, zpath)
    _extract_laz(zpath, laz)
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


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


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
