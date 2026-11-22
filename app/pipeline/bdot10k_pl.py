"""Polsko: BDOT10k (GUGiK) jako obdoba české DTM pro kresbu, obdobně jako ZBGIS na Slovensku.

BDOT10k je celostátní topografická databáze 1:10 000 (zdarma k libovolnému využití, čl. 40a Prawo geodezyjne;
uvést „Źródło danych: GUGiK“). GUGiK ji dává jako ZIP GeoPackage po okresech
(``opendata.geoportal.gov.pl/bdot10k/schemat2021/GPKG/<vojvodství>/<TERYT>_GPKG.zip``, Krakov 35 MB). Okresy výřezu
najde služba ULDK GUGiK (okres v bodě i s obrysem), ZIP okresu se stáhne do cache a vybalí se z něj jen tři vrstvy:

- ``OT_BUBD_A`` budovy (bez zničených a rozestavěných),
- ``OT_SKJZ_L`` osy silnic se šířkou vozovky (``SZEROKOSCNAWIERZCHNI``) → plochy vozovek jako ``zbgis_sk.road_polygons``;
  nezpevněné (``grunt naturalny``, štěrk) a silnice pod zemí kreslí OSM,
- ``OT_PTPL_A`` zpevněná náměstí a plochy jako pěší plochy.

Chodníky ani ploty BDOT10k nemá (ty kreslí OSM); podrobná mapa BDOT500 je placená po okresech.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from app.pipeline import build_stats

BASE_URL = "https://opendata.geoportal.gov.pl/bdot10k/schemat2021/GPKG"
ULDK_URL = "https://uldk.gugik.gov.pl/"
SOURCE_EPSG = 2180  # PL-1992; ULDK i GeoPackage v pořadí východ, sever
SOURCE_CRS = f"EPSG:{SOURCE_EPSG}"
SOURCE_LABEL = "BDOT10k (GUGiK)"
CACHE_SUBDIR = "pl_bdot10k"
CACHE_MAX_AGE_DAYS = 90  # nepoužitý okres se smaže (``disk_space.aoi_cache_roots``)
META_NAME = "meta.json"
VERSION_CHECK_DAYS = 30
HTTP_TIMEOUT_S = 120
HTTP_RETRIES = 2
CHUNK = 1 << 20
MAX_ZIP_BYTES = 1_000_000_000  # dnes desítky MB na okres
MAX_COUNTIES = 12  # výřez přes víc okresů je chyba (ochrana smyčky ULDK)
LAYERS = ("OT_BUBD_A", "OT_SKJZ_L", "OT_PTPL_A")
PROBE_TERYT = "1261"  # Kraków

SKIP_BUILDINGS = frozenset({"zniszczony", "w budowie"})
UNPAVED = frozenset({"grunt naturalny", "żwir", "tłuczeń"})  # polní a štěrkové cesty kreslí OSM
UNDERGROUND = "pod powierzchnią gruntu"

LogFn = Callable[[str], None]


def package_url(teryt: str) -> str:
    return f"{BASE_URL}/{teryt[:2]}/{teryt}_GPKG.zip"


def probe_url() -> str:
    """Lehký dotaz pro stav zdrojů: hlavička ZIPu okresu Kraków."""
    return package_url(PROBE_TERYT)


def uldk_url(x: float, y: float) -> str:
    query = {"request": "GetCountyByXY", "xy": f"{x:.1f},{y:.1f},{SOURCE_EPSG}", "result": "teryt,geom_wkt", "srid": str(SOURCE_EPSG)}
    return f"{ULDK_URL}?{urllib.parse.urlencode(query)}"


def _http(url: str, method: str = "GET"):
    from app.pipeline.fetch_openzu import USER_AGENT

    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S)


def _get_text(url: str) -> str:
    last: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            with _http(url) as resp:
                return resp.read(20_000_000).decode("utf-8", "replace")
        except Exception as exc:  # síť / 5xx: krátce počkat a zkusit znovu
            last = exc
            if attempt < HTTP_RETRIES:
                time.sleep(2.0 * (attempt + 1))
    assert last is not None
    raise last


def parse_uldk(text: str):
    """Odpověď ULDK ``0\\n<teryt>|SRID=2180;MULTIPOLYGON(…)`` → (TERYT, shapely obrys), mimo Polsko None."""
    from shapely import wkt

    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    if len(lines) < 2 or lines[0] != "0" or "|" not in lines[1]:
        return None
    teryt, geom = lines[1].split("|", 1)
    teryt = teryt.strip()
    if not (len(teryt) == 4 and teryt.isdigit()):
        return None
    if ";" in geom:
        geom = geom.split(";", 1)[1]
    try:
        return teryt, wkt.loads(geom)
    except Exception:
        return teryt, None


def counties_for(box: tuple[float, float, float, float], fetch: Callable[[str], str] = _get_text) -> list[str]:
    """TERYT okresů, které pokrývají obálku (EPSG:2180): dotaz ULDK ve středu nepokryté části, dokud nezbude nic."""
    from shapely.geometry import box as shbox
    from shapely.ops import unary_union

    rest = shbox(*box)
    found: list[str] = []
    covered = []
    for _ in range(MAX_COUNTIES):
        if rest.is_empty or rest.area < 1.0:
            break
        pt = rest.representative_point()
        hit = parse_uldk(fetch(uldk_url(pt.x, pt.y)))
        if hit is None:  # bod mimo Polsko (moře, cizina): tu část vynechat
            rest = rest.difference(pt.buffer(max(50.0, (rest.area ** 0.5) / 20)))
            continue
        teryt, geom = hit
        if teryt not in found:
            found.append(teryt)
        if geom is None or geom.is_empty or not geom.covers(pt):
            break  # bez obrysu nejde pokračovat; stačí, co máme
        covered.append(geom)
        rest = rest.difference(unary_union(covered))
    return found


def cache_dir(teryt: str) -> Path:
    from app import settings

    return Path(settings.DOWNLOADS_DIR) / CACHE_SUBDIR / teryt


def _head(url: str) -> dict:
    with _http(url, "HEAD") as resp:
        return {"last_modified": resp.headers.get("Last-Modified") or "", "size": int(resp.headers.get("Content-Length") or 0)}


def _download(url: str, dest: Path) -> int:
    total = 0
    with _http(url) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ZIP_BYTES:
                raise RuntimeError(f"BDOT10k: soubor větší než {MAX_ZIP_BYTES // 1_000_000} MB")
            out.write(chunk)
    return total


def _read_meta(folder: Path) -> dict:
    try:
        return json.loads((folder / META_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_meta(folder: Path, meta: dict) -> None:
    tmp = folder / (META_NAME + ".tmp")
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(tmp, folder / META_NAME)


def _complete(folder: Path) -> bool:
    return all((folder / f"{layer}.gpkg").is_file() for layer in LAYERS)


def _install(teryt: str, folder: Path, remote: dict, *, now: float, download=_download, log: LogFn | None = None) -> Path:
    url = package_url(teryt)
    folder.mkdir(parents=True, exist_ok=True)
    zip_path = folder / "download.zip.part"
    t0 = time.monotonic()
    parts: list[Path] = []
    try:
        nbytes = download(url, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            for layer in LAYERS:
                member = next((n for n in names if n.endswith(f"__{layer}.gpkg")), None)
                tmp = folder / f"{layer}.gpkg.part"
                parts.append(tmp)
                if member is None:  # vrstva v okrese chybí (např. bez náměstí): prázdný soubor nevytvářet
                    continue
                with zf.open(member) as src, open(tmp, "wb") as out:
                    while True:
                        chunk = src.read(CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
        if not (folder / "OT_BUBD_A.gpkg.part").is_file():
            raise RuntimeError(f"BDOT10k {teryt}: v ZIPu nejsou budovy")
        for tmp in parts:
            final = folder / tmp.name.removesuffix(".part")
            if tmp.is_file():
                os.replace(tmp, final)
            else:
                final.unlink(missing_ok=True)
                _empty_layer(final)
    finally:
        zip_path.unlink(missing_ok=True)
        for tmp in parts:
            tmp.unlink(missing_ok=True)
    _write_meta(folder, {**remote, "checked": now, "url": url})
    build_stats.note_bytes("pl_bdot10k", nbytes, cached=False, key=f"bdot10k_{teryt}")
    if log:
        log(f"BDOT10k {teryt}: staženo {nbytes / 1e6:.0f} MB do cache ({time.monotonic() - t0:.0f} s)")
    return folder


def _empty_layer(path: Path) -> None:
    """Prázdná značka chybějící vrstvy (čtení ji přeskočí)."""
    path.write_bytes(b"")


def ensure_county(teryt: str, *, log: LogFn | None = None, head=_head, download=_download, now: float | None = None) -> Path:
    """Složka s vrstvami okresu v cache (stáhne při první potřebě, jednou za ``VERSION_CHECK_DAYS`` ověří novější)."""
    from app.slot_lock import file_lock

    folder = cache_dir(teryt)
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    url = package_url(teryt)
    with file_lock(folder / ".lock"):
        meta = _read_meta(folder) if _complete(folder) else {}
        if meta and now - float(meta.get("checked") or 0) < VERSION_CHECK_DAYS * 86400:
            _write_meta(folder, meta)  # čas použití pro úklid cache
            return folder
        try:
            remote = head(url)
        except Exception as exc:  # server nedostupný: starší data jsou lepší než žádná
            if meta:
                if log:
                    log(f"BDOT10k {teryt}: kontrola novější verze selhala ({type(exc).__name__}), použita data z cache")
                return folder
            raise
        if meta and remote.get("last_modified") == meta.get("last_modified") and remote.get("size") == meta.get("size"):
            _write_meta(folder, {**meta, "checked": now})
            return folder
        try:
            return _install(teryt, folder, remote, now=now, download=download, log=log)
        except Exception as exc:  # nová verze se nestáhla: starší data z cache, další pokus za den
            if not meta:
                raise
            if log:
                log(f"BDOT10k {teryt}: stažení novější verze selhalo ({type(exc).__name__}), použita data z cache")
            _write_meta(folder, {**meta, "checked": now - (VERSION_CHECK_DAYS - 1) * 86400})
            return folder


def _rows(gpkg: Path, table: str, columns: str, box) -> list[tuple]:
    if not gpkg.is_file() or gpkg.stat().st_size == 0:
        return []
    x0, y0, x1, y1 = box
    con = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    try:
        return con.execute(
            f'select t.geometria, t.LOKALNYID{columns} from "{table}" t where t.id in '
            f'(select id from "rtree_{table}_geometria" where maxx >= ? and minx <= ? and maxy >= ? and miny <= ?)',
            (x0, x1, y0, y1),
        ).fetchall()
    finally:
        con.close()


def read_county(folder: Path, box) -> dict[str, list[dict]]:
    """Budovy, silnice a náměstí okresu v obálce (EPSG:2180) jako GeoJSON objekty."""
    from shapely import force_2d, wkb
    from shapely.geometry import mapping

    from app.pipeline.zbgis_sk import gpkg_wkb

    def geom(blob):
        try:
            return force_2d(wkb.loads(gpkg_wkb(blob)))
        except Exception:  # vadná geometrie
            return None

    out: dict[str, list[dict]] = {"buildings": [], "roads": [], "squares": []}
    for blob, lid, state in _rows(folder / "OT_BUBD_A.gpkg", "OT_BUBD_A", ", t.KATEGORIAISTNIENIA", box):
        g = geom(blob) if blob and state not in SKIP_BUILDINGS else None
        if g is not None and g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty:
            out["buildings"].append({"type": "Feature", "geometry": mapping(g), "properties": {"id": lid}})
    cols = ", t.SZEROKOSCNAWIERZCHNI, t.MATERIALNAWIERZCHNI, t.POLOZENIE, t.KATEGORIAISTNIENIA"
    for blob, lid, width, material, position, state in _rows(folder / "OT_SKJZ_L.gpkg", "OT_SKJZ_L", cols, box):
        if material in UNPAVED or position == UNDERGROUND or state == "w budowie" or not blob:
            continue
        g = geom(blob)
        if g is not None and g.geom_type in ("LineString", "MultiLineString") and not g.is_empty:
            out["roads"].append({"type": "Feature", "geometry": mapping(g), "properties": {"id": lid, "width": width}})
    for blob, lid, material in _rows(folder / "OT_PTPL_A.gpkg", "OT_PTPL_A", ", t.MATERIALNAWIERZCHNI", box):
        if material in UNPAVED or not blob:
            continue
        g = geom(blob)
        if g is not None and g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty:
            out["squares"].append({"type": "Feature", "geometry": mapping(g), "properties": {"id": lid}})
    return out


def merge(parts: list[dict[str, list[dict]]]) -> dict[str, list[dict]]:
    """Spojí vrstvy více okresů (objekt na hranici okresů jen jednou, podle ``LOKALNYID``)."""
    out: dict[str, list[dict]] = {"buildings": [], "roads": [], "squares": []}
    seen: set[tuple[str, str]] = set()
    for part in parts:
        for key, feats in part.items():
            for f in feats:
                lid = str((f.get("properties") or {}).get("id") or "")
                if lid and (key, lid) in seen:
                    continue
                seen.add((key, lid))
                out[key].append(f)
    return out


LOG_LABELS = {"buildings": "budovy", "roads": "vozovky ze šířky silnic", "sidewalks": "zpevněná náměstí (PTPL)"}


def prepare(
    work_dir: Path,
    bbox_wgs84: tuple[float, float, float, float],
    *,
    log: LogFn | None = None,
    force: bool = False,  # noqa: ARG001 – stejný podpis jako příprava DTM
    counties_fn: Callable[..., list[str]] = counties_for,
    county_fn: Callable[..., Path] = ensure_county,
) -> Path | None:
    """Budovy, vozovky a náměstí BDOT10k pro výřez → soubory ``work_dir/dtm``; None, když ve výřezu nic není."""
    from pyproj import Transformer

    from app.pipeline.bgt_nl import clean_polygons, feature_polygons, write_dtm_files
    from app.pipeline.crs_5514 import CRS_LABEL
    from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514
    from app.pipeline.fmzk_at import reproject_features
    from app.pipeline.zbgis_sk import road_polygons

    west, south, east, north = bbox_wgs84
    bounds = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    box = Transformer.from_crs(CRS_LABEL, SOURCE_CRS, always_xy=True).transform_bounds(*bounds, densify_pts=21)
    t0 = time.monotonic()
    build_stats.note_background("dtm", source="pl_bdot10k")
    counties = counties_fn(box)
    if not counties:
        if log:
            log("BDOT10k: výřez mimo okresy Polska (ULDK), kresba z OSM.")
        return None
    parts = []
    for teryt in counties:
        try:
            parts.append(read_county(county_fn(teryt, log=log), box))
        except Exception as exc:  # jeden okres nedostupný: zbytek výřezu z ostatních
            if log:
                log(f"⚠ BDOT10k {teryt}: okres nedostupný ({type(exc).__name__}: {exc}), jeho část z OSM")
    if not parts:
        raise RuntimeError("BDOT10k: žádný okres výřezu se nepodařilo načíst")
    data = merge(parts)
    reproject_features(data, SOURCE_CRS, CRS_LABEL)
    count = sum(len(v) for v in data.values())
    build_stats.note_background("dtm", packages=len(counties), regions=counties, source="pl_bdot10k", objects=count)
    if log:
        log(
            f"BDOT10k (okresy {', '.join(counties)}): {len(data['buildings'])} budov, {len(data['roads'])} úseků silnic, "
            f"{len(data['squares'])} náměstí ({time.monotonic() - t0:.0f} s)"
        )
    if not count:
        if log:
            log("BDOT10k: ve výřezu nic, kresba z OSM.")
        return None
    buildings = clean_polygons([p for f in data["buildings"] for p in feature_polygons(f)])
    roads = road_polygons(data["roads"])
    squares = clean_polygons([p for f in data["squares"] for p in feature_polygons(f)])
    sorted_data = {"buildings": buildings, "roads": roads, "sidewalks": squares, "public": list(roads) + list(squares)}
    return write_dtm_files(
        work_dir, sorted_data, bounds, log=log, source="pl_bdot10k", label="BDOT10k", labels=LOG_LABELS,
        yard_label="zahrady (v BDOT10k nejsou)",
    )
