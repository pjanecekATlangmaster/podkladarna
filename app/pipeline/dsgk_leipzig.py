"""Lipsko: Digitale Stadtgrundkarte (DSGK 1:1 000) jako doplněk ALKIS, obdoba české DTM pro kresbu.

ALKIS vereinfacht (``alkis_de``) má jen uliční prostor od plotu k plotu. Město Lipsko dává celou Stadtgrundkarte
(dl-de/by-2.0, ``geodaten.leipzig.de``, měsíčně) jako jeden GeoPackage celého města (ZIP ~0,7 GB, rozbalený 2,1 GB,
EPSG:25833, 2,5D). WFS zatím není a ArcGIS REST města z cloudu neodpovídá, proto se ZIP stáhne do cache jako ZBGIS
na Slovensku a z rozbaleného GeoPackage se uloží jen potřebné vrstvy do malé SQLite s R-stromem (~desítky MB).

Z DSGK se bere, co ALKIS nemá: vozovky (``GK_Strasse``, disjunktní s chodníky), chodníky a stezky se zpevněním
(``GK_WegPfadSteig``), parkoviště a stání, náměstí a pěší zóny (``GK_Strassenverkehrsanlage``), schody a ploty
(``GK_SonstigesBauwerkOSonstigeEinrichtung``). Budovy a uliční prostor pro sprint zůstávají z ALKIS. Uvnitř obrysu
města (``configs/countries/de_leipzig.geojson``) se pěší plochy a parkoviště ALKIS nahradí podrobnějšími z DSGK.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from app.pipeline import build_stats

LISTING_URL = (
    "https://geodaten.leipzig.de/open_data/organisationseinheit/Amt_fuer_Geoinformation_und_Bodenordnung/"
    "Stadtgrundkarte/GeoPackage/"
)
DEFAULT_ZIP = "DSGK2025mH_GPKG25833_7837_NON_TILED.zip"  # jméno nese verzi modelu DSGK, ne datum (2026-11-22)
ZIP_PATTERN = re.compile(r'href="([^"/?]*_GPKG25833_[^"/?]*NON_TILED\.zip)"', re.IGNORECASE)
AREA_FILE = Path(__file__).resolve().parents[2] / "configs" / "countries" / "de_leipzig.geojson"
CACHE_SUBDIR = "de_leipzig"
DATASET = "dsgk"
STORE_NAME = "dsgk_lite.sqlite"
META_NAME = "meta.json"
STORE_VERSION = 1  # změna obsahu malé SQLite = nové sestavení z rozbaleného GeoPackage
SOURCE_EPSG = 25833
SOURCE_CRS = f"EPSG:{SOURCE_EPSG}"
SOURCE_LABEL = "DSGK Stadt Leipzig"
VERSION_CHECK_DAYS = 30
CACHE_MAX_AGE_DAYS = 90  # nepoužitá cache se smaže (``disk_space.aoi_cache_roots``)
HTTP_TIMEOUT_S = 120
CHUNK = 1 << 20
MAX_ZIP_BYTES = 3_000_000_000  # dnes 0,66 GB; víc je chyba serveru
MAX_GPKG_BYTES = 8_000_000_000  # rozbalený GeoPackage dnes 2,1 GB; víc = vadný nebo podvržený ZIP (zip bomba)
MIN_FREE_BYTES = 4_000_000_000  # volno před stažením (ZIP 0,7 GB + rozbalení 2,1 GB + rezerva), jinak jen ALKIS
_GEOM = '"$(GPKG_GEOMETRY_ATTRIBUT)"'  # sloupec geometrie se v exportu města skutečně takhle jmenuje

# (tabulka DSGK, sloupec kódu, druhý sloupec nebo None, kódy, které bereme)
LAYERS: tuple[tuple[str, str, str | None, frozenset[int] | None], ...] = (
    ("GK_Strasse", "widmung", None, None),
    ("GK_Strassenverkehrsanlage", "art", None, frozenset({1000, 4000, 5310, 5315, 5320, 5330, 5340, 5350})),
    ("GK_WegPfadSteig", "art", "befestigung", frozenset({1103, 1106, 1110, 1111, 2020})),
    ("GK_SonstigesBauwerkOSonstigeEinrichtung_f", "bauwerksfunktion", None, frozenset({1620})),
    ("GK_SonstigesBauwerkOSonstigeEinrichtung_l", "bauwerksfunktion", None, frozenset({1740, 1741, 1742})),
)
PARKING_ART = frozenset({5310, 5315, 5320})  # Parkplatz, Stellplatz, Rastplatz
PEDESTRIAN_ART = frozenset({4000, 5330, 5340, 5350})  # Platz, Fußgängerzone, Marktplatz, Festplatz
FOOTWAY_ART = frozenset({1103, 1106, 1110, 1111})  # Fuß-, Rad-, Rad- und Fußweg, Skaterstrecke
FAHRWEG_ART = 2020  # Fahrweg, Wirtschaftsweg: jen zpevněný jako zpevněná plocha (nezpevněné cesty kreslí OSM)
UNPAVED = 2000
STAIRS = 1620
FENCES = frozenset({1740, 1741, 1742})  # Zaun, Zaun rechts / links (strana značky, plot je tentýž)

LogFn = Callable[[str], None]


def cache_dir() -> Path:
    from app import settings

    return Path(settings.DOWNLOADS_DIR) / CACHE_SUBDIR / DATASET


def _area_rings() -> tuple:
    import json

    data = json.loads(AREA_FILE.read_text(encoding="utf-8"))
    return tuple(
        tuple((float(x), float(y)) for x, y in poly[0])
        for f in data["features"]
        for poly in (f["geometry"]["coordinates"] if f["geometry"]["type"] == "MultiPolygon" else [f["geometry"]["coordinates"]])
    )


def intersects(bbox_wgs84) -> bool:
    from app.countries import rings_share

    return rings_share(tuple(float(v) for v in bbox_wgs84), _area_rings()) > 0.0


def area_polygon(dst_crs: str):
    """Obrys města v ``dst_crs`` (shapely), ve kterém DSGK nahrazuje pěší plochy a parkoviště ALKIS."""
    from pyproj import Transformer
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    t = Transformer.from_crs("EPSG:4326", dst_crs, always_xy=True)
    polys = []
    for ring in _area_rings():
        xs, ys = t.transform([p[0] for p in ring], [p[1] for p in ring])
        polys.append(Polygon(list(zip(xs, ys))))
    return unary_union(polys)


def probe_url() -> str:
    """Lehký dotaz pro stav zdrojů: výpis složky GeoPackage na serveru města."""
    return LISTING_URL


def _http(url: str, method: str = "GET"):
    from app.pipeline.fetch_openzu import USER_AGENT

    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S)


def resolve_zip_url(listing: Callable[[str], str] | None = None) -> str:
    """Adresa aktuálního ZIPu z výpisu složky (jméno nese verzi modelu); bez výpisu výchozí jméno."""
    try:
        if listing is None:
            with _http(LISTING_URL) as resp:
                html = resp.read(1_000_000).decode("utf-8", "replace")
        else:
            html = listing(LISTING_URL)
    except Exception:
        return LISTING_URL + DEFAULT_ZIP
    names = sorted(set(ZIP_PATTERN.findall(html)))
    return LISTING_URL + (names[-1] if names else DEFAULT_ZIP)


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
                raise RuntimeError(f"DSGK Leipzig: soubor větší než {MAX_ZIP_BYTES // 1_000_000_000} GB")
            out.write(chunk)
    return total


def _gpkg_wkb(blob: bytes) -> bytes:
    from app.pipeline.zbgis_sk import gpkg_wkb

    return gpkg_wkb(blob)


def build_store(gpkg: Path, dest: Path) -> int:
    """Z GeoPackage DSGK uloží potřebné vrstvy (2D WKB, kódy, obálka) do malé SQLite s R-stromem; vrátí počet objektů."""
    from shapely import force_2d, wkb

    src = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    count = 0
    try:
        out.execute("create table f (id integer primary key, layer text, code integer, code2 integer, geom blob)")
        out.execute("create virtual table f_idx using rtree(id, minx, maxx, miny, maxy)")
        tables = {r[0] for r in src.execute("select name from sqlite_master where type='table'")}
        for table, col, col2, codes in LAYERS:
            if table not in tables:
                continue
            second = f', "{col2}"' if col2 else ", null"
            for blob, code, code2 in src.execute(f'select {_GEOM}, "{col}"{second} from "{table}"'):
                if not blob or (codes is not None and code not in codes):
                    continue
                try:
                    geom = force_2d(wkb.loads(_gpkg_wkb(blob)))
                except Exception:  # vadná geometrie: přeskočit
                    continue
                if geom.is_empty:
                    continue
                cur = out.execute(
                    "insert into f (layer, code, code2, geom) values (?, ?, ?, ?)",
                    (table, code, code2, wkb.dumps(geom)),
                )
                x0, y0, x1, y1 = geom.bounds
                out.execute("insert into f_idx values (?, ?, ?, ?, ?)", (cur.lastrowid, x0, x1, y0, y1))
                count += 1
        out.commit()
    finally:
        src.close()
        out.close()
    return count


def _read_meta(folder: Path) -> dict:
    from app.pipeline.zbgis_sk import _read_meta as read

    return read(folder)


def _write_meta(folder: Path, meta: dict) -> None:
    from app.pipeline.zbgis_sk import _write_meta as write

    write(folder, meta)


def _install(folder: Path, url: str, remote: dict, *, now: float, download=_download, log: LogFn | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    zip_path = folder / "download.zip.part"
    tmp_gpkg = folder / "dsgk.gpkg.part"
    tmp_store = folder / (STORE_NAME + ".part")
    t0 = time.monotonic()
    free = shutil.disk_usage(folder).free
    if free < MIN_FREE_BYTES:
        raise RuntimeError(f"DSGK Leipzig: málo místa na disku ({free / 1e9:.1f} GB)")
    try:
        nbytes = download(url, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            member = next((n for n in zf.namelist() if n.lower().endswith(".gpkg")), None)
            if member is None:
                raise RuntimeError("DSGK Leipzig: v ZIPu není GeoPackage")
            if zf.getinfo(member).file_size > MAX_GPKG_BYTES:
                raise RuntimeError("DSGK Leipzig: GeoPackage v ZIPu je nečekaně velký")
            written = 0
            with zf.open(member) as src, open(tmp_gpkg, "wb") as out:
                while chunk := src.read(CHUNK):
                    written += len(chunk)
                    if written > MAX_GPKG_BYTES:  # hlavička ZIPu může lhát
                        raise RuntimeError("DSGK Leipzig: GeoPackage v ZIPu je nečekaně velký")
                    out.write(chunk)
        zip_path.unlink(missing_ok=True)
        tmp_store.unlink(missing_ok=True)
        count = build_store(tmp_gpkg, tmp_store)
        if not count:
            raise RuntimeError("DSGK Leipzig: v GeoPackage nejsou očekávané vrstvy")
        os.replace(tmp_store, folder / STORE_NAME)
    finally:
        zip_path.unlink(missing_ok=True)
        tmp_gpkg.unlink(missing_ok=True)
        tmp_store.unlink(missing_ok=True)
    _write_meta(folder, {**remote, "checked": now, "url": url, "store": STORE_VERSION, "objects": count})
    build_stats.note_bytes("de_leipzig", nbytes, cached=False, key="dsgk_leipzig")
    if log:
        log(f"DSGK Lipsko: staženo {nbytes / 1e6:.0f} MB, do cache {count} objektů ({time.monotonic() - t0:.0f} s)")
    return folder / STORE_NAME


def ensure_store(
    *, log: LogFn | None = None, head=_head, download=_download, listing=None, now: float | None = None
) -> Path:
    """Malá SQLite s vrstvami DSGK v cache (stáhne při první potřebě, jednou za ``VERSION_CHECK_DAYS`` ověří novější)."""
    from app.slot_lock import file_lock

    folder = cache_dir()
    store = folder / STORE_NAME
    now = time.time() if now is None else now
    folder.mkdir(parents=True, exist_ok=True)
    with file_lock(folder / ".lock"):
        meta = _read_meta(folder) if store.is_file() else {}
        if meta and meta.get("store") != STORE_VERSION:
            meta = {}
        if meta and now - float(meta.get("checked") or 0) < VERSION_CHECK_DAYS * 86400:
            _write_meta(folder, meta)  # čas použití pro úklid cache
            return store
        url = resolve_zip_url(listing)
        try:
            remote = head(url)
        except Exception as exc:  # server nedostupný: starší data jsou lepší než žádná
            if meta:
                if log:
                    log(f"DSGK Lipsko: kontrola novější verze selhala ({type(exc).__name__}), použita data z cache")
                return store
            raise
        if meta and url == meta.get("url") and remote.get("last_modified") == meta.get("last_modified") and remote.get("size") == meta.get("size"):
            _write_meta(folder, {**meta, "checked": now})
            return store
        if log and meta:
            log("DSGK Lipsko: město vydalo novější verzi, stahuji znovu")
        try:
            return _install(folder, url, remote, now=now, download=download, log=log)
        except Exception as exc:  # nová verze se nestáhla: starší data z cache, další pokus za den
            if not meta:
                raise
            if log:
                log(f"DSGK Lipsko: stažení novější verze selhalo ({type(exc).__name__}), použita data z cache")
            _write_meta(folder, {**meta, "checked": now - (VERSION_CHECK_DAYS - 1) * 86400})
            return store


def read_features(store: Path, bounds_src: tuple[float, float, float, float]) -> list[tuple[str, int, int | None, object]]:
    """Objekty v obálce (EPSG:25833) jako ``(vrstva, kód, kód2, shapely geometrie)``."""
    from shapely import wkb

    x0, y0, x1, y1 = bounds_src
    con = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select layer, code, code2, geom from f where id in "
            "(select id from f_idx where maxx >= ? and minx <= ? and maxy >= ? and miny <= ?)",
            (x0, x1, y0, y1),
        ).fetchall()
    finally:
        con.close()
    return [(layer, code, code2, wkb.loads(blob)) for layer, code, code2, blob in rows]


def target(layer: str, code: int | None, code2: int | None) -> str | None:
    """Soubor DTM pro objekt DSGK, nebo None (kreslí OSM / ALKIS)."""
    if layer == "GK_Strasse":
        return "roads"
    if layer == "GK_Strassenverkehrsanlage":
        if code == 1000:
            return "roads"
        if code in PARKING_ART:
            return "parking"
        if code in PEDESTRIAN_ART:
            return "sidewalks"
        return None
    if layer == "GK_WegPfadSteig":
        if code2 == UNPAVED:
            return None
        if code in FOOTWAY_ART:
            return "sidewalks"
        if code == FAHRWEG_ART and code2 is not None:  # Fahrweg bez údaje o zpevnění bývá polní cesta
            return "small_paved"
        return None
    if layer.endswith("_f") and code == STAIRS:
        return "stairs"
    if layer.endswith("_l") and code in FENCES:
        return "fences"
    return None


def classify(feats: list[tuple[str, int, int | None, object]]) -> dict[str, list]:
    """Geometrie v cílovém CRS roztříděné do souborů DTM (plochy jako seznam prstenců, ploty jako ``dtm_fences.Fence``)."""
    from app.pipeline import dtm_fences
    from app.pipeline.bgt_nl import clean_polygons

    out: dict[str, list] = {k: [] for k in ("roads", "sidewalks", "parking", "small_paved", "stairs", "fences")}
    for layer, code, code2, geom in feats:
        key = target(layer, code, code2)
        if key is None:
            continue
        if key == "fences":
            for line in getattr(geom, "geoms", [geom]):
                if line.geom_type != "LineString":
                    continue
                coords = tuple((float(x), float(y)) for x, y in line.coords)
                if len(coords) >= 2:
                    out["fences"].append(dtm_fences.Fence(coords=coords, druh="", level=0, boundary=False, zpusob="1"))
            continue
        for poly in getattr(geom, "geoms", [geom]):
            if poly.geom_type != "Polygon":
                continue
            rings = [[list(p) for p in poly.exterior.coords]] + [[list(p) for p in r.coords] for r in poly.interiors]
            out[key].extend(clean_polygons([rings]))
    return out


def _reproject(feats, dst_crs: str):
    from pyproj import Transformer
    from shapely import transform

    t = Transformer.from_crs(SOURCE_CRS, dst_crs, always_xy=True)

    def fn(coords):
        import numpy as np

        xs, ys = t.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([xs, ys])

    return [(layer, code, code2, transform(geom, fn)) for layer, code, code2, geom in feats]


def _outside(polys: list, area) -> tuple[list, int]:
    """Plochy ALKIS (seznam prstenců) zkrácené o obrys města (mimo město beze změny) a počet dotčených."""
    from shapely.geometry import Polygon

    from app.pipeline.bgt_nl import clean_polygons

    out = []
    touched = 0
    for rings in polys:
        poly = Polygon(rings[0], rings[1:])
        if not poly.intersects(area):
            out.append(rings)
            continue
        touched += 1
        rest = poly.difference(area)
        for part in getattr(rest, "geoms", [rest]):
            if part.geom_type == "Polygon" and not part.is_empty:
                out.extend(
                    clean_polygons([[[list(p) for p in part.exterior.coords]] + [[list(p) for p in r.coords] for r in part.interiors]])
                )
    return out, touched


def augment(
    sorted_data: dict[str, list],
    bounds: tuple[float, float, float, float],
    dst_crs: str,
    *,
    log: LogFn | None = None,
    store_fn: Callable[..., Path] = ensure_store,
) -> dict[str, list]:
    """Doplní roztříděná data ALKIS (v ``dst_crs``) o vrstvy DSGK ve výřezu ``bounds`` (týž CRS)."""
    from pyproj import Transformer

    t0 = time.monotonic()
    store = store_fn(log=log)
    box = Transformer.from_crs(dst_crs, SOURCE_CRS, always_xy=True).transform_bounds(*bounds, densify_pts=21)
    feats = _reproject(read_features(store, box), dst_crs)
    extra = classify(feats)
    count = sum(len(v) for v in extra.values())
    build_stats.note_background("dtm", source="de_leipzig", objects=count)
    if log:
        parts = ", ".join(f"{k} {len(v)}" for k, v in extra.items() if v)
        log(f"DSGK Lipsko: {count} objektů ve výřezu ({parts or 'nic'}; {time.monotonic() - t0:.0f} s)")
    if not count:
        return sorted_data
    area = area_polygon(dst_crs)
    out = {k: list(v) for k, v in sorted_data.items()}
    for key in ("sidewalks", "parking"):
        out[key], replaced = _outside(out.get(key) or [], area)
        if log and replaced:
            log(f"DSGK Lipsko: {replaced} ploch ALKIS ({key}) uvnitř města nahrazeno podrobnějšími")
    for key, items in extra.items():
        out.setdefault(key, []).extend(items)
        if key in ("roads", "sidewalks", "parking", "small_paved"):
            out.setdefault("public", []).extend(items)
    return out
