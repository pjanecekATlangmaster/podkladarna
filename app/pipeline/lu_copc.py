"""Lucembursko: výška porostu z mračna LiDAR 2024 (COPC v ZIPech data.public.lu), čtená jen řidší úrovní.

Celostátní MNS 0,5 m (``reference_lu.LU_DSM_URL``) je z náletu únor–duben 2024 a v korunách listnáčů bez listí má
mezery: v lese vychází spousta drobných žlutých plošek 401 (Müllerthal 3 km²: CHM nad 2 m jen 66 % plochy, s mračnem
72 %; Lucemburk sprint 58 → 66 %). Mračno tam má většinu bodů ve třídách 3–5 (nízká, střední, vysoká vegetace),
povrch se proto doplní maximem výšky těchto bodů v buňce 2 m (jen zvýší, nikdy nesníží MNS).

Mračno je po dlaždicích 500 m (název ``<E západ>_<N sever>.laz``), devět dlaždic v jednom ZIPu 1,5 × 1,5 km
(``lidar2024-cCCC-rRRR.zip``, ~1,8 GB). Členy ZIPu jsou uložené bez komprese, takže každá dlaždice COPC je souvislý
úsek bajtů a laspy z něj přes HTTP Range čte jen nejhrubší uzly do rozlišení ``RESOLUTION_M`` (~3,6 MB a ~1 s na
dlaždici místo ~200 MB; Müllerthal 3 km² = 20 dlaždic 72 MB). Mřížka dlaždice se ukládá do cache
(``CACHE_DIR/lu_lidar``), další výřez ji nestahuje.
"""

from __future__ import annotations

import io
import json
import math
import os
import struct
import threading
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from app import settings

LU_LIDAR_DATASET_API = "https://data.public.lu/api/1/datasets/lidar-2024-releve-3d-du-territoire-luxembourgeois/"
TILE_M = 500
ZIP_M = 1500
# Mřížka ZIPů: sloupec od E 48 500, řada od N 56 500 (ověřeno na c001-r029, c020-r012, c020-r013).
ZIP_E0 = 48500
ZIP_N0 = 56500
RESOLUTION_M = 4.0  # nejhrubší úrovně COPC: ~3,6 MB na dlaždici (2 m: ~13 MB); plocha CHM nad 2 m 71,8 proti 72,4 %
CELL_M = 2.0
VEG_CLASSES = (3, 4, 5)
INDEX_TTL_S = 7 * 24 * 3600
CACHE_VERSION = 2
HTTP_TIMEOUT_S = 60
WORKERS = 4
# Strop stažených bajtů na jednu dlaždici (hrubé uzly mají 3–6 MB, celá dlaždice ~200 MB): víc = chyba, ne download.
MAX_TILE_BYTES = 40_000_000


def tile_names(window: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """Dlaždice 500 m (E západ, N sever), které zasahují do okna (EPSG:2169)."""
    xmin, ymin, xmax, ymax = window
    out = []
    for e in range(math.floor(xmin / TILE_M) * TILE_M, math.ceil(xmax / TILE_M) * TILE_M, TILE_M):
        for n in range(math.floor(ymin / TILE_M) * TILE_M, math.ceil(ymax / TILE_M) * TILE_M, TILE_M):
            out.append((e, n + TILE_M))
    return out


def zip_name(e_west: int, n_north: int) -> str:
    col = (e_west - ZIP_E0) // ZIP_M + 1
    row = (n_north - TILE_M - ZIP_N0) // ZIP_M + 1
    return f"lidar2024-c{col:03d}-r{row:03d}.zip"


def _cache_dir() -> Path:
    return settings.CACHE_DIR / "lu_lidar"


def _get(url: str, *, headers: dict | None = None, method: str = "GET"):
    from app.pipeline.fetch_openzu import USER_AGENT

    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S)  # noqa: S310 – pevná adresa data.public.lu


def zip_index(*, fetch=None) -> dict[str, str]:
    """Název ZIPu → URL (adresy mají v cestě datum nahrání, proto z API datasetu; cache týden)."""
    path = _cache_dir() / "zip_index.json"
    try:
        if time.time() - path.stat().st_mtime < INDEX_TTL_S:
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if fetch is None:
        def fetch(url):
            with _get(url) as r:
                return json.load(r)
    data = fetch(LU_LIDAR_DATASET_API)
    index = {r["url"].rsplit("/", 1)[-1]: r["url"] for r in data.get("resources", []) if r.get("url", "").endswith(".zip")}
    if index:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(index), encoding="utf-8")
    return index


class _RangeFile(io.RawIOBase):
    """Úsek ``[offset, offset + size)`` vzdáleného souboru jako soubor (HTTP Range)."""

    def __init__(self, url: str, offset: int, size: int, max_bytes: int = MAX_TILE_BYTES):
        self.url, self.offset, self.size, self.pos, self.bytes_read = url, offset, size, 0, 0
        self.max_bytes = max_bytes

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos

    def read(self, n=-1):
        n = self.size - self.pos if n is None or n < 0 else min(n, self.size - self.pos)
        if n <= 0:
            return b""
        if self.bytes_read + n > self.max_bytes:
            raise RuntimeError(f"stahování přes {self.max_bytes / 1e6:.0f} MB odmítnuto")
        a = self.offset + self.pos
        with _get(self.url, headers={"Range": f"bytes={a}-{a + n - 1}"}) as r:
            data = r.read(n + 1)
        if len(data) > n:  # server Range ignoroval (200 s celým souborem)
            raise RuntimeError("server nevrátil požadovaný úsek (HTTP Range)")
        self.pos += len(data)
        self.bytes_read += len(data)
        return data

    def readinto(self, buf):
        data = self.read(len(buf))
        buf[: len(data)] = data
        return len(data)


def _member_range(url: str, member: str) -> tuple[int, int] | None:
    """Začátek a délka nekomprimovaného členu ZIPu (z centrálního adresáře a lokální hlavičky)."""
    with _get(url, method="HEAD") as r:
        total = int(r.headers["Content-Length"])
    f = _RangeFile(url, 0, total)
    with zipfile.ZipFile(f) as z:
        try:
            info = z.getinfo(member)
        except KeyError:
            return None
    if info.compress_type != zipfile.ZIP_STORED:
        return None
    f.seek(info.header_offset)
    head = f.read(30)
    name_len, extra_len = struct.unpack("<HH", head[26:30])
    return info.header_offset + 30 + name_len + extra_len, info.file_size


def _tile_grid(url: str, member: str, e_west: int, n_north: int) -> tuple[np.ndarray, int]:
    """Maximum výšky bodů vegetace v buňkách ``CELL_M`` dlaždice (NaN bez bodů) a počet stažených bajtů."""
    import laspy

    rng = _member_range(url, member)
    if rng is None:
        raise RuntimeError(f"{member} v {url.rsplit('/', 1)[-1]} chybí nebo je komprimovaný")
    f = _RangeFile(url, *rng)
    with laspy.CopcReader(f, http_num_threads=1) as reader:
        pts = reader.query(resolution=RESOLUTION_M)
    cls = np.asarray(pts.classification)
    keep = np.isin(cls, VEG_CLASSES)
    x, y, z = np.asarray(pts.x)[keep], np.asarray(pts.y)[keep], np.asarray(pts.z)[keep]
    n = int(TILE_M / CELL_M)
    col = np.clip(((x - e_west) / CELL_M).astype(int), 0, n - 1)
    row = np.clip(((n_north - y) / CELL_M).astype(int), 0, n - 1)
    grid = np.full(n * n, -np.inf, dtype="float64")
    np.maximum.at(grid, row * n + col, z)
    grid = grid.reshape(n, n).astype("float32")
    grid[~np.isfinite(grid)] = np.nan
    return grid, f.bytes_read


def vegetation_surface(window: tuple[float, float, float, float], out_dir: Path, *, log=None) -> Path | None:
    """GeoTIFF (EPSG:2169, buňka 2 m) s nejvyšším bodem vegetace; None, když mračno pro okno není dostupné."""
    from osgeo import gdal

    tiles = tile_names(window)
    if not tiles:
        return None
    index = zip_index()
    cache = _cache_dir()
    cache.mkdir(parents=True, exist_ok=True)

    def one(tile):
        e, n = tile
        member = f"{e}_{n}.laz"
        cached = cache / f"v{CACHE_VERSION}_{e}_{n}.npy"
        if cached.is_file():
            return tile, np.load(cached), 0, None
        url = index.get(zip_name(e, n))
        if not url:
            return tile, None, 0, "mimo pokrytí"
        try:
            grid, nbytes = _tile_grid(url, member, e, n)
        except Exception as exc:  # noqa: BLE001 – jedna dlaždice nesmí shodit job, porost zůstane z MNS
            return tile, None, 0, str(exc)[:160]
        tmp = cached.with_name(f"{cached.stem}.{os.getpid()}.{threading.get_ident()}.tmp.npy")
        np.save(tmp, grid)
        tmp.replace(cached)
        return tile, grid, nbytes, None

    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="lu-copc") as ex:
        results = list(ex.map(one, tiles))
    got = [(t, g) for t, g, _b, _e in results if g is not None]
    total = sum(b for _t, _g, b, _e in results)
    failed = [(t, err) for t, g, _b, err in results if g is None and err != "mimo pokrytí"]
    if log:
        log(f"  Mračno LiDAR 2024 (vegetace, COPC do {RESOLUTION_M:g} m): {len(got)}/{len(tiles)} dlaždic, "
            f"staženo {total / 1e6:.1f} MB")
        for (e, n), err in failed:
            log(f"    dlaždice {e}_{n}: {err}")
    if not got:
        return None
    es = [t[0] for t, _g in got]
    ns = [t[1] for t, _g in got]
    x0, y1 = min(es), max(ns)
    cols = (max(es) + TILE_M - x0) // TILE_M
    rows = (y1 - (min(ns) - TILE_M)) // TILE_M
    n = int(TILE_M / CELL_M)
    mosaic = np.full((rows * n, cols * n), np.nan, dtype="float32")
    for (e, nn), grid in got:
        r0, c0 = (y1 - nn) // TILE_M * n, (e - x0) // TILE_M * n
        mosaic[r0:r0 + n, c0:c0 + n] = grid
    nodata = -9999.0
    mosaic[np.isnan(mosaic)] = nodata
    dest = out_dir / "lu_veg_max.tif"
    gdal.UseExceptions()
    ds = gdal.GetDriverByName("GTiff").Create(str(dest), mosaic.shape[1], mosaic.shape[0], 1, gdal.GDT_Float32,
                                              ["COMPRESS=DEFLATE"])
    ds.SetGeoTransform((float(x0), CELL_M, 0.0, float(y1), 0.0, -CELL_M))
    from osgeo import osr

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(2169)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(nodata)
    band.WriteArray(mosaic)
    band.FlushCache()
    ds = None
    return dest
