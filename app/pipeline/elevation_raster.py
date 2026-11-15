"""Výšky jobu z rastrů terénu (DTM) a povrchu (DSM) – zahraniční build bez LAZ.

Česko bere LiDAR (DMR 5G + DMP OK) jako mračno bodů a DEM / DSM / CHM z něj počítá ``dem_prep``. Jinde
národní agentury dávají hlavně hotové rastry (SK DMR 5.0 / DMP 1.0, NL AHN, AT BEV). Tento modul je převede
na mřížku mapy do ``work/dem/`` se stejnými jmény jako ``dem_prep`` (``dem_raw``, ``dem_filled``, ``dsm_raw``,
``dsm_filled``, ``chm`` + ``dem_meta.json``), takže vrstevnice, srázy, kupky, stínování i vegetace z CHM
běží beze změny. Vegetace z hustoty bodů (``vegetation_density``) bez LAZ přeskočí na CHM.

Zdroj se volí podle ``elevation`` země v ``configs/countries.yaml``:

- ``local:<cesta>`` – předem stažené dlaždice (GeoTIFF) ve složce ``<cesta>/dtm`` a ``<cesta>/dsm``
  (relativní cesta k ``DATA_ROOT``); pro piloty z ručních exportů (SK MAPKA ≤ 400 km²).
- další zdroje (WCS a podobně) se registrují v ``PROVIDERS``.

Rozdíl proti CHM z DMP OK: rastrový DSM má i budovy (kde je DTM pod budovou interpolovaný, budova vyjde
jako „porost“, kterou ale v mapě překryje budova) a bývá z bezlistého období (listnatý les nižší).
Kde DTM z měření chybí (AHN: budovy a voda), je CHM 0.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from app import settings
from app.pipeline.crs_5514 import CRS_WKT
from app.pipeline.dem_prep import DemPrepResult, dem_work_dir
from app.pipeline.prepare_lidar import find_tool, log_step, run_cmd
from app.pipeline.reference_nl import NL_AHN_WCS

# Díry DTM (budovy, voda) jsou u rastrů větší než u DMR 5G z LAZ (-md 24 v ``_fill_dem_nodata``).
FILL_MAX_DISTANCE_PX = 250
NODATA = -9999.0



@dataclass
class SourceRasters:
    """Výsledek zdroje: rastry terénu a povrchu (GeoTIFF / VRT v libovolném CRS, s NoData).

    ``vertical_offset(lon, lat)`` (ETRS89): výška, o kterou jsou hodnoty nad národním výškovým systémem
    (elipsoidické výšky → odečte se kvazigeoid), jinak None.
    """

    dtm: Path
    dsm: Path | None = None
    vertical_offset: Callable[[float, float], float] | None = None


# Zdroj: funkce (bounds v CRS mapy, cílová složka, log) → SourceRasters.
Provider = Callable[[tuple[float, float, float, float], Path, Callable | None], SourceRasters]
PROVIDERS: dict[str, Provider] = {}


class ElevationUnavailable(RuntimeError):
    """Pro výřez nejsou výšková data (mimo pokrytí zdroje nebo zdroj ``none``)."""


def _gdal_tool(name: str) -> str:
    for candidate in (name, f"{name}.exe", f"{name}.py"):
        try:
            return find_tool(candidate)
        except RuntimeError:
            continue
    raise RuntimeError(f"GDAL nástroj '{name}' není k dispozici")


def _local_dir(spec: str) -> Path:
    raw = spec.split(":", 1)[1].strip()
    path = Path(raw)
    return path if path.is_absolute() else settings.DATA_ROOT / path


def _tiles_vrt(tiles_dir: Path, dest: Path, *, log=None) -> Path | None:
    tiles = sorted(p for p in tiles_dir.glob("*.tif") if p.is_file()) if tiles_dir.is_dir() else []
    if not tiles:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    list_file = dest.with_suffix(".txt")
    list_file.write_text("\n".join(str(p) for p in tiles) + "\n", encoding="utf-8")
    run_cmd([_gdal_tool("gdalbuildvrt"), "-input_file_list", str(list_file), str(dest)], log=log)
    return dest if dest.is_file() else None


def local_provider(spec: str) -> Provider:
    base = _local_dir(spec)

    def _fetch(bounds, out_dir: Path, log=None) -> SourceRasters:
        dtm = _tiles_vrt(base / "dtm", out_dir / "src_dtm.vrt", log=log)
        if dtm is None:
            raise ElevationUnavailable(f"chybí dlaždice terénu v {base / 'dtm'}")
        return SourceRasters(dtm, _tiles_vrt(base / "dsm", out_dir / "src_dsm.vrt", log=log))

    return _fetch


# --------------------------------------------------------------------------- NL: AHN přes PDOK WCS

AHN_WCS = NL_AHN_WCS
AHN_CRS = "EPSG:28992"
AHN_BUFFER_M = 20.0
AHN_MAX_SIDE_M = 1950.0  # PDOK WCS MAXSIZE=4000 px (0,5 m → do 2 km); větší výřez po dlaždicích


def _bounds_in(bounds, crs_from_wkt: str, crs_to: str) -> tuple[float, float, float, float]:
    from pyproj import Transformer

    xmin, ymin, xmax, ymax = bounds
    t = Transformer.from_crs(crs_from_wkt, crs_to, always_xy=True)
    xs, ys = [], []
    for i in range(11):  # hrany, ne jen rohy (pootočení mezi souřadnicovými systémy)
        f = i / 10
        for x, y in ((xmin + f * (xmax - xmin), ymin), (xmin + f * (xmax - xmin), ymax),
                     (xmin, ymin + f * (ymax - ymin)), (xmax, ymin + f * (ymax - ymin))):
            px, py = t.transform(x, y)
            xs.append(px)
            ys.append(py)
    return min(xs), min(ys), max(xs), max(ys)


def _download(url: str, dest: Path, *, log=None) -> Path:
    import urllib.request

    from app.pipeline.fetch_openzu import USER_AGENT

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=300) as resp, tmp.open("wb") as fh:
        ctype = resp.headers.get("Content-Type", "")
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    if "tiff" not in ctype.lower() or tmp.stat().st_size < 500:
        detail = tmp.read_bytes()[:300].decode("utf-8", "replace")
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"WCS nevrátil GeoTIFF ({ctype}): {detail}")
    tmp.replace(dest)
    if log:
        log(f"  {dest.name}: {dest.stat().st_size / 1e6:.1f} MB")
    return dest


def tile_boxes(
    bounds: tuple[float, float, float, float], *, max_side: float, buffer: float, step: float
) -> list[tuple[float, float, float, float]]:
    """Výřez (+ ``buffer``) rozdělený na dlaždice nejvýš ``max_side``, hrany zarovnané na ``step``."""
    import math

    xmin, ymin, xmax, ymax = bounds
    xmin, ymin = math.floor(xmin - buffer), math.floor(ymin - buffer)
    xmax, ymax = math.ceil(xmax + buffer), math.ceil(ymax + buffer)
    nx = max(1, math.ceil((xmax - xmin) / max_side))
    ny = max(1, math.ceil((ymax - ymin) / max_side))
    out = []
    for i in range(nx):
        for j in range(ny):
            box = (
                xmin + i * (xmax - xmin) / nx,
                ymin + j * (ymax - ymin) / ny,
                xmin + (i + 1) * (xmax - xmin) / nx,
                ymin + (j + 1) * (ymax - ymin) / ny,
            )
            out.append(tuple(round(v / step) * step for v in box))
    return out


def ahn_coverage_urls(coverage: str, bounds_rd: tuple[float, float, float, float]) -> list[str]:
    """GetCoverage URL po dlaždicích nejvýš ``AHN_MAX_SIDE_M`` (souřadnice RD New, zarovnané na 0,5 m)."""
    return [
        f"{AHN_WCS}?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&COVERAGEID={coverage}"
        f"&FORMAT=image/tiff&SUBSET=x({x0:.1f},{x1:.1f})&SUBSET=y({y0:.1f},{y1:.1f})"
        for x0, y0, x1, y1 in tile_boxes(bounds_rd, max_side=AHN_MAX_SIDE_M, buffer=AHN_BUFFER_M, step=0.5)
    ]


def nl_ahn_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """AHN (Actueel Hoogtebestand Nederland) DTM a DSM 0,5 m z PDOK WCS, CC0 / CC BY 4.0, bez klíče."""
    bounds_rd = _bounds_in(bounds, CRS_WKT, AHN_CRS)
    out = []
    for coverage in ("dtm_05m", "dsm_05m"):
        tiles = [
            _download(url, out_dir / f"ahn_{coverage}_{k}.tif", log=log)
            for k, url in enumerate(ahn_coverage_urls(coverage, bounds_rd))
        ]
        vrt = out_dir / f"ahn_{coverage}.vrt"
        run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", AHN_CRS, str(vrt), *map(str, tiles)], log=log)
        out.append(vrt)
    return SourceRasters(out[0], out[1])


PROVIDERS["nl_ahn"] = nl_ahn_provider


# --------------------------------------------------------------------------- CH: swissALTI3D + swissSURFACE3D (STAC)

# swisstopo (OGD, volné použití s uvedením zdroje): dlaždice 1 × 1 km jako COG GeoTIFF 0,5 m v EPSG:2056, výšky LN02
# (EPSG:5728, ortometrické – nic se nepřevádí). STAC vrací pro dlaždici všechny ročníky, bere se nejnovější.
# Z dlaždic se přes /vsicurl čte jen okno výřezu (celá dlaždice ~15 MB, sprint by stahoval 4–9 dlaždic dvakrát).
CH_STAC = "https://data.geo.admin.ch/api/stac/v0.9"
CH_CRS = "EPSG:2056"
CH_COLLECTIONS = (("dtm", "ch.swisstopo.swissalti3d"), ("dsm", "ch.swisstopo.swisssurface3d-raster"))
CH_GSD = 0.5
CH_BUFFER_M = 20.0


def _asset_url_ok(href: str, host: str) -> bool:
    """Odkaz na asset je z odpovědi STAC: jen HTTPS na serveru swisstopo (jde do /vsicurl gdal_translate)."""
    import urllib.parse

    parts = urllib.parse.urlsplit(href)
    return parts.scheme == "https" and parts.netloc == host


def stac_latest_assets(items: list[dict], gsd: float = CH_GSD, host: str | None = None) -> list[str]:
    """URL GeoTIFFu s rozlišením ``gsd`` z nejnovějšího ročníku každé dlaždice (id ``<sada>_<rok>_<E>-<N>``).

    Odkazy mimo ``host`` (výchozí server ``CH_STAC``) nebo bez HTTPS se zahodí."""
    import urllib.parse

    host = host or urllib.parse.urlsplit(CH_STAC).netloc
    best: dict[str, tuple[str, str]] = {}
    for item in items:
        tile = str(item.get("id") or "").rsplit("_", 1)[-1]
        when = str((item.get("properties") or {}).get("datetime") or "")
        href = next(
            (
                a.get("href")
                for a in (item.get("assets") or {}).values()
                if str(a.get("href") or "").endswith(".tif")
                and abs(float(a.get("eo:gsd") or 0) - gsd) < 1e-6
                and _asset_url_ok(str(a.get("href")), host)
            ),
            None,
        )
        if href and (tile not in best or when > best[tile][0]):
            best[tile] = (when, href)
    return [href for _tile, (_when, href) in sorted(best.items())]


def _stac_items(collection: str, bbox_wgs84: tuple[float, float, float, float]) -> list[dict]:
    import urllib.request

    from app.pipeline.fetch_openzu import USER_AGENT

    url: str | None = (
        f"{CH_STAC}/collections/{collection}/items?bbox=" + ",".join(f"{v:.6f}" for v in bbox_wgs84) + "&limit=100"
    )
    from app.pipeline.bgt_nl import next_page_url

    items: list[dict] = []
    pages = 0
    while url and pages < 20:  # výřez do 25 km² = desítky dlaždic, 100 na stránku
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        pages += 1
        items.extend(data.get("features") or [])
        url = next_page_url(data, url)
    return items


def cog_windows(
    urls: list[str], window: tuple[float, float, float, float], crs: str, out_dir: Path, prefix: str, *, log=None
) -> Path | None:
    """Okno ``window`` (v ``crs``) z každého vzdáleného COG přes /vsicurl → VRT; None, když žádné okno nevyšlo.

    Čte se jen část dlaždice v okně (COG: swisstopo 1 km ~15 MB, BEV 50 km ~10 GB). Dlaždice, která do okna
    nezasahuje, gdal_translate odmítne – přeskočí se."""
    xmin, ymin, xmax, ymax = window
    tiles = []
    for k, url in enumerate(urls):
        dest = out_dir / f"{prefix}_{k}.tif"
        try:
            run_cmd(
                [
                    _gdal_tool("gdal_translate"),
                    "--config", "GDAL_HTTP_MAX_RETRY", "3",
                    "--config", "GDAL_HTTP_RETRY_DELAY", "2",
                    "--config", "GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR",
                    "-projwin", f"{xmin:.1f}", f"{ymax:.1f}", f"{xmax:.1f}", f"{ymin:.1f}",
                    "-projwin_srs", crs,
                    "-co", "COMPRESS=DEFLATE",
                    f"/vsicurl/{url}",
                    str(dest),
                ],
                log=log,
            )
        except subprocess.CalledProcessError as exc:
            if log:
                log(f"  {url.rsplit('/', 1)[-1]}: přeskočeno ({(exc.stderr or '').strip()[-200:]})")
            continue
        if dest.is_file():
            tiles.append(dest)
    if not tiles:
        return None
    vrt = out_dir / f"{prefix}.vrt"
    run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", crs, str(vrt), *map(str, tiles)], log=log)
    if log:
        log(f"  {prefix}: {len(tiles)} dlaždic (okno výřezu)")
    return vrt


def _buffered(bounds, crs_to: str, buffer_m: float) -> tuple[float, float, float, float]:
    b = _bounds_in(bounds, CRS_WKT, crs_to)
    return b[0] - buffer_m, b[1] - buffer_m, b[2] + buffer_m, b[3] + buffer_m


def ch_swisstopo_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """swissALTI3D (terén) a swissSURFACE3D Raster (povrch) 0,5 m ze STAC swisstopo, bez klíče."""
    window = _buffered(bounds, CH_CRS, CH_BUFFER_M)
    bbox_wgs = _bounds_in(window, CH_CRS, "EPSG:4326")
    out: dict[str, Path | None] = {}
    for kind, collection in CH_COLLECTIONS:
        urls = stac_latest_assets(_stac_items(collection, bbox_wgs))
        out[kind] = cog_windows(urls, window, CH_CRS, out_dir, f"ch_{kind}", log=log) if urls else None
    if out["dtm"] is None:
        raise ElevationUnavailable("výřez je mimo pokrytí swissALTI3D")
    return SourceRasters(out["dtm"], out["dsm"])


PROVIDERS["ch_swisstopo"] = ch_swisstopo_provider


# --------------------------------------------------------------------------- AT: BEV ALS DTM + DSM 1 m (COG)

# BEV (CC BY 4.0, „Datenquelle: BEV“): roční mozaika celého Rakouska k 15. 9., COG 50 × 50 km v EPSG:3035, 1 m,
# výšky nad Jadranem (ortometrické, nic se nepřevádí). Výpis adresáře je zakázaný (403), jména dlaždic jsou podle
# mřížky: ``ALS_<DTM|DSM>_CRS3035RES50000mN<N>E<E>.tif``. Nejnovější ročník se najde HEAD dotazem.
AT_BEV_BASE = "https://data.bev.gv.at/download/ALS"
AT_BEV_CRS = "EPSG:3035"
AT_BEV_TILE_M = 50000
AT_BEV_BUFFER_M = 20.0
AT_BEV_FIRST_YEAR = 2023
_bev_dates: dict[tuple[int, int], str] = {}


def bev_tile_url(kind: str, date: str, east: int, north: int) -> str:
    return f"{AT_BEV_BASE}/{kind}/{date}/ALS_{kind}_CRS3035RES50000mN{north}E{east}.tif"


def bev_tiles(window_3035: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """Dlaždice (E, N levého dolního rohu) mřížky 50 km, které zasahují do okna."""
    import math

    xmin, ymin, xmax, ymax = window_3035
    t = AT_BEV_TILE_M
    return [
        (e * t, n * t)
        for e in range(math.floor(xmin / t), math.floor(xmax / t) + 1)
        for n in range(math.floor(ymin / t), math.floor(ymax / t) + 1)
    ]


def _head_ok(url: str) -> bool:
    import urllib.request

    from app.pipeline.fetch_openzu import USER_AGENT

    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def bev_latest_date(east: int, north: int, *, head=_head_ok, today=None) -> str | None:
    """Nejnovější ročník mozaiky (``RRRR0915``), který pro dlaždici existuje (DTM i DSM)."""
    import datetime as _dt

    key = (east, north)
    if key in _bev_dates:
        return _bev_dates[key]
    year = (today or _dt.date.today()).year
    for y in range(year, AT_BEV_FIRST_YEAR - 1, -1):
        date = f"{y}0915"
        if head(bev_tile_url("DTM", date, east, north)) and head(bev_tile_url("DSM", date, east, north)):
            _bev_dates[key] = date
            return date
    return None


def at_bev_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """BEV ALS DTM a DSM 1 m (COG, bez klíče) – okno výřezu z dlaždic 50 km."""
    window = _buffered(bounds, AT_BEV_CRS, AT_BEV_BUFFER_M)
    found = [(e, n, bev_latest_date(e, n)) for e, n in bev_tiles(window)]
    found = [(e, n, d) for e, n, d in found if d]
    if not found:
        raise ElevationUnavailable("výřez je mimo pokrytí BEV ALS")
    if log:
        log(f"  BEV ALS: mozaika k {', '.join(sorted({d for *_r, d in found}))}")
    out = {
        kind: cog_windows(
            [bev_tile_url(kind.upper(), d, e, n) for e, n, d in found], window, AT_BEV_CRS, out_dir, f"at_{kind}", log=log
        )
        for kind in ("dtm", "dsm")
    }
    if out["dtm"] is None:
        raise ElevationUnavailable("BEV ALS: žádné okno výřezu se nestáhlo")
    return SourceRasters(out["dtm"], out["dsm"])


PROVIDERS["at_bev"] = at_bev_provider


# --------------------------------------------------------------------------- DE: Berlín DGM1 + DOM1 (XYZ 2 × 2 km)

# GDI Berlin (Datenlizenz Deutschland – Zero 2.0, bez klíče): ATKIS DGM1 (terén) a DOM1 (povrch z ALS 2021), obojí
# 1 m, EPSG:25833, výšky DHHN2016 (normální výšky, nic se nepřevádí). Jen ZIP s XYZ textem po dlaždicích 2 × 2 km
# (levý dolní roh v km, sudé); mračno ALS jen v ZIPech desítek GB → nepoužitelné. Novější bDOM (z leteckých snímků
# 2026) je bez listí: v Grunewaldu má nad 5 m jen 29 % plochy proti 72 % v DOM1, porost by vyšel jako louka.
# Dlaždice se jednou převede na GeoTIFF (sever nahoře) a drží v cache.
DE_BE_BASE = "https://gdi.berlin.de/data"
DE_BE_CRS = "EPSG:25833"
DE_BE_TILE_M = 2000
DE_BE_BUFFER_M = 20.0
DE_BE_KINDS = {"dtm": "dgm1/atom/DGM1_{e}_{n}.zip", "dsm": "dom/atom/DOM1_{e}_{n}.zip"}
DE_BE_CACHE = {"dtm": "dgm1", "dsm": "dom1"}
DE_BE_MAX_ZIP_BYTES = 200_000_000  # dlaždice má ~20 MB; víc je chyba služby


def berlin_tiles(window: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """Dlaždice (E, N levého dolního rohu v km) mřížky 2 km, které zasahují do okna."""
    import math

    xmin, ymin, xmax, ymax = window
    step = DE_BE_TILE_M
    xs = range(math.floor(xmin / step) * step, math.ceil(xmax / step) * step, step)
    ys = range(math.floor(ymin / step) * step, math.ceil(ymax / step) * step, step)
    return [(x // 1000, y // 1000) for x in xs for y in ys]


def berlin_tile_url(kind: str, east_km: int, north_km: int) -> str:
    return f"{DE_BE_BASE}/{DE_BE_KINDS[kind].format(e=east_km, n=north_km)}"


def berlin_tile_dir(east_km: int, north_km: int) -> Path:
    """Složka dlaždice v cache (terén i povrch); úklid cache (``download_cache.prune_lidar_cache``) ji maže celou."""
    return settings.CACHE_DIR / "de_berlin" / f"{east_km}_{north_km}"


def _berlin_tile(kind: str, east_km: int, north_km: int, *, log=None) -> Path | None:
    """GeoTIFF dlaždice z cache, jinak stáhne ZIP s XYZ a převede; None mimo Berlín (404)."""
    import tempfile
    import urllib.error
    import urllib.request
    import zipfile

    from app.download_cache import touch_last_used
    from app.pipeline.fetch_openzu import USER_AGENT

    folder = berlin_tile_dir(east_km, north_km)
    dest = folder / f"{DE_BE_CACHE[kind]}.tif"
    if dest.is_file():
        touch_last_used(folder)
        return dest
    folder.mkdir(parents=True, exist_ok=True)
    # Jedinečné dočasné soubory (souběžné joby nad stejnou dlaždicí), na konci přejmenování; /vsizip chce příponu .zip.
    fd, zname = tempfile.mkstemp(prefix=f".{DE_BE_CACHE[kind]}-", suffix=".zip", dir=folder)
    os.close(fd)
    zpath = Path(zname)
    tmp: Path | None = None
    req = urllib.request.Request(berlin_tile_url(kind, east_km, north_km), headers={"User-Agent": USER_AGENT})
    try:
        try:
            with urllib.request.urlopen(req, timeout=300) as resp, zpath.open("wb") as fh:
                size = 0
                while chunk := resp.read(1 << 20):
                    size += len(chunk)
                    if size > DE_BE_MAX_ZIP_BYTES:
                        raise RuntimeError(
                            f"Berlín {kind} {east_km}_{north_km}: ZIP větší než {DE_BE_MAX_ZIP_BYTES // 1_000_000} MB"
                        )
                    fh.write(chunk)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        with zipfile.ZipFile(zpath) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith((".xyz", ".txt"))]
        if not names:
            raise RuntimeError(f"Berlín {kind} {east_km}_{north_km}: ZIP bez XYZ")
        fd, tname = tempfile.mkstemp(prefix=f".{DE_BE_CACHE[kind]}-", suffix=".tif", dir=folder)
        os.close(fd)
        tmp = Path(tname)
        # gdalwarp (ne translate): DGM1 má řádky od jihu, výstup sever nahoře (gdalbuildvrt jinak odmítne)
        run_cmd(
            [_gdal_tool("gdalwarp"), "-q", "-overwrite", "-s_srs", DE_BE_CRS, "-t_srs", DE_BE_CRS, "-ot", "Float32",
             "-dstnodata", str(NODATA), "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES",
             f"/vsizip/{zpath}/{names[0]}", str(tmp)],
            log=log,
        )
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise RuntimeError(f"Berlín {kind} {east_km}_{north_km}: převod XYZ selhal")
        tmp.replace(dest)
        tmp = None
    finally:
        zpath.unlink(missing_ok=True)
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    touch_last_used(folder)
    if log:
        log(f"  Berlín {kind} {east_km}_{north_km}: {dest.stat().st_size / 1e6:.1f} MB")
    return dest


def de_berlin_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """Berlín: ATKIS DGM1 (terén) a DOM1 (povrch z ALS) 1 m z GDI Berlin, dlaždice 2 km v cache."""
    window = _buffered(bounds, DE_BE_CRS, DE_BE_BUFFER_M)
    out: dict[str, Path | None] = {}
    for kind in DE_BE_KINDS:
        tiles = [t for e, n in berlin_tiles(window) if (t := _berlin_tile(kind, e, n, log=log)) is not None]
        if tiles:
            vrt = out_dir / f"de_berlin_{kind}.vrt"
            run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", DE_BE_CRS, str(vrt), *map(str, tiles)], log=log)
            out[kind] = vrt if vrt.is_file() else None
        else:
            out[kind] = None
    if out["dtm"] is None:
        raise ElevationUnavailable("výřez je mimo pokrytí DGM1 Berlín")
    return SourceRasters(out["dtm"], out["dsm"])


PROVIDERS["de_berlin"] = de_berlin_provider


# --------------------------------------------------------------------------- FR: LiDAR HD MNT + MNS (WMS-R IGN)

# IGN LiDAR HD (Licence Ouverte Etalab 2.0, bez klíče): MNT (terén) a MNS (povrch) 0,5 m, Lambert-93 (EPSG:2154),
# výšky NGF-IGN69 (nadmořské, nic se nepřevádí). Hotové rastry vydává služba WMS-R data.geopf.fr jako Float32
# GeoTIFF (NoData −9999, nejvýš 5010 px na stranu); dlaždice 2 × 2 km = 4000 px ≈ 64 MB, ~20 s. Mimo pokrytí
# vrátí samé NoData a výřez skončí na kontrole platných buněk v ``prepare_raster_surfaces``.

FR_WMS = "https://data.geopf.fr/wms-r/wms"
FR_CRS = "EPSG:2154"
FR_LAYERS = {
    "dtm": "IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93",
    "dsm": "IGNF_LIDAR-HD_MNS_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93",
}
FR_GSD = 0.5
FR_MAX_SIDE_M = 2000.0
FR_BUFFER_M = 20.0
FR_RETRIES = 3  # WMS-R občas vrátí 502 / přeruší spojení; další pokus obvykle projde


def _download_retry(url: str, dest: Path, *, retries: int = FR_RETRIES, log=None, wait_s: float = 3.0) -> Path:
    """``_download`` s opakováním po přechodné chybě (5xx, 429, síť); 4xx a odpověď bez GeoTIFFu hned dál."""
    import time
    import urllib.error

    for attempt in range(retries + 1):
        try:
            return _download(url, dest, log=log)
        except urllib.error.HTTPError as exc:
            if attempt >= retries or not (exc.code >= 500 or exc.code == 429):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt >= retries:
                raise
        if log:
            log(f"  {dest.name}: přechodná chyba služby, pokus {attempt + 2}/{retries + 1}")
        time.sleep(wait_s * (attempt + 1))
    raise AssertionError("nedosažitelné")


def fr_lidarhd_urls(kind: str, bounds_l93: tuple[float, float, float, float]) -> list[str]:
    """GetMap URL po dlaždicích nejvýš ``FR_MAX_SIDE_M`` (Lambert-93, hrany na 0,5 m, pixel přesně 0,5 m)."""
    out = []
    for x0, y0, x1, y1 in tile_boxes(bounds_l93, max_side=FR_MAX_SIDE_M, buffer=FR_BUFFER_M, step=FR_GSD):
        w, h = round((x1 - x0) / FR_GSD), round((y1 - y0) / FR_GSD)
        out.append(
            f"{FR_WMS}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS={FR_LAYERS[kind]}&STYLES="
            f"&CRS={FR_CRS}&BBOX={x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f}&WIDTH={w}&HEIGHT={h}&FORMAT=image/geotiff"
        )
    return out


def fr_lidarhd_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """IGN LiDAR HD: MNT (terén) a MNS (povrch) 0,5 m z WMS-R data.geopf.fr po dlaždicích 2 km."""
    from concurrent.futures import ThreadPoolExecutor

    bounds_l93 = _bounds_in(bounds, CRS_WKT, FR_CRS)
    jobs = [(kind, k, url) for kind in FR_LAYERS for k, url in enumerate(fr_lidarhd_urls(kind, bounds_l93))]
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="fr-lidarhd") as ex:
        tiles = list(ex.map(lambda j: _download_retry(j[2], out_dir / f"fr_{j[0]}_{j[1]}.tif", log=log), jobs))
    out = {}
    for kind in FR_LAYERS:
        vrt = out_dir / f"fr_lidarhd_{kind}.vrt"
        mine = [str(t) for (k, _i, _u), t in zip(jobs, tiles) if k == kind]
        run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", FR_CRS, str(vrt), *mine], log=log)
        out[kind] = vrt
    return SourceRasters(out["dtm"], out["dsm"])


PROVIDERS["fr_lidarhd"] = fr_lidarhd_provider

FR_TILES_MAX = 2000  # MTBO 25 km² ≈ 40 dlaždic 1 km; víc je chyba dotazu


def fr_lidarhd_coverage(bbox_wgs84: tuple[float, float, float, float], *, fetch=None) -> float:
    """Podíl výřezu s vydaným MNT i MNS LiDAR HD (tabulka dlaždic IGN); LiDAR HD vychází postupně, např. Lille chybí."""
    import urllib.parse

    from shapely.geometry import box, shape
    from shapely.ops import unary_union

    from app.pipeline.reference_fr import FR_LIDAR_TILES_WFS

    west, south, east, north = bbox_wgs84
    url = (
        f"{FR_LIDAR_TILES_WFS}&COUNT={FR_TILES_MAX}&BBOX="
        + urllib.parse.quote(f"{south:.6f},{west:.6f},{north:.6f},{east:.6f},urn:ogc:def:crs:EPSG::4326")
    )
    data = (fetch or _get_json_url)(url)
    tiles = [
        shape(f["geometry"])
        for f in data.get("features") or []
        if f.get("geometry") and (f.get("properties") or {}).get("url_mnt") and (f.get("properties") or {}).get("url_mns")
    ]
    window = box(west, south, east, north)
    if not tiles or window.area <= 0:
        return 0.0
    return float(unary_union(tiles).intersection(window).area / window.area)


def _get_json_url(url: str, timeout: float = 20.0) -> dict:
    import urllib.request

    from app.pipeline.fetch_openzu import USER_AGENT

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(50_000_000))


# --------------------------------------------------------------------------- SK: DMR 5.0 přes INSPIRE WCS ÚGKK

# Stahovací služba INSPIRE (Elevation): DMR 5.0 (1. cyklus LLS) 1 m, EPSG:3046 (ETRS89 / UTM 34N), výšky
# elipsoidické ETRS89-h; CC BY 4.0, atribuce „ÚGKK SR“. Povrch (DMP) ani mračno služba nemá (jen export
# mapového klienta ZBGIS s odkazem e-mailem). Z cloudu (USA) nedostupné, z Česka ano.
SK_WCS = "https://inspirews.skgeodesy.sk/geoserver/el/ows"
SK_COVERAGE = "el__EL.GridCoverage"
SK_CRS = "EPSG:3046"
SK_MAX_SIDE_M = 4000.0  # 2 × 2 km ≈ 12 MB za 30 s
SK_BUFFER_M = 20.0
# Kvazigeoid ETRS89-h → Bpv (Baltic 1957) z PROJ-data (ÚGKK SR, CC BY 4.0), ~0,5 MB, stáhne se jednou do cache.
SK_GEOID_URL = "https://cdn.proj.org/sk_gku_Slovakia_ETRS89h_to_Baltic1957.tif"


def sk_wcs_urls(bounds_utm: tuple[float, float, float, float]) -> list[str]:
    return [
        f"{SK_WCS}?service=WCS&version=2.0.1&request=GetCoverage&coverageId={SK_COVERAGE}"
        f"&format=image/tiff&subset=E({x0:.0f},{x1:.0f})&subset=N({y0:.0f},{y1:.0f})&geotiff:compression=Deflate"
        for x0, y0, x1, y1 in tile_boxes(bounds_utm, max_side=SK_MAX_SIDE_M, buffer=SK_BUFFER_M, step=1.0)
    ]


def _sk_geoid_grid(log=None) -> Path | None:
    dest = settings.CACHE_DIR / "geoid" / "sk_gku_Slovakia_ETRS89h_to_Baltic1957.tif"
    if dest.is_file() and dest.stat().st_size > 100_000:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _download(SK_GEOID_URL, dest, log=log)
    except Exception as exc:  # bez mřížky zůstanou výšky elipsoidické (vrstevnice posunuté ~40 m)
        if log:
            log(f"⚠ Kvazigeoid SK se nestáhl ({exc}): výšky zůstanou elipsoidické (o ~40–46 m vyšší).")
        return None


def geoid_offsets(grid: Path, points_lonlat: list[tuple[float, float]]) -> list[float]:
    """Výška kvazigeoidu (m) v bodech; bilineárně mezi středy pixelů mřížky PROJ-data."""
    from app.pipeline.gdal_cli_raster import read_float32_geotiff

    arr, gt, _nd = read_float32_geotiff(grid)
    arr = arr.astype("float64")
    out = []
    for lon, lat in points_lonlat:
        fx = (lon - gt[0]) / gt[1] - 0.5
        fy = (lat - gt[3]) / gt[5] - 0.5
        ix = min(max(int(fx), 0), arr.shape[1] - 2)
        iy = min(max(int(fy), 0), arr.shape[0] - 2)
        dx, dy = min(max(fx - ix, 0.0), 1.0), min(max(fy - iy, 0.0), 1.0)
        v = (
            arr[iy, ix] * (1 - dx) * (1 - dy)
            + arr[iy, ix + 1] * dx * (1 - dy)
            + arr[iy + 1, ix] * (1 - dx) * dy
            + arr[iy + 1, ix + 1] * dx * dy
        )
        out.append(float(v))
    return out


def sk_ugkk_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """DMR 5.0 Slovenska z INSPIRE WCS ÚGKK (výšky elipsoidické, kvazigeoid odečte ``prepare_raster_surfaces``)."""
    bounds_utm = _bounds_in(bounds, CRS_WKT, SK_CRS)
    tiles = [_download(url, out_dir / f"sk_dmr5_{k}.tif", log=log) for k, url in enumerate(sk_wcs_urls(bounds_utm))]
    vrt = out_dir / "sk_dmr5.vrt"
    run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", SK_CRS, str(vrt), *map(str, tiles)], log=log)
    grid = _sk_geoid_grid(log=log)
    offset = None
    if grid is not None:

        def offset(lon: float, lat: float) -> float:
            return geoid_offsets(grid, [(lon, lat)])[0]

    return SourceRasters(vrt, None, offset)


PROVIDERS["sk_ugkk"] = sk_ugkk_provider


SOURCE_LABELS = {
    "nl_ahn": "AHN, PDOK",
    "sk_ugkk": "DMR 5.0, ÚGKK SR",
    "ch_swisstopo": "swissALTI3D a swissSURFACE3D, swisstopo",
    "at_bev": "BEV ALS DTM a DSM",
    "de_berlin": "DGM1 a DOM1, GDI Berlin",
    "fr_lidarhd": "LiDAR HD MNT a MNS, IGN",
}


# Pokrytí výřezu výškami ještě před jobem (zdroje, které nejsou celoplošné): podíl 0–1.
COVERAGE: dict[str, Callable[[tuple[float, float, float, float]], float]] = {"fr_lidarhd": fr_lidarhd_coverage}
COVERAGE_MIN_SHARE = 0.5  # pod polovinou výřezu s výškami job nespouštět (terén by chyběl)
_COVERAGE_CACHE: dict[tuple, tuple[float, float | None]] = {}
_COVERAGE_TTL_S = 600.0


def elevation_coverage(source: str, bbox_wgs84: tuple[float, float, float, float]) -> float | None:
    """Podíl výřezu s výškami zdroje; None = zdroj je celoplošný nebo služba neodpověděla (job nebrzdit)."""
    import time

    check = COVERAGE.get(source)
    if check is None:
        return None
    key = (source, *(round(v, 5) for v in bbox_wgs84))
    hit = _COVERAGE_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _COVERAGE_TTL_S:
        return hit[1]
    try:
        share = check(tuple(bbox_wgs84))
    except Exception:  # noqa: BLE001 – kontrola je jen pojistka, výpadek služby job nezastaví
        return None
    if len(_COVERAGE_CACHE) > 500:
        _COVERAGE_CACHE.clear()
    _COVERAGE_CACHE[key] = (time.monotonic(), share)
    return share


def resolve_provider(source: str) -> Provider:
    if source.startswith("local:"):
        return local_provider(source)
    if source in PROVIDERS:
        return PROVIDERS[source]
    raise ElevationUnavailable(f"zdroj výšek „{source}“ není k dispozici")


def _is_krovak(src: Path) -> bool:
    from app.pipeline.gdal_cli_raster import _gdalinfo_json

    wkt = (_gdalinfo_json(src).get("coordinateSystem") or {}).get("wkt") or ""
    return "krovak" in wkt.lower()


def _warp_to_grid(
    src: Path,
    dest: Path,
    bounds: tuple[float, float, float, float],
    resolution_m: float,
    *,
    resampling: str,
    log=None,
) -> Path:
    xmin, ymin, xmax, ymax = bounds
    dest.parent.mkdir(parents=True, exist_ok=True)
    srs_file = dest.parent / "job_crs.wkt"
    srs_file.write_text(CRS_WKT + "\n", encoding="utf-8")
    # S-JTSK zdroje (SK: JTSK i JTSK03) mají stejné souřadnice jako mapa; bez -s_srs by GDAL převáděl
    # datum (oficiální S-JTSK → WGS84 → náš TOWGS84) a rastr posunul o metry až desítky metrů.
    same = ["-s_srs", str(srs_file)] if _is_krovak(src) and "Krovak" in CRS_WKT else []
    run_cmd(
        [
            _gdal_tool("gdalwarp"),
            "-overwrite",
            *same,
            "-t_srs",
            str(srs_file),
            "-te",
            *(f"{v:.3f}" for v in (xmin, ymin, xmax, ymax)),
            "-tr",
            f"{resolution_m:g}",
            f"{resolution_m:g}",
            "-r",
            resampling,
            "-dstnodata",
            f"{NODATA:g}",
            "-ot",
            "Float32",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            str(src),
            str(dest),
        ],
        log=log,
    )
    if not dest.is_file() or dest.stat().st_size < 500:
        raise RuntimeError(f"gdalwarp nevytvořil {dest.name}")
    return dest


def _fill(src: Path, dest: Path, *, log=None) -> Path:
    try:
        tool = _gdal_tool("gdal_fillnodata")
    except RuntimeError:
        dest.write_bytes(src.read_bytes())
        return dest
    run_cmd([tool, "-md", str(FILL_MAX_DISTANCE_PX), "-q", str(src), str(dest)], log=log)
    return dest


def _read(path: Path) -> np.ndarray:
    """Pole Float32 s NoData převedeným na ``NODATA`` (osgeo, jinak GDAL CLI)."""
    from app.pipeline.gdal_cli_raster import read_float32_geotiff

    arr, _gt, nd = read_float32_geotiff(path)
    arr = np.asarray(arr, dtype="float32")
    bad = ~np.isfinite(arr)
    if nd is not None:
        bad |= arr == np.float32(nd)
    return np.where(bad, np.float32(NODATA), arr)


def _write_like(template: Path, arr: np.ndarray, dest: Path) -> Path:
    from app.pipeline.gdal_cli_raster import read_float32_geotiff, write_float32_geotiff

    _a, gt, _nd = read_float32_geotiff(template)
    return write_float32_geotiff(dest, arr, gt, srs_wkt=CRS_WKT, nodata=NODATA)


def _valid_share(path: Path) -> float:
    arr = _read(path)
    return float((arr > NODATA / 2).mean()) if arr.size else 0.0


def _subtract_vertical_offset(path: Path, bounds, offset: Callable[[float, float], float]) -> tuple[float, float]:
    """Odečte posun výšek (kvazigeoid) jako bilineární plochu z rohů výřezu; vrátí rozsah posunu."""
    from pyproj import Transformer

    from app.pipeline.crs_5514 import CRS_PROJ4

    xmin, ymin, xmax, ymax = bounds
    to_ll = Transformer.from_crs(CRS_PROJ4, "EPSG:4258", always_xy=True)
    n00, n10, n01, n11 = (offset(*to_ll.transform(x, y)) for x, y in ((xmin, ymax), (xmax, ymax), (xmin, ymin), (xmax, ymin)))
    arr = _read(path)
    h, w = arr.shape
    fx = (np.arange(w) + 0.5)[None, :] / w
    fy = (np.arange(h) + 0.5)[:, None] / h  # řádek 0 = sever
    plane = n00 * (1 - fx) * (1 - fy) + n10 * fx * (1 - fy) + n01 * (1 - fx) * fy + n11 * fx * fy
    valid = arr > NODATA / 2
    tmp = path.with_name(path.stem + "_vert.tif")
    _write_like(path, np.where(valid, arr - plane, NODATA).astype("float32"), tmp)
    tmp.replace(path)
    return min(n00, n10, n01, n11), max(n00, n10, n01, n11)


def chm_from_rasters(dem_raw: Path, dem_filled: Path, dsm: Path, dest: Path) -> Path:
    """CHM = max(DSM − DTM, 0). Kde chybí DTM z měření (budovy a voda v AHN), je CHM 0 (ne porost),
    kde chybí DSM, také 0; mimo platný DTM vůbec NoData."""
    raw = _read(dem_raw)
    filled = _read(dem_filled)
    surf = _read(dsm)
    valid = filled > NODATA / 2
    chm = np.where(valid, np.maximum(surf - filled, 0.0), NODATA)
    chm = np.where(valid & ((surf <= NODATA / 2) | (raw <= NODATA / 2)), 0.0, chm).astype("float32")
    return _write_like(dem_filled, chm, dest)


def prepare_raster_surfaces(
    work_dir: Path,
    bounds: tuple[float, float, float, float],
    *,
    source: str,
    resolution_m: float = 1.0,
    force_refresh: bool = False,
    log=None,
) -> DemPrepResult:
    """DTM + DSM zdroje → ``work/dem`` na mřížce mapy (stejné soubory jako ``dem_prep``)."""
    del force_refresh  # cache zdrojů řeší provider
    provider = resolve_provider(source)
    dem_dir = dem_work_dir(Path(work_dir))
    src_dir = dem_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    label = SOURCE_LABELS.get(source, source)
    log_step(log, f"Stahuji výšková data ({label}): terén a povrch jako rastr")
    src = provider(tuple(bounds), src_dir, log)
    dtm_src, dsm_src = src.dtm, src.dsm
    if dsm_src is None:
        # Výška porostu vždy z modelu povrchu (Petr 2026-11-14): bez DSM by vegetace chyběla a mapa byla bílá.
        raise ElevationUnavailable(f"zdroj výšek {source!r} nemá model povrchu (DSM / DMP)")

    dem_raw = _warp_to_grid(dtm_src, dem_dir / "dem_raw.tif", bounds, resolution_m, resampling="bilinear", log=log)
    if src.vertical_offset is not None:
        lo, hi = _subtract_vertical_offset(dem_raw, bounds, src.vertical_offset)
        if log:
            log(f"Výšky převedeny z elipsoidických na národní systém (kvazigeoid {lo:.2f}–{hi:.2f} m).")
    share = _valid_share(dem_raw)
    if share < 0.05:
        raise ElevationUnavailable(f"výřez je mimo pokrytí zdroje výšek (platných buněk {share:.0%})")
    if log:
        log(f"Terén z rastru: {share:.0%} buněk s daty, rozlišení {resolution_m:g} m")
    dem_filled = _fill(dem_raw, dem_dir / "dem_filled.tif", log=log)

    # Povrch: maximum v buňce (koruny, ne průměr s mezerami).
    dsm_raw = _warp_to_grid(dsm_src, dem_dir / "dsm_raw.tif", bounds, resolution_m, resampling="max", log=log)
    if src.vertical_offset is not None:
        _subtract_vertical_offset(dsm_raw, bounds, src.vertical_offset)
    dsm_filled = _fill(dsm_raw, dem_dir / "dsm_filled.tif", log=log)
    chm = chm_from_rasters(dem_raw, dem_filled, dsm_raw, dem_dir / "chm.tif")

    result = DemPrepResult(
        dem_raw=dem_raw,
        dem_filled=dem_filled,
        dsm_raw=dsm_raw,
        dsm_filled=dsm_filled,
        chm=chm,
        bounds=tuple(bounds),
        resolution_m=float(resolution_m),
        ground_laz="",
        surface_laz=None,
    )
    result.write_meta(dem_dir)
    (dem_dir / "elevation_source.json").write_text(
        json.dumps({"source": source, "dtm": str(dtm_src), "dsm": str(dsm_src) if dsm_src else None}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return result
