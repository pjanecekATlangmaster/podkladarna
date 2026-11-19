"""Finsko: bažiny, holá skála, balvany a pole z Maastotietokanta (MML) jako doplněk OSM.

MapAnt.fi kreslí bažiny, skály a balvany z topografické databáze MML; z OSM je ve Finsku dostaneme jen zčásti.
Maastotietokanta je pod CC BY 4.0 a zrcadlo kartat.kapsi.fi ji dává jako SHP po polovinách listů 1 : 25 000
(12 × 12 km, ~7–12 MB ZIP, EPSG:3067). ZIP se stáhne do cache ``cache/fi_mtk/`` (cache podle výřezu,
``disk_space.aoi_cache_roots``) a čte se ogr2ogr výřezem přímo z ``/vsizip/``.

Výsledek se přidá do ``osm_paths/features.geojson`` (zdroj ``mtk``) stejnými druhy jako OSM, takže ho kresba bere
bez dalších pravidel: bažina 308 (MTBO 310), zamokřená půda 310 (``wetland_indistinct``), holá skála 214 jen v části
bez stromů podle CHM (skály MTK jsou z velké části pod lesem, v Nuuksiu ~20 % území; Petr 2026-11-19), balvany 204
(balvan z DEM u nich se nekreslí podruhé), pole 412 a skalní stěny 201 (jyrkänne; OSM skály se nekreslí). Kde stejný druh už má OSM, MTK se o něj ořízne.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

BASE_URL = "https://kartat.kapsi.fi/files/maastotietokanta/kaikki/etrs89/shp"
CACHE_SUBDIR = "fi_mtk"
SOURCE = "mtk"
FI_CRS = "EPSG:3067"
HTTP_TIMEOUT_S = 120
MAX_ZIP_BYTES = 200_000_000  # polovina listu má ~7–12 MB
VERSION_CHECK_DAYS = 30
FI_MTK_PROBE = f"{BASE_URL}/L4/L41/L4132R.shp.zip"  # Nuuksio; HEAD kontroly zdrojů
RETRIES = 2

# LUOKKA → druh (kind) features.geojson. Vrstvy: m_<list>_p plochy, m_<list>_s body.
AREA_CLASSES: dict[int, str] = {
    35411: "wetland",  # avosuo, helppokulkuinen (otevřená, schůdná)
    35412: "wetland",  # metsäsuo, helppokulkuinen (zalesněná, schůdná)
    35421: "wetland",  # avosuo, vaikeakulkuinen
    35422: "wetland",  # metsäsuo, vaikeakulkuinen
    35300: "wetland_indistinct",  # soistuma (zamokřená půda)
    34100: "bare_rock",  # kallioalue (skála, kreslí se jen její část bez stromů, ``_open_part_fn``)
    32611: "farmland",  # pelto
}
POINT_CLASSES: dict[int, str] = {34600: "boulder"}  # kivi
LINE_CLASSES: dict[int, str] = {34400: "cliff_mtk"}  # jyrkänne (skalní stěna), vrstva m_<list>_v
KIND_CODES = {"cliff_mtk": "201", "wetland": "308", "wetland_indistinct": "310", "bare_rock": "214", "farmland": "412", "boulder": "204"}

MIN_AREA_M2 = {"wetland": 150.0, "wetland_indistinct": 300.0, "bare_rock": 100.0, "farmland": 500.0}
BOULDER_OSM_MATCH_M = 5.0
CLIFF_MIN_M = 5.0
ROCK_TREE_M = 3.0  # výška porostu = strom
ROCK_OPEN_M = 3.0  # otevřená část skály: výběžky a mezery užší než toto se srovnají
ROCK_SAMPLE_M = 2.0


def half_sheet(sheet: str) -> str:
    """List 1 : 10 000 (``L4132D``) → polovina listu 1 : 25 000 na zrcadle (``L4132L``; A–D vlevo, E–H vpravo)."""
    return sheet[:5] + ("L" if sheet[5] in "ABCD" else "R")


def half_sheets(window: tuple[float, float, float, float]) -> list[str]:
    from app.pipeline.elevation_raster import fi_sheets

    out: list[str] = []
    for sheet in fi_sheets(window):
        half = half_sheet(sheet)
        if half not in out:
            out.append(half)
    return out


def sheet_url(half: str) -> str:
    return f"{BASE_URL}/{half[:2]}/{half[:3]}/{half}.shp.zip"


def cache_dir() -> Path:
    from app import settings

    return Path(settings.DOWNLOADS_DIR) / CACHE_SUBDIR


def _zip_ok(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            return any(n.startswith("m_") and n.endswith(".shp") for n in zf.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def _fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, suffix=".part")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "podkladarna"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp, tmp.open("wb") as out:
            total = 0
            while chunk := resp.read(1 << 20):
                total += len(chunk)
                if total > MAX_ZIP_BYTES:
                    raise RuntimeError(f"{dest.name}: odpověď větší než {MAX_ZIP_BYTES // 1_000_000} MB")
                out.write(chunk)
        if not _zip_ok(tmp):
            raise RuntimeError(f"{dest.name}: stažený soubor není ZIP s vrstvami MTK")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def ensure_sheet(half: str, *, log: LogFn | None = None) -> Path:
    """ZIP poloviny listu v cache; starší než ``VERSION_CHECK_DAYS`` se stáhne znovu (při chybě zůstane starý)."""
    dest = cache_dir() / f"{half}.shp.zip"
    fresh = dest.is_file() and time.time() - dest.stat().st_mtime < VERSION_CHECK_DAYS * 86400
    if fresh and _zip_ok(dest):
        os.utime(dest)  # použití = poslední přístup pro úklid cache
        return dest
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            _fetch(sheet_url(half), dest)
            return dest
        except urllib.error.HTTPError as exc:
            last = exc
            if not (exc.code >= 500 or exc.code == 429):
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError, RuntimeError) as exc:
            last = exc
        if attempt < RETRIES:
            time.sleep(3 * (attempt + 1))
    if dest.is_file() and _zip_ok(dest):
        if log:
            log(f"Maastotietokanta {half}: obnova selhala ({last}), beru starší kopii z cache")
        os.utime(dest)
        return dest
    raise RuntimeError(f"Maastotietokanta {half}: {last}")


def _read_layer(zip_path: Path, layer: str, classes, window, tmp_dir: Path) -> list[dict]:
    """Prvky vrstvy ve výřezu s LUOKKA ze ``classes`` (GeoJSON features v EPSG:3067)."""
    from app.pipeline.prepare_lidar import find_tool, run_cmd

    with zipfile.ZipFile(zip_path) as zf:
        if f"{layer}.shp" not in zf.namelist():
            return []
    out = tmp_dir / f"{layer}.geojson"
    xmin, ymin, xmax, ymax = window
    run_cmd([
        find_tool("ogr2ogr"), "-f", "GeoJSON", "-overwrite", "-s_srs", FI_CRS, "-t_srs", FI_CRS,
        "-spat", f"{xmin:.1f}", f"{ymin:.1f}", f"{xmax:.1f}", f"{ymax:.1f}",
        "-where", f"LUOKKA IN ({','.join(str(c) for c in sorted(classes))})",
        "-select", "LUOKKA", str(out), f"/vsizip/{zip_path}/{layer}.shp",
    ])
    try:
        return list(json.loads(out.read_text(encoding="utf-8")).get("features") or [])
    except (OSError, ValueError):
        return []


def _open_part_fn(work_dir: Path):
    """Funkce polygon → jeho otevřená část (CHM pod ``ROCK_TREE_M``, buňky ``ROCK_SAMPLE_M``, drobné výběžky
    a mezery do ``ROCK_OPEN_M`` srovnané); None = CHM není a skála se nekreslí."""
    from app.pipeline.vegetation_chm import chm_path

    path = chm_path(work_dir)
    if path is None:
        return None
    try:
        import numpy as np
        import shapely
        from osgeo import gdal
    except ImportError:
        return None
    gdal.UseExceptions()
    ds = gdal.Open(str(path))
    chm = ds.GetRasterBand(1).ReadAsArray().astype("float32")
    nodata = ds.GetRasterBand(1).GetNoDataValue()
    x0, dx, _, y0, _, dy = ds.GetGeoTransform()
    ds = None
    h, w = chm.shape

    from shapely.geometry import box as _box
    from shapely.ops import unary_union as _union

    half = ROCK_SAMPLE_M / 2

    def open_part(poly):
        minx, miny, maxx, maxy = poly.bounds
        xs = np.arange(minx + ROCK_SAMPLE_M / 2, maxx, ROCK_SAMPLE_M)
        ys = np.arange(miny + ROCK_SAMPLE_M / 2, maxy, ROCK_SAMPLE_M)
        if not len(xs) or not len(ys):
            p = poly.representative_point()
            xs, ys = np.array([p.x]), np.array([p.y])
        gx, gy = np.meshgrid(xs, ys)
        gx, gy = gx.ravel(), gy.ravel()
        inside = shapely.contains_xy(poly, gx, gy)
        gx, gy = gx[inside], gy[inside]
        cols = ((gx - x0) / dx).astype(int)
        rows = ((gy - y0) / dy).astype(int)
        ok = (cols >= 0) & (cols < w) & (rows >= 0) & (rows < h)
        gx, gy, cols, rows = gx[ok], gy[ok], cols[ok], rows[ok]
        vals = chm[rows, cols]
        bare = np.isfinite(vals) & (vals < ROCK_TREE_M)
        if nodata is not None:
            bare &= vals != nodata
        if not bare.any():
            return poly.buffer(0).difference(poly)  # prázdná
        cells = _union([_box(x - half, y - half, x + half, y + half) for x, y in zip(gx[bare], gy[bare])])
        r = ROCK_OPEN_M / 2
        return cells.buffer(-r, join_style=2).buffer(2 * r, join_style=2).buffer(-r, join_style=2).intersection(poly)

    return open_part


def mtk_features(
    work_dir: Path,
    window: tuple[float, float, float, float],
    existing: list[dict],
    *,
    log: LogFn | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """Prvky MTK ve výřezu (EPSG:3067) oříznuté o OSM stejného druhu; vrátí (features, počty podle druhu)."""
    from shapely.geometry import Point, box, mapping, shape
    from shapely.ops import unary_union

    clip = box(*window)
    areas: dict[str, list] = {k: [] for k in MIN_AREA_M2}
    boulders: list = []
    cliffs: list = []
    with tempfile.TemporaryDirectory(prefix="mtk_") as tmp:
        tmp_dir = Path(tmp)
        for half in half_sheets(window):
            zip_path = ensure_sheet(half, log=log)
            for feat in _read_layer(zip_path, f"m_{half}_p", AREA_CLASSES, window, tmp_dir):
                kind = AREA_CLASSES.get(int((feat.get("properties") or {}).get("LUOKKA") or 0))
                try:
                    geom = shape(feat["geometry"])
                except Exception:
                    continue
                if kind and not geom.is_empty:
                    areas[kind].append(geom if geom.is_valid else geom.buffer(0))
            for feat in _read_layer(zip_path, f"m_{half}_s", POINT_CLASSES, window, tmp_dir):
                geom = feat.get("geometry") or {}
                if geom.get("type") == "Point":
                    boulders.append(Point(geom["coordinates"][:2]))
            for feat in _read_layer(zip_path, f"m_{half}_v", LINE_CLASSES, window, tmp_dir):
                try:
                    cliffs.append(shape(feat["geometry"]))
                except Exception:
                    continue

    osm_areas: dict[str, list] = {k: [] for k in MIN_AREA_M2}
    osm_boulders: list = []
    for feat in existing:
        kind = str((feat.get("properties") or {}).get("kind") or "")
        try:
            geom = shape(feat.get("geometry"))
        except Exception:
            continue
        if kind == "boulder" and geom.geom_type == "Point":
            osm_boulders.append(geom)
        elif kind in osm_areas and geom.geom_type in ("Polygon", "MultiPolygon"):
            osm_areas[kind].append(geom if geom.is_valid else geom.buffer(0))
    # Bažina z OSM vylučuje i zamokřenou půdu MTK (OSM nerozlišuje).
    osm_areas["wetland_indistinct"] = osm_areas["wetland_indistinct"] + osm_areas["wetland"]

    open_part = _open_part_fn(work_dir)
    out: list[dict] = []
    stats: dict[str, int] = {}
    for kind, geoms in areas.items():
        if not geoms or (kind == "bare_rock" and open_part is None):
            continue
        merged = unary_union(geoms).intersection(clip)
        if osm_areas[kind]:
            merged = merged.difference(unary_union(osm_areas[kind]))
        if kind == "wetland_indistinct" and areas["wetland"]:
            merged = merged.difference(unary_union(areas["wetland"]))
        parts = [p for p in getattr(merged, "geoms", [merged]) if p.geom_type == "Polygon"]
        if kind == "bare_rock":
            total = sum(p.area for p in parts)
            opened = [open_part(p) for p in parts if p.area >= MIN_AREA_M2[kind]]
            parts = [q for g in opened for q in getattr(g, "geoms", [g]) if q.geom_type == "Polygon"]
            kept = sum(p.area for p in parts if p.area >= MIN_AREA_M2[kind])
            stats["bare_rock_forest_pct"] = round(100 * (1 - kept / total)) if total else 0
        for poly in parts:
            if poly.area < MIN_AREA_M2[kind]:
                continue
            out.append({"type": "Feature", "geometry": mapping(poly),
                        "properties": {"source": SOURCE, "kind": kind, "oom_code": KIND_CODES[kind]}})
            stats[kind] = stats.get(kind, 0) + 1
    for line in cliffs:
        piece = line.intersection(clip)
        for part in getattr(piece, "geoms", [piece]):
            if part.geom_type != "LineString" or part.length < CLIFF_MIN_M:
                continue
            out.append({"type": "Feature", "geometry": mapping(part),
                        "properties": {"source": SOURCE, "kind": "cliff_mtk", "oom_code": "201"}})
            stats["cliff_mtk"] = stats.get("cliff_mtk", 0) + 1
    for pt in boulders:
        if not clip.contains(pt) or any(pt.distance(o) <= BOULDER_OSM_MATCH_M for o in osm_boulders):
            continue
        out.append({"type": "Feature", "geometry": mapping(pt),
                    "properties": {"source": SOURCE, "kind": "boulder", "oom_code": "204"}})
        stats["boulder"] = stats.get("boulder", 0) + 1
    return out, stats


def add_mtk_features(work_dir: Path, window: tuple[float, float, float, float], *, log: LogFn | None = None) -> int:
    """Doplní MTK do ``osm_paths/features.geojson`` (opakované spuštění nahradí dřívější prvky ``mtk``)."""
    path = Path(work_dir) / "osm_paths" / "features.geojson"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        data = {}
    feats = [f for f in (data.get("features") or []) if (f.get("properties") or {}).get("source") != SOURCE]
    new, stats = mtk_features(work_dir, window, feats, log=log)
    data = {**data, "type": "FeatureCollection", "features": feats + new}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".geojson.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    shutil.move(str(tmp), str(path))
    if log:
        labels = {"wetland": "bažiny", "wetland_indistinct": "zamokřená půda", "bare_rock": "holá skála",
                  "farmland": "pole", "boulder": "balvany", "cliff_mtk": "skalní stěny"}
        parts = [f"{labels[k]} {stats[k]}" for k in labels if stats.get(k)]
        if stats.get("bare_rock_forest_pct"):
            parts.append(f"skála pod lesem vynechána ({stats['bare_rock_forest_pct']} % plochy skal)")
        log("Maastotietokanta (MML): " + (", ".join(parts) if parts else "ve výřezu nic navíc proti OSM"))
    return len(new)


def prepare(work_dir: Path, bbox_wgs84: tuple[float, float, float, float], *, log: LogFn | None = None) -> int:
    """Vstup z ``run_job`` po přípravě OSM: výřez jobu (+ buffer pro vektory) v souřadnicích jobu (EPSG:3067)."""
    from app.pipeline import crs_5514
    from app.pipeline.fetch_openzu import VECTOR_FETCH_BUFFER_M, crop_bounds_5514

    if crs_5514.CRS_LABEL != FI_CRS:
        if log:
            log(f"Maastotietokanta: přeskočeno (souřadnice jobu {crs_5514.CRS_LABEL}, ne {FI_CRS})")
        return 0
    west, south, east, north = bbox_wgs84
    window = crop_bounds_5514(west, south, east, north, buffer_m=VECTOR_FETCH_BUFFER_M)
    return add_mtk_features(work_dir, tuple(float(v) for v in window), log=log)
