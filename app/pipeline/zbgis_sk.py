"""Slovensko: budovy a šířky silnic z INSPIRE sad ÚGKK (odvozené ze ZBGIS) jako obdoba české DTM pro kresbu.

Plný ZBGIS je placený (objednávka), zdarma pod CC BY 4.0 jsou celostátní INSPIRE sady na opendata.skgeodesy.sk:
budovy (``Building``, obrysy ZBGIS 2017–2021) a dopravní síť (``RoadLink`` s šířkou ``RoadWidth`` a druhem
``FormOfWay``). Každá je jeden ZIP s GeoPackage (EPSG:4258, ~0,6 GB), výřez přes službu nejde.

ZIP se stáhne při prvním slovenském jobu do běžné cache (``cache/sk_zbgis/<sada>/``), rozbalí a dál se čte výřezem
přes prostorový index GeoPackage. Jednou za ``VERSION_CHECK_DAYS`` se hlavičkou (Last-Modified + velikost) ověří
novější vydání. Cache patří k cache podle výřezu (``disk_space.aoi_cache_roots``): nepoužitá se po
``SK_ZBGIS_CACHE_MAX_AGE_DAYS`` smaže a při nedostatku místa jde mezi prvními, další job si ji stáhne znovu.

Kreslí se budovy (ne zbořené a chátrající) a vozovky: osa silnice se známou šířkou (ne polní cesty) rozšířená
na plochu vozovky do ``roads``, odkud ji bere volba symbolu podle šířky a koridory jako v Česku. Ploty, chodníky,
zahrady a plochy využití sady nemají, ty kreslí OSM. Výsledek jde do ``work_dir/dtm`` stejně jako BGT nebo ETAK.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from app.pipeline import build_stats

BASE_URL = "https://opendata.skgeodesy.sk/static/INSPIRE"
DATASETS: dict[str, tuple[str, str]] = {  # sada → (URL ZIPu, GeoPackage uvnitř)
    "bu": (f"{BASE_URL}/Buildings/inspire_bu_gpkg.zip", "INSPIRE_BU.gpkg"),
    "tn": (f"{BASE_URL}/Transport_networks/inspire_tn_gpkg.zip", "INSPIRE_TN.gpkg"),
}
CACHE_SUBDIR = "sk_zbgis"
META_NAME = "meta.json"
SOURCE_SRID = 4258
SOURCE_CRS = "EPSG:4258"
SOURCE_LABEL = "INSPIRE ZBGIS (ÚGKK SR)"
VERSION_CHECK_DAYS = 30
HTTP_TIMEOUT_S = 120
CHUNK = 1 << 20
MAX_ZIP_BYTES = 5_000_000_000  # dnes 0,6 GB; víc je chyba serveru

# INSPIRE kódy (konec URI codelistu)
SKIP_CONDITIONS = frozenset({"demolished", "declined", "ruin"})  # zbořené a chátrající budovy se nekreslí
SKIP_FORMS = frozenset({"tractorRoad"})  # polní a lesní cesty kreslí OSM (pás „silnice z DTM“ by mazal pěšiny)
ROAD_MIN_WIDTH_M = 2.5
ROAD_MAX_WIDTH_M = 40.0
TN_INDEXES = (("RoadWidth", "networkRef"), ("FormOfWay", "networkRef"))

LogFn = Callable[[str], None]


def cache_dir(key: str) -> Path:
    from app import settings

    return Path(settings.DOWNLOADS_DIR) / CACHE_SUBDIR / key


def _read_meta(folder: Path) -> dict:
    try:
        return json.loads((folder / META_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_meta(folder: Path, meta: dict) -> None:
    tmp = folder / (META_NAME + ".tmp")
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(tmp, folder / META_NAME)


def _head(url: str) -> dict:
    from app.pipeline.fetch_openzu import USER_AGENT

    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return {"last_modified": resp.headers.get("Last-Modified") or "", "size": int(resp.headers.get("Content-Length") or 0)}


def _download(url: str, dest: Path) -> int:
    from app.pipeline.fetch_openzu import USER_AGENT

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    total = 0
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ZIP_BYTES:
                raise RuntimeError(f"ZBGIS: soubor větší než {MAX_ZIP_BYTES // 1_000_000_000} GB")
            out.write(chunk)
    return total


def _add_indexes(gpkg: Path) -> None:
    """Indexy pro spojení os silnic se šířkou a druhem (v sadě nejsou; bez nich výřez prochází statisíce řádků)."""
    con = sqlite3.connect(gpkg)
    try:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        for table, column in TN_INDEXES:
            if table in tables:
                con.execute(f'create index if not exists "idx_{table}_{column}" on "{table}"("{column}")')
        con.commit()
    finally:
        con.close()


def _install(key: str, folder: Path, remote: dict, *, now: float, download=_download, log: LogFn | None = None) -> Path:
    url, member = DATASETS[key]
    folder.mkdir(parents=True, exist_ok=True)
    zip_path = folder / "download.zip.part"
    tmp_gpkg = folder / (member + ".tmp")
    t0 = time.monotonic()
    try:
        nbytes = download(url, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            with zf.open(member) as src, open(tmp_gpkg, "wb") as out:
                shutil.copyfileobj(src, out, CHUNK)
        zip_path.unlink(missing_ok=True)
        if key == "tn":
            _add_indexes(tmp_gpkg)
        os.replace(tmp_gpkg, folder / member)
    finally:
        zip_path.unlink(missing_ok=True)
        tmp_gpkg.unlink(missing_ok=True)
    _write_meta(folder, {**remote, "checked": now, "url": url})
    build_stats.note_bytes("sk_zbgis", nbytes, cached=False, key=f"zbgis_{key}")
    if log:
        log(f"ZBGIS {key}: staženo {nbytes / 1e6:.0f} MB do cache ({time.monotonic() - t0:.0f} s)")
    return folder / member


def ensure_dataset(key: str, *, log: LogFn | None = None, head=_head, download=_download, now: float | None = None) -> Path:
    """GeoPackage sady v cache (stáhne při první potřebě, jednou za ``VERSION_CHECK_DAYS`` ověří novější vydání)."""
    from app.slot_lock import file_lock

    url, member = DATASETS[key]
    folder = cache_dir(key)
    gpkg = folder / member
    now = time.time() if now is None else now
    with file_lock(folder / ".lock"):
        meta = _read_meta(folder) if gpkg.is_file() else {}
        if meta and now - float(meta.get("checked") or 0) < VERSION_CHECK_DAYS * 86400:
            _write_meta(folder, meta)  # čas použití pro úklid cache
            return gpkg
        try:
            remote = head(url)
        except Exception as exc:  # server nedostupný: starší data jsou lepší než žádná
            if gpkg.is_file():
                if log:
                    log(f"ZBGIS {key}: kontrola novější verze selhala ({type(exc).__name__}), použita data z cache")
                return gpkg
            raise
        if meta and remote.get("last_modified") == meta.get("last_modified") and remote.get("size") == meta.get("size"):
            _write_meta(folder, {**meta, "checked": now})
            return gpkg
        if log and meta:
            log(f"ZBGIS {key}: ÚGKK vydal novější verzi, stahuji znovu")
        return _install(key, folder, remote, now=now, download=download, log=log)


def gpkg_wkb(blob: bytes) -> bytes:
    """WKB z geometrie GeoPackage (hlavička „GP“ + obálka podle příznaků)."""
    if not blob or blob[:2] != b"GP":
        raise ValueError("není geometrie GeoPackage")
    flags = blob[3]
    envelope = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((flags >> 1) & 0x07)
    if envelope is None:
        raise ValueError("neznámá obálka GeoPackage")
    return blob[8 + envelope:]


def _bbox_rows(con: sqlite3.Connection, table: str, columns: str, bbox, join: str = "") -> list[tuple]:
    west, south, east, north = bbox
    sql = (
        f'select t.geometry{columns} from "{table}" t {join} '
        f'where t.fid in (select id from "rtree_{table}_geometry" where maxx >= ? and minx <= ? and maxy >= ? and miny <= ?)'
    )
    return con.execute(sql, (west, east, south, north)).fetchall()


def _code(uri) -> str:
    return str(uri or "").rstrip("/").rsplit("/", 1)[-1]


def read_buildings(gpkg: Path, bbox_4258) -> list[dict]:
    """Obrysy budov ve výřezu jako GeoJSON objekty v EPSG:4258 (bez zbořených, chátrajících a bodů)."""
    from shapely import wkb
    from shapely.geometry import mapping

    con = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    try:
        rows = _bbox_rows(con, "Building", ", t.conditionOfConstruction, t.localId", bbox_4258)
    finally:
        con.close()
    out = []
    for blob, condition, local_id in rows:
        if _code(condition) in SKIP_CONDITIONS or not blob:
            continue
        geom = wkb.loads(gpkg_wkb(blob))
        if geom.geom_type not in ("Polygon", "MultiPolygon") or geom.is_empty:
            continue
        out.append({"type": "Feature", "geometry": mapping(geom), "properties": {"id": local_id}})
    return out


def read_roads(gpkg: Path, bbox_4258) -> list[dict]:
    """Osy silnic ve výřezu se šířkou (m) a druhem jako GeoJSON objekty v EPSG:4258."""
    from shapely import wkb
    from shapely.geometry import mapping

    con = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    try:
        rows = _bbox_rows(
            con, "RoadLink", ", w.width, f.formOfWay", bbox_4258,
            join='left join "RoadWidth" w on w.networkRef = t.id left join "FormOfWay" f on f.networkRef = t.id',
        )
    finally:
        con.close()
    out = []
    for blob, width, form in rows:
        if not blob:
            continue
        geom = wkb.loads(gpkg_wkb(blob))
        if geom.geom_type not in ("LineString", "MultiLineString") or geom.is_empty:
            continue
        out.append({"type": "Feature", "geometry": mapping(geom), "properties": {"width": width, "form": _code(form)}})
    return out


def road_polygons(roads: list[dict]) -> list:
    """Plochy vozovek: osa se známou šířkou (ne polní cesty) rozšířená o půl šířky, sloučená do souvislých ploch."""
    from shapely.geometry import shape
    from shapely.ops import unary_union

    from app.pipeline.bgt_nl import clean_polygons

    parts = []
    for f in roads:
        props = f.get("properties") or {}
        try:
            width = float(props.get("width") or 0)
        except (TypeError, ValueError):
            continue
        if props.get("form") in SKIP_FORMS or not ROAD_MIN_WIDTH_M <= width <= ROAD_MAX_WIDTH_M:
            continue
        parts.append(shape(f["geometry"]).buffer(width / 2.0, cap_style="flat", join_style="mitre", mitre_limit=2.0))
    if not parts:
        return []
    merged = unary_union(parts)
    rings = [
        [list(p.exterior.coords)] + [list(r.coords) for r in p.interiors]
        for p in getattr(merged, "geoms", [merged])
        if p.geom_type == "Polygon" and not p.is_empty
    ]
    return clean_polygons(rings)


LOG_LABELS = {"buildings": "budovy", "roads": "vozovky ze šířky silnic"}


def prepare(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log: LogFn | None = None,
    force: bool = False,  # noqa: ARG001 – stejný podpis jako příprava DTM
) -> Path | None:
    """Budovy a vozovky ZBGIS pro výřez → soubory ``work_dir/dtm``; None, když ve výřezu nic není."""
    from pyproj import Transformer

    from app.pipeline.bgt_nl import clean_polygons, feature_polygons, write_dtm_files
    from app.pipeline.crs_5514 import CRS_LABEL
    from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514
    from app.pipeline.fmzk_at import reproject_features

    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    box = Transformer.from_crs(CRS_LABEL, SOURCE_CRS, always_xy=True).transform_bounds(*bounds, densify_pts=21)
    t0 = time.monotonic()
    build_stats.note_background("dtm", source="sk_zbgis")
    data = {
        "buildings": read_buildings(ensure_dataset("bu", log=log), box),
        "roads": read_roads(ensure_dataset("tn", log=log), box),
    }
    reproject_features(data, SOURCE_CRS, CRS_LABEL)
    count = sum(len(v) for v in data.values())
    build_stats.note_background("dtm", packages=len(DATASETS), regions=["SK"], source="sk_zbgis", objects=count)
    if log:
        log(f"ZBGIS: {len(data['buildings'])} budov a {len(data['roads'])} úseků silnic ({time.monotonic() - t0:.0f} s)")
    if not count:
        if log:
            log("ZBGIS: ve výřezu nic, kresba z OSM.")
        return None
    buildings = clean_polygons([p for f in data["buildings"] for p in feature_polygons(f)])
    roads = road_polygons(data["roads"])
    sorted_data = {"buildings": buildings, "roads": roads, "public": list(roads)}
    return write_dtm_files(
        work_dir, sorted_data, bounds, log=log, source="sk_zbgis", label="ZBGIS", labels=LOG_LABELS,
        yard_label="zahrady (v sadě nejsou)",
    )
