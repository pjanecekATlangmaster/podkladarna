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
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from app import settings
from app.pipeline.crs_5514 import CRS_WKT
from app.pipeline.dem_prep import DemPrepResult, dem_work_dir
from app.pipeline.job_progress import log_issue
from app.pipeline.prepare_lidar import find_tool, log_step, run_cmd
from app.pipeline.reference_gb import GB_EA_PRODUCTS, GB_EA_TILES
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


# Tvrdý strop jedné odpovědi výškové služby: dlaždice WCS mají 2–20 MB (2 × 2 km po 1 m ~16 MB), víc je chyba služby
# nebo dotazu, ne stahování (review PR 350).
DOWNLOAD_MAX_BYTES = 300_000_000


def _download(url: str, dest: Path, *, log=None, max_bytes: int = DOWNLOAD_MAX_BYTES) -> Path:
    import urllib.request

    from app.pipeline.fetch_openzu import USER_AGENT

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=300) as resp, tmp.open("wb") as fh:
        ctype = resp.headers.get("Content-Type", "")
        length = resp.headers.get("Content-Length")
        too_big = RuntimeError(f"odpověď výškové služby přes {max_bytes // 1_000_000} MB odmítnuta")
        if length and length.isdigit() and int(length) > max_bytes:
            fh.close()
            tmp.unlink(missing_ok=True)
            raise too_big
        size = 0
        while chunk := resp.read(1 << 20):
            size += len(chunk)
            if size > max_bytes:
                fh.close()
                tmp.unlink(missing_ok=True)
                raise too_big
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


# Odmítnutí okna, které nejde o výpadek: list do okna nezasahuje, nebo neexistuje (moře, okraj mřížky listů).
_COG_SKIP_OK = re.compile(r"outside raster extent|HTTP response code: 404|does not exist in the file system", re.I)


def cog_windows(
    urls: list[str],
    window: tuple[float, float, float, float],
    crs: str,
    out_dir: Path,
    prefix: str,
    *,
    log=None,
    nodata: float | None = None,
) -> Path | None:
    """Okno ``window`` (v ``crs``) z každého vzdáleného COG přes /vsicurl → VRT; None, když žádné okno nevyšlo.

    Čte se jen část dlaždice v okně (COG: swisstopo 1 km ~15 MB, BEV 50 km ~10 GB). Dlaždice, která do okna
    nezasahuje, gdal_translate odmítne – přeskočí se. ``nodata`` pro zdroje bez NoData: okno přesahující list se jinak
    doplní nulou a ta ve VRT přepíše sousední list (Estonsko DSM)."""
    xmin, ymin, xmax, ymax = window
    nodata_args = ["-a_nodata", f"{nodata:g}"] if nodata is not None else []
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
                    *nodata_args,
                    "-co", "COMPRESS=DEFLATE",
                    f"/vsicurl/{url}",
                    str(dest),
                ],
                log=log,
            )
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or "").strip()
            if log:
                log(f"  {url.rsplit('/', 1)[-1]}: přeskočeno ({err[-200:]})")
            if not _COG_SKIP_OK.search(err):
                # Síťová chyba / 5xx (ne „list do okna nezasahuje“ nebo neexistuje): kus výřezu bude bez terénu
                # nebo porostu a díru zalije fillnodata – do souhrnu jobu, ne jen do logu.
                log_issue(log, f"Výškové modely: list {url.rsplit('/', 1)[-1]} se nestáhl, část výřezu může být bez "
                               f"terénu nebo porostu ({err[-120:]})")
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


# --------------------------------------------------------------------------- DE-NW: DGM1 + nDOM50 (Geobasis NRW)

# Severní Porýní-Vestfálsko (dl-de/zero 2.0, bez klíče): terén DGM1, zimní DOM1 z ALS a výška objektů nDOM z letního
# obrazového DOM (``reference_de_nw``) z WCS po dlaždicích 2 km, vše po 1 m ve stejné mřížce (hrany na celé metry).
# Výška porostu = menší z obou: ALS je starší a zimní (vytěžené kůrovcové smrčiny v něm zůstávají lesem; Willebadessen
# 2 × 2 km 7,5 % plochy), letní obrazový DOM vidí obilí a kukuřici jako porost a rozmazává okraje budov (Werther: pole
# 406). Shodně vysoko vychází jen skutečný porost; listnáče v zimním ALS chybí jen na ~5 % plochy lesa.
DE_NW_MAX_SIDE_M = 2000.0
DE_NW_BUFFER_M = 20.0


def de_nw_tile_urls(kind: str, bounds_utm: tuple[float, float, float, float]) -> list[str]:
    from app.pipeline.reference_de_nw import de_nw_coverage_url

    boxes = tile_boxes(bounds_utm, max_side=DE_NW_MAX_SIDE_M, buffer=DE_NW_BUFFER_M, step=1.0)
    return [de_nw_coverage_url(kind, b) for b in boxes]


def _de_nw_surface(dtm: Path, ndom: Path | None, dsm: Path | None, dest: Path, *, log=None) -> Path:
    """DGM + výška porostu = min(DOM − DGM, nDOM) (obojí aspoň 0) v mřížce dlaždice DGM; jen jeden zdroj = ten."""
    from osgeo import gdal

    gdal.UseExceptions()
    t_ds = gdal.Open(str(dtm))
    band = t_ds.GetRasterBand(1)
    nd = band.GetNoDataValue()
    t = band.ReadAsArray().astype("float32")
    bad = ~np.isfinite(t) | (t <= NODATA + 1) | ((t == nd) if nd is not None else False)

    def height(path: Path | None, *, relative: bool) -> np.ndarray | None:
        if path is None:
            return None
        ds = gdal.Open(str(path))
        if (ds.RasterXSize, ds.RasterYSize) != (t_ds.RasterXSize, t_ds.RasterYSize) or not np.allclose(
            ds.GetGeoTransform(), t_ds.GetGeoTransform(), atol=1e-3
        ):
            # jiná mřížka než DGM: tento povrch vynechat, druhý stačí (review PR 350)
            log_issue(log, f"Severní Porýní-Vestfálsko: {path.name} má jinou mřížku než DGM "
                           f"({ds.RasterXSize}×{ds.RasterYSize} proti {t_ds.RasterXSize}×{t_ds.RasterYSize}), vynechán")
            return None
        a = ds.GetRasterBand(1).ReadAsArray().astype("float32")
        ok = np.isfinite(a) & (a > NODATA + 1)
        h = a if relative else a - t
        return np.where(ok, np.clip(h, 0.0, None), np.nan)

    hs = [h for h in (height(ndom, relative=True), height(dsm, relative=False)) if h is not None]
    if not hs:
        raise RuntimeError("žádný povrch nemá mřížku DGM")
    h = np.fmin(hs[0], hs[1]) if len(hs) == 2 else hs[0]
    out = np.where(bad, NODATA, t + np.nan_to_num(h, nan=0.0)).astype("float32")
    ds = gdal.GetDriverByName("GTiff").Create(str(dest), t_ds.RasterXSize, t_ds.RasterYSize, 1, gdal.GDT_Float32,
                                              ["COMPRESS=DEFLATE"])
    ds.SetGeoTransform(t_ds.GetGeoTransform())
    ds.SetProjection(t_ds.GetProjection())
    ob = ds.GetRasterBand(1)
    ob.SetNoDataValue(NODATA)
    ob.WriteArray(out)
    ob.FlushCache()
    ds = t_ds = None
    return dest


def de_nrw_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """DGM1 a povrch DGM1 + min(zimní DOM1 z ALS − DGM, letní nDOM50) z WCS Geobasis NRW."""
    from concurrent.futures import ThreadPoolExecutor

    from app.pipeline.reference_de_nw import DE_NW_CRS

    bounds_utm = _bounds_in(bounds, CRS_WKT, DE_NW_CRS)

    def fetch(kind: str) -> list[Path]:
        urls = de_nw_tile_urls(kind, bounds_utm)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix=f"nrw-{kind}") as ex:
            return list(ex.map(lambda j: _download_retry(j[1], out_dir / f"nrw_{kind}_{j[0]}.tif", log=log),
                               enumerate(urls)))

    def fetch_optional(kind: str, label: str) -> list[Path] | None:
        try:
            return fetch(kind)
        except Exception as exc:  # noqa: BLE001 – druhý zdroj povrchu stačí, job běží dál
            log_issue(log, f"Severní Porýní-Vestfálsko: {label} se nestáhl ({str(exc)[:120]})")
            return None

    def vrt(name: str, tiles: list[Path]) -> Path:
        path = out_dir / f"de_nrw_{name}.vrt"
        run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", DE_NW_CRS, str(path), *map(str, tiles)], log=log)
        return path

    dtm_tiles = fetch("dtm")
    ndom = fetch_optional("ndom", "letní nDOM50 (pole a holiny mohou vyjít nepřesně)")
    dsm = fetch_optional("dsm", "zimní DOM1 (pole s plodinami mohou vyjít jako porost)")
    if ndom is None and dsm is None:
        return SourceRasters(vrt("dtm", dtm_tiles), None)
    try:
        surf = [_de_nw_surface(t, ndom[i] if ndom else None, dsm[i] if dsm else None,
                               out_dir / f"nrw_surface_{i}.tif", log=log)
                for i, t in enumerate(dtm_tiles)]
    except RuntimeError as exc:
        log_issue(log, f"Severní Porýní-Vestfálsko: povrch nejde složit ({str(exc)[:120]}), výška porostu chybí")
        return SourceRasters(vrt("dtm", dtm_tiles), None)
    if log and ndom and dsm:
        log("  Povrch = DGM1 + menší z výšek zimního DOM1 (ALS) a letního nDOM50 (snímky)")
    return SourceRasters(vrt("dtm", dtm_tiles), vrt("dsm", surf))


PROVIDERS["de_nrw"] = de_nrw_provider


# --------------------------------------------------------------------------- PL: NMT + NMPT (GUGiK)

# Polsko (GUGiK, bezplatně k libovolnému využití, uvést zdroj): NMT jako Float32 GeoTIFF po 2 km, NMPT jen jako ARC/INFO
# ASCII po 1 km (GeoTIFF služba vrací obarvený obrázek), po jedné dlaždici – souběžné dotazy server zahazuje
# (``reference_pl``). Obojí 1 m ve stejné mřížce (hrany na celé metry).
PL_DTM_SIDE_M = 2000.0
PL_DSM_SIDE_M = 1000.0
PL_BUFFER_M = 20.0
PL_DSM_MAX_BYTES = 40_000_000  # ASCII 1 km² po 1 m má ~20 MB
PL_RETRIES = 3
PL_DSM_TILE_DEADLINE_S = 600.0  # celkový strop jedné dlaždice včetně opakování (běžně ~40 s)


def pl_tile_boxes(bounds_pl: tuple[float, float, float, float], side: float) -> list[tuple[float, float, float, float]]:
    return tile_boxes(bounds_pl, max_side=side, buffer=PL_BUFFER_M, step=1.0)


def parse_wcs_aaigrid(raw: bytes) -> tuple[np.ndarray, tuple[float, float, float, float, float, float], float | None]:
    """Z multipart odpovědi WCS (``image/x-aaigrid``) mřížka, geotransformace (horní levý roh) a NoData."""
    text = raw.decode("ascii", "replace")
    start = text.find("ncols")
    if start < 0:
        raise RuntimeError(f"WCS nevrátil ASCII mřížku: {text[:200]}")
    end = text.find("\n--", start)
    body = text[start:end if end > 0 else None]
    lines = body.split("\n")
    head: dict[str, float] = {}
    i = 0
    while i < len(lines) and lines[i].split() and lines[i].split()[0].lower() in (
        "ncols", "nrows", "xllcorner", "yllcorner", "xllcenter", "yllcenter", "cellsize", "nodata_value"
    ):
        key, val = lines[i].split()[:2]
        head[key.lower()] = float(val)
        i += 1
    ncols, nrows, cell = int(head["ncols"]), int(head["nrows"]), head["cellsize"]
    data = np.array("\n".join(lines[i:]).split(), dtype="float32")
    if data.size != ncols * nrows:
        raise RuntimeError(f"ASCII mřížka má {data.size} hodnot místo {ncols * nrows}")
    x0 = head.get("xllcorner", head.get("xllcenter", 0.0) - cell / 2)
    y0 = head.get("yllcorner", head.get("yllcenter", 0.0) - cell / 2)
    gt = (x0, cell, 0.0, y0 + nrows * cell, 0.0, -cell)
    return data.reshape(nrows, ncols), gt, head.get("nodata_value")


def _pl_dsm_tile(url: str, dest: Path, *, log=None) -> Path:
    """Jedna dlaždice NMPT (ASCII z WCS) → Float32 GeoTIFF; prázdnou odpověď serveru zkusí znovu."""
    import time
    import urllib.error
    import urllib.request

    from osgeo import gdal, osr

    from app.pipeline.fetch_openzu import USER_AGENT

    raw = b""
    deadline = time.monotonic() + PL_DSM_TILE_DEADLINE_S
    for attempt in range(PL_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            remaining = deadline - time.monotonic()
            with urllib.request.urlopen(req, timeout=max(10.0, min(300.0, remaining))) as resp:  # noqa: S310 – pevná adresa GUGiK
                chunks: list[bytes] = []
                size = 0
                while chunk := resp.read(1 << 20):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > PL_DSM_MAX_BYTES:
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"NMPT: dlaždice nestihla {PL_DSM_TILE_DEADLINE_S:.0f} s")
                raw = b"".join(chunks)
            if raw.strip():
                break
            # prázdná odpověď 200 = server zahodil dotaz (souběh) → znovu jako chyba sítě
            if attempt >= PL_RETRIES:
                raise RuntimeError("NMPT: server vrátil prázdnou odpověď")
        except urllib.error.HTTPError as exc:
            if attempt >= PL_RETRIES or not (exc.code >= 500 or exc.code == 429):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt >= PL_RETRIES or time.monotonic() > deadline:
                raise
        if time.monotonic() + 5.0 * (attempt + 1) > deadline:
            raise TimeoutError(f"NMPT: dlaždice nestihla {PL_DSM_TILE_DEADLINE_S:.0f} s")
        if log:
            log(f"  {dest.name}: server neodpověděl, pokus {attempt + 2}/{PL_RETRIES + 1}")
        time.sleep(5.0 * (attempt + 1))
    if len(raw) > PL_DSM_MAX_BYTES:
        raise RuntimeError(f"NMPT: odpověď přes {PL_DSM_MAX_BYTES // 1_000_000} MB odmítnuta")
    grid, gt, nodata = parse_wcs_aaigrid(raw)
    if nodata is not None:
        grid[grid == nodata] = NODATA
    gdal.UseExceptions()
    ds = gdal.GetDriverByName("GTiff").Create(str(dest), grid.shape[1], grid.shape[0], 1, gdal.GDT_Float32,
                                              ["COMPRESS=DEFLATE"])
    ds.SetGeoTransform(gt)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(2180)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteArray(grid)
    band.FlushCache()
    ds = None
    if log:
        log(f"    {dest.name}: {len(raw) / 1e6:.1f} MB (ASCII)")
    return dest


def pl_gugik_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """NMT 1 m (GeoTIFF) a NMPT 1 m (ASCII) z WCS GUGiK; bez NMPT jen terén (job upozorní)."""
    from concurrent.futures import ThreadPoolExecutor

    from app.pipeline.reference_pl import PL_CRS, pl_dsm_url, pl_dtm_url

    bounds_pl = _bounds_in(bounds, CRS_WKT, PL_CRS)

    def vrt(name: str, tiles: list[Path]) -> Path:
        path = out_dir / f"pl_{name}.vrt"
        run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", PL_CRS, str(path), *map(str, tiles)], log=log)
        return path

    dsm_boxes = pl_tile_boxes(bounds_pl, PL_DSM_SIDE_M)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="pl-nmpt") as ex:  # NMPT běží vedle NMT, sám po jedné
        def all_dsm():
            return [_pl_dsm_tile(pl_dsm_url(b), out_dir / f"pl_dsm_{k}.tif", log=log) for k, b in enumerate(dsm_boxes)]

        dsm_future = ex.submit(all_dsm)
        dtm = [_download_retry(pl_dtm_url(b), out_dir / f"pl_dtm_{k}.tif", log=log)
               for k, b in enumerate(pl_tile_boxes(bounds_pl, PL_DTM_SIDE_M))]
        try:
            dsm = dsm_future.result()
        except Exception as exc:  # noqa: BLE001 – bez povrchu terén zůstane, porost chybí
            log_issue(log, f"Polsko: povrch NMPT se nestáhl ({str(exc)[:120]}), výška porostu chybí")
            return SourceRasters(vrt("dtm", dtm), None)
    return SourceRasters(vrt("dtm", dtm), vrt("dsm", dsm))


PROVIDERS["pl_gugik"] = pl_gugik_provider


# --------------------------------------------------------------------------- GB-ENG: Environment Agency LiDAR Composite

# Environment Agency (Open Government Licence v3, bez klíče): LIDAR Composite DTM a First Return DSM 2022, 1 m,
# EPSG:27700, výšky ODN (nic se nepřevádí). WCS služby na velký výřez nestíhá (504 i na 500 m), stahuje se proto
# celý ZIP dlaždice 5 × 5 km mřížky OS (Float32 GeoTIFF LZW, ~70 MB terén a ~75 MB povrch, ~5 s). Dlaždice
# se drží v cache (úklid ``download_cache.prune_lidar_cache``). Nálety jsou z větší části v zimě: listnáče mohou
# vyjít nižší (jako zimní ALS v NRW).
GB_EA_CRS = "EPSG:27700"
GB_EA_TILE_M = 5000
GB_EA_BUFFER_M = 20.0
GB_EA_MAX_ZIP_BYTES = 200_000_000  # dlaždice má ~75 MB; víc je chyba služby
# Strop počtu dlaždic na výřez (terén + povrch ~155 MB na dlaždici): víc /api/sheets i job odmítnou. Volba
# ``GB_EA_MAX_TILES`` v prostředí; výchozí 3 (~460 MB včetně povrchu, z toho terén ~230 MB).
GB_EA_MAX_TILES = int(os.environ.get("GB_EA_MAX_TILES", "3"))
GB_EA_TILE_DEADLINE_S = 600.0  # celkový strop jedné dlaždice včetně opakování (běžně 5–15 s)
GB_EA_NONE_MARK = "{kind}.none"  # značka „dlaždice mimo pokrytí“ (server vrací prázdný ZIP 22 B), aby se nestahovala znovu
_OS_LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"  # bez I


def os_grid_tile(east: float, north: float) -> str:
    """Jméno dlaždice 5 km mřížky OS (např. ``SE2530``) pro bod v EPSG:27700 (levý dolní roh)."""
    e100, n100 = int(east // 100_000), int(north // 100_000)
    if not (0 <= e100 < 7 and 0 <= n100 < 13):
        raise ValueError(f"bod {east:.0f}, {north:.0f} je mimo mřížku OS")
    first = (19 - n100) - (19 - n100) % 5 + (e100 + 10) // 5
    second = (19 - n100) * 5 % 25 + e100 % 5
    e5 = int(east % 100_000) // GB_EA_TILE_M * 5
    n5 = int(north % 100_000) // GB_EA_TILE_M * 5
    return f"{_OS_LETTERS[first]}{_OS_LETTERS[second]}{e5:02d}{n5:02d}"


def gb_ea_tiles(window: tuple[float, float, float, float]) -> list[str]:
    """Dlaždice 5 km, které zasahují do okna (EPSG:27700)."""
    import math

    xmin, ymin, xmax, ymax = window
    step = GB_EA_TILE_M
    xs = range(math.floor(xmin / step) * step, math.ceil(xmax / step) * step, step)
    ys = range(math.floor(ymin / step) * step, math.ceil(ymax / step) * step, step)
    return [os_grid_tile(x, y) for x in xs for y in ys]


def gb_ea_tile_url(kind: str, tile: str) -> str:
    return f"{GB_EA_TILES}/{GB_EA_PRODUCTS[kind]}/{tile}"


def gb_ea_tile_dir(tile: str) -> Path:
    """Složka dlaždice v cache (terén i povrch); úklid cache ji maže celou."""
    return settings.CACHE_DIR / "gb_ea" / tile


def _gb_ea_tile(kind: str, tile: str, *, log=None) -> Path | None:
    """GeoTIFF dlaždice z cache, jinak stáhne ZIP a vybalí z něj TIF.

    None mimo pokrytí: moře, Wales a Skotsko vrací server jako 200 s prázdným ZIPem (22 B), neznámý produkt jako 404
    s JSON. Výsledek „mimo“ se uloží značkou vedle dlaždic. HTML odpověď (např. „Cannot GET“ při přetížení) je
    přechodná chyba, ne mimo pokrytí."""
    import shutil
    import tempfile
    import time
    import urllib.error
    import urllib.request
    import zipfile

    from app.download_cache import touch_last_used
    from app.pipeline.fetch_openzu import USER_AGENT

    folder = gb_ea_tile_dir(tile)
    dest = folder / f"{kind}.tif"
    none_mark = folder / GB_EA_NONE_MARK.format(kind=kind)
    if dest.is_file():
        touch_last_used(folder)
        return dest
    if none_mark.is_file():
        return None
    folder.mkdir(parents=True, exist_ok=True)
    fd, zname = tempfile.mkstemp(prefix=f".{kind}-", suffix=".zip", dir=folder)
    os.close(fd)
    zpath = Path(zname)
    tmp: Path | None = None
    started = time.monotonic()
    try:
        deadline = started + GB_EA_TILE_DEADLINE_S
        for attempt in range(FR_RETRIES + 1):
            req = urllib.request.Request(gb_ea_tile_url(kind, tile), headers={"User-Agent": USER_AGENT})
            try:
                timeout = max(10.0, min(300.0, deadline - time.monotonic()))
                with urllib.request.urlopen(req, timeout=timeout) as resp, zpath.open("wb") as fh:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if "html" in ctype:
                        raise ConnectionError(f"Anglie {kind} {tile}: služba vrátila HTML místo ZIPu")
                    size = 0
                    while chunk := resp.read(1 << 20):
                        size += len(chunk)
                        if size > GB_EA_MAX_ZIP_BYTES:
                            raise RuntimeError(f"Anglie {kind} {tile}: ZIP větší než {GB_EA_MAX_ZIP_BYTES // 1_000_000} MB")
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"Anglie {kind} {tile}: dlaždice nestihla {GB_EA_TILE_DEADLINE_S:.0f} s")
                        fh.write(chunk)
                break
            except urllib.error.HTTPError as exc:
                body = exc.read(2000).decode("utf-8", "replace") if exc.fp else ""
                if exc.code == 404 and "html" not in (exc.headers.get("Content-Type") or "").lower() \
                        and "Cannot GET" not in body:
                    none_mark.touch()
                    return None
                transient = exc.code >= 500 or exc.code == 429 or exc.code == 404
                if attempt >= FR_RETRIES or not transient:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt >= FR_RETRIES or time.monotonic() > deadline:
                    raise
            if time.monotonic() + 5.0 * (attempt + 1) > deadline:
                raise TimeoutError(f"Anglie {kind} {tile}: dlaždice nestihla {GB_EA_TILE_DEADLINE_S:.0f} s")
            if log:
                log(f"    {tile} {kind}: přechodná chyba služby, pokus {attempt + 2}/{FR_RETRIES + 1}")
            time.sleep(5.0 * (attempt + 1))
        with zipfile.ZipFile(zpath) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".tif")]
            if not names:
                # prázdný ZIP (22 B) = dlaždice mimo pokrytí (moře, Wales, Skotsko)
                none_mark.touch()
                if log:
                    log(f"    {tile} {kind}: mimo pokrytí (prázdný ZIP)")
                return None
            fd, tname = tempfile.mkstemp(prefix=f".{kind}-", suffix=".tif", dir=folder)
            os.close(fd)
            tmp = Path(tname)
            with zf.open(names[0]) as src, tmp.open("wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
        tmp.replace(dest)
        tmp = None
    finally:
        zpath.unlink(missing_ok=True)
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    touch_last_used(folder)
    if log:
        log(f"    {tile} {kind}: {dest.stat().st_size / 1e6:.1f} MB za {time.monotonic() - started:.0f} s")
    return dest


def gb_ea_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """Anglie: LIDAR Composite DTM a First Return DSM 1 m (Environment Agency), dlaždice 5 km v cache."""
    from concurrent.futures import ThreadPoolExecutor

    window = _buffered(bounds, GB_EA_CRS, GB_EA_BUFFER_M)
    tiles = gb_ea_tiles(window)
    if len(tiles) > GB_EA_MAX_TILES + 1:  # bránu dělá /api/sheets (``TILE_LIMITS``); +1 na zaokrouhlení mřížky jobu
        raise ElevationUnavailable(
            f"výřez zasahuje do {len(tiles)} dlaždic 5 km Environment Agency (strop {GB_EA_MAX_TILES}); zmenšete nebo posuňte výřez"
        )
    out: dict[str, Path | None] = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        found = {kind: ex.map(lambda t, k=kind: _gb_ea_tile(k, t, log=log), tiles) for kind in GB_EA_PRODUCTS}
        found = {kind: [t for t in paths if t is not None] for kind, paths in found.items()}
    for kind, paths in found.items():
        if paths:
            vrt = out_dir / f"gb_ea_{kind}.vrt"
            run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", GB_EA_CRS, str(vrt), *map(str, paths)], log=log)
            out[kind] = vrt if vrt.is_file() else None
        else:
            out[kind] = None
    if out["dtm"] is None:
        raise ElevationUnavailable("výřez je mimo pokrytí LIDAR Composite DTM (Environment Agency)")
    if out["dsm"] is None:
        log_issue(log, "Anglie: povrch (First Return DSM) pro výřez chybí, výška porostu se nespočítá")
    return SourceRasters(out["dtm"], out["dsm"])


PROVIDERS["gb_ea"] = gb_ea_provider


def gb_ea_tile_count(bbox_wgs84: tuple[float, float, float, float]) -> int:
    """Počet dlaždic 5 km, které job pro výřez stáhne (bez sítě; pro odmítnutí v /api/sheets)."""
    xmin, ymin, xmax, ymax = _bounds_in(bbox_wgs84, "EPSG:4326", GB_EA_CRS)
    b = GB_EA_BUFFER_M
    return len(gb_ea_tiles((xmin - b, ymin - b, xmax + b, ymax + b)))


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


# --------------------------------------------------------------------------- EE: Maa- ja Ruumiamet DTM + DSM 1 m

# Maa- ja Ruumiamet (otevřená licence, uvést zdroj a datum): GeoTIFF 1 m po mapových listech, EPSG:3301 (L-EST97),
# výšky EH2000 (nadmořské, nic se nepřevádí), data LLS 2021–2024. DTM po listech 1 : 10 000 (5 × 5 km, ~75 MB,
# DEFLATE dlaždice 512 px) – čte se jen okno přes /vsicurl; DSM po listech 1 : 2 000 (1 × 1 km, ~4 MB).
# Adresa listu je stahovací odkaz geoportálu (``kaardiruut`` = číslo listu, ``f`` = jméno souboru).
EE_GEOPORTAL = "https://geoportaal.maaruum.ee/index.php"
EE_CRS = "EPSG:3301"
EE_BUFFER_M = 20.0
EE_NODATA = -9999.0


def ee_sheet_url(kind: str, sheet: str) -> str:
    """Stahovací odkaz listu: ``dtm`` = list 1 : 10 000 (5 km), ``dsm`` = list 1 : 2 000 (1 km)."""
    andmetyyp, name = ("dem_1m_geotiff", f"{sheet}_dtm_1m.tif") if kind == "dtm" else ("ndsm_abs_1m_geotiff", f"{sheet}_dsm_1m.tif")
    return f"{EE_GEOPORTAL}?lang_id=2&plugin_act=otsing&kaardiruut={sheet}&andmetyyp={andmetyyp}&dl=1&f={name}&page_id=664"


def ee_sheet_10k(e_m: float, n_m: float) -> str:
    """Číslo listu 1 : 10 000 s bodem (L-EST97, m): list 1 : 20 000 (10 km) + čtvrtina 1 JZ, 2 JV, 3 SZ, 4 SV.

    List 1 : 20 000 ``ABCD`` má JZ roh N = 6000 + (A − 1)·100 + C·10 km, E = (B + 2)·100 + D·10 km
    (ověřeno na listech 44744, 54752, 63843, 74331)."""
    e_km, n_km = int(e_m // 5000) * 5, int(n_m // 5000) * 5
    n0, e0 = n_km - n_km % 10, e_km - e_km % 10
    a, c = (n0 - 6000) // 100 + 1, ((n0 - 6000) % 100) // 10
    b, d = e0 // 100 - 2, (e0 % 100) // 10
    q = 1 + (e_km % 10 == 5) + 2 * (n_km % 10 == 5)
    return f"{a}{b}{c}{d}{q}"


def ee_sheet_2k(e_m: float, n_m: float) -> str:
    """Číslo listu 1 : 2 000 (1 × 1 km): severní km − 6000 a východní km JZ rohu (např. 589542)."""
    return f"{int(n_m // 1000) - 6000:03d}{int(e_m // 1000):03d}"


def ee_sheets(kind: str, window: tuple[float, float, float, float]) -> list[str]:
    import math

    step = 5000 if kind == "dtm" else 1000
    fn = ee_sheet_10k if kind == "dtm" else ee_sheet_2k
    xmin, ymin, xmax, ymax = window
    out = []
    for i in range(math.floor(xmin / step), math.floor(xmax / step) + 1):
        for j in range(math.floor(ymin / step), math.floor(ymax / step) + 1):
            out.append(fn(i * step + 1, j * step + 1))
    return out


def ee_maaamet_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """Maa- ja Ruumiamet: DTM (okno z listů 5 km) a DSM (listy 1 km) 1 m přes /vsicurl, bez klíče."""
    window = _buffered(bounds, EE_CRS, EE_BUFFER_M)
    out = {
        kind: cog_windows(
            [ee_sheet_url(kind, s) for s in ee_sheets(kind, window)], window, EE_CRS, out_dir, f"ee_{kind}",
            log=log, nodata=EE_NODATA,  # listy DSM NoData nemají
        )
        for kind in ("dtm", "dsm")
    }
    if out["dtm"] is None:
        raise ElevationUnavailable("výřez je mimo pokrytí výšek Maa- ja Ruumiamet")
    return SourceRasters(out["dtm"], out["dsm"])


PROVIDERS["ee_maaamet"] = ee_maaamet_provider


# --------------------------------------------------------------------------- IT: Trentino DTM + DSM 0,5 m (PAT)

# Provincia autonoma di Trento (CC BY 4.0, bez klíče): LiDAR 2014 – DTM a DSM first 0,5 m, dlaždice 500 m (ESRI ASCII
# v ZIPu, ~2 MB), EPSG:25832, výšky nadmořské (nic se nepřevádí). Index dlaždic je WFS (``n_tavola``, ``location``);
# soubory vydává asynchronní služba „merge“ geoportálu (POST seznamu → id + token → stav → ZIP se ZIPy dlaždic).
# Dlaždice se ukládají do cache jako GeoTIFF (``tn_tile_dir``), úklid je maže jako listy LiDARu.
TN_WFS = "https://siat.provincia.tn.it/geoserver/wfs"
TN_SERVICES = "https://siat.provincia.tn.it/stem/services"
TN_CRS = "EPSG:25832"
TN_LAYERS = {"dtm": "stem:inqlid2014_dtm_asc", "dsm": "stem:inqlid2014_dsmf_asc"}
TN_TILE_M = 500
TN_BUFFER_M = 20.0
TN_BATCH = 40  # souborů na jednu žádost (služba má strop 500 MB, dlaždice ~2 MB)
TN_WAIT_S = 900.0
TN_POLL_S = 3.0
TN_MAX_ZIP_BYTES = 400_000_000
_TN_INNER = re.compile(r"^(5h\d{9})_(DTM|DSMFirst)\.asc\.zip$")


def _tn_request(url: str, data: bytes | None = None, *, timeout: float = 60.0) -> bytes:
    import urllib.request

    from app.pipeline.fetch_openzu import USER_AGENT

    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(TN_MAX_ZIP_BYTES + 1)


def tn_tiles(kind: str, window: tuple[float, float, float, float]) -> dict[str, str]:
    """Dlaždice zasahující do okna (EPSG:25832): číslo dlaždice → cesta souboru pro službu merge."""
    xmin, ymin, xmax, ymax = window
    url = (
        f"{TN_WFS}?service=WFS&version=2.0.0&request=GetFeature&typeNames={TN_LAYERS[kind]}&count=5000"
        f"&outputFormat=application/json&srsName={TN_CRS}&bbox={xmin:.1f},{ymin:.1f},{xmax:.1f},{ymax:.1f},{TN_CRS}"
    )
    out = {}
    for feat in json.loads(_tn_request(url)).get("features") or []:
        p = feat.get("properties") or {}
        x0, y0 = float(p.get("x_min", 0)), float(p.get("y_min", 0))
        # WFS vrací i dlaždice, které se oknu jen dotýkají hranou
        if p.get("n_tavola") and p.get("location") and x0 < xmax and x0 + TN_TILE_M > xmin and y0 < ymax and y0 + TN_TILE_M > ymin:
            out[str(p["n_tavola"])] = str(p["location"])
    return out


def tn_tile_dir(tile: str) -> Path:
    """Složka dlaždice v cache (terén i povrch); úklid cache (``download_cache.prune_lidar_cache``) ji maže celou."""
    return settings.CACHE_DIR / "it_trentino" / tile


def _tn_merge(locations: list[str], dest: Path, *, log=None) -> Path:
    """Jedna žádost služby merge: počká na sloučení a stáhne ZIP se ZIPy dlaždic do ``dest``."""
    import time

    resp = json.loads(_tn_request(f"{TN_SERVICES}/merge", json.dumps({"files": locations}).encode()))
    query = f"id={resp['id']}&token={resp['token']}"
    deadline = time.monotonic() + TN_WAIT_S
    while True:
        info = json.loads(_tn_request(f"{TN_SERVICES}/file/info?{query}", timeout=30.0))
        if info.get("state") == 1:
            break
        if info.get("state") not in (0, None) or time.monotonic() > deadline:
            raise RuntimeError(f"Trentino: služba merge nevydala soubory (stav {info.get('state')})")
        time.sleep(TN_POLL_S)
    data = _tn_request(f"{TN_SERVICES}/file?{query}", timeout=600.0)
    if len(data) > TN_MAX_ZIP_BYTES:
        raise RuntimeError(f"Trentino: ZIP větší než {TN_MAX_ZIP_BYTES // 1_000_000} MB")
    dest.write_bytes(data)
    if log:
        log(f"  Trentino: {len(locations)} dlaždic, {len(data) / 1e6:.1f} MB")
    return dest


def _tn_store(outer: Path, *, log=None) -> None:
    """Dlaždice z výsledku merge (ZIP se ZIPy ASCII gridů) převede na GeoTIFF do cache."""
    import tempfile
    import zipfile

    from app.download_cache import touch_last_used

    with tempfile.TemporaryDirectory(prefix="tn-", dir=outer.parent) as tmp_dir, zipfile.ZipFile(outer) as zf:
        for name in zf.namelist():
            m = _TN_INNER.match(Path(name).name)
            if not m:
                continue
            tile, kind = m.group(1), "dtm" if m.group(2) == "DTM" else "dsm"
            inner = Path(tmp_dir) / f"{tile}_{kind}.zip"
            with zf.open(name) as src, inner.open("wb") as fh:
                while chunk := src.read(1 << 20):
                    fh.write(chunk)
            folder = tn_tile_dir(tile)
            folder.mkdir(parents=True, exist_ok=True)
            fd, tname = tempfile.mkstemp(prefix=f".{kind}-", suffix=".tif", dir=folder)
            os.close(fd)
            tmp = Path(tname)
            try:
                run_cmd(
                    [_gdal_tool("gdal_translate"), "-q", "-a_srs", TN_CRS, "-a_nodata", f"{NODATA:g}", "-ot", "Float32",
                     "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", f"/vsizip/{inner}/{m.group(0)[:-4]}", str(tmp)],
                )  # bez logu příkazu: MTBO má stovky dlaždic, chyba se vyhodí i tak
                if not tmp.is_file() or tmp.stat().st_size == 0:
                    raise RuntimeError(f"Trentino {kind} {tile}: převod ASCII gridu selhal")
                tmp.replace(folder / f"{kind}.tif")
                tmp = None
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
            touch_last_used(folder)


def it_trentino_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """Trentino: DTM a DSM first 0,5 m (LiDAR PAT 2014), dlaždice 500 m ze služby merge, cache po dlaždicích."""
    window = _buffered(bounds, TN_CRS, TN_BUFFER_M)
    wanted = {kind: tn_tiles(kind, window) for kind in TN_LAYERS}
    missing = [loc for kind, tiles in wanted.items() for tile, loc in sorted(tiles.items())
               if not (tn_tile_dir(tile) / f"{kind}.tif").is_file()]
    if log:
        cached = sum(map(len, wanted.values())) - len(missing)
        log(f"  Trentino: {sum(map(len, wanted.values()))} dlaždic (z mezipaměti {cached})")
    for k in range(0, len(missing), TN_BATCH):
        outer = _tn_merge(missing[k:k + TN_BATCH], out_dir / f"tn_merge_{k // TN_BATCH}.zip", log=log)
        try:
            _tn_store(outer, log=log)
        finally:
            outer.unlink(missing_ok=True)
    out: dict[str, Path | None] = {}
    for kind, tiles in wanted.items():
        files = [str(f) for t in sorted(tiles) if (f := tn_tile_dir(t) / f"{kind}.tif").is_file()]
        out[kind] = None
        if files:
            vrt = out_dir / f"it_trentino_{kind}.vrt"
            run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", TN_CRS, str(vrt), *files], log=log)
            out[kind] = vrt if vrt.is_file() else None
    if out["dtm"] is None:
        raise ElevationUnavailable("výřez je mimo pokrytí LiDARu Trentina")
    return SourceRasters(out["dtm"], out["dsm"])


PROVIDERS["it_trentino"] = it_trentino_provider


def it_trentino_coverage(bbox_wgs84: tuple[float, float, float, float]) -> float:
    """Podíl výřezu s dlaždicemi DTM i DSM Trentina (výřez přes hranici provincie)."""
    from shapely.geometry import box
    from shapely.ops import unary_union

    window = _bounds_in(bbox_wgs84, "EPSG:4326", TN_CRS)
    common = set(tn_tiles("dtm", window)) & set(tn_tiles("dsm", window))
    boxes = [box(x, y, x + TN_TILE_M, y + TN_TILE_M) for x, y in map(tn_tile_origin, common)]
    area = box(*window).area
    return float(unary_union(boxes).intersection(box(*window)).area / area) if boxes and area > 0 else 0.0


def tn_tile_origin(tile: str) -> tuple[float, float]:
    """JZ roh dlaždice z čísla: ``5h`` + východ / 100 (4 číslice) + sever / 100 (5 číslic), např. 5h663050995."""
    digits = tile[2:]
    return float(digits[:4]) * 100, float(digits[4:]) * 100


# --------------------------------------------------------------------------- BE: DHMV II DTM + DSM 1 m (Flandry)

# Digitaal Hoogtemodel Vlaanderen II (LiDAR 2013–2015, Modellicentie Gratis Hergebruik, bez klíče): DTM a DSM 1 m
# z WCS Digitaal Vlaanderen, nativně EPSG:31370 (Belgian Lambert 72), výšky TAW (nadmořské, nic se nepřevádí).
# WCS 1.0.0 vrací čistý GeoTIFF (2.0.1 multipart); dlaždice 2 × 2 km ≈ 16 MB. Pokrývá Flandry i Brusel.
BE_WCS = "https://geo.api.vlaanderen.be/DHMV/wcs"
BE_CRS = "EPSG:31370"
BE_COVERAGES = {"dtm": "DHMVII_DTM_1m", "dsm": "DHMVII_DSM_1m"}
BE_MAX_SIDE_M = 2000.0
BE_BUFFER_M = 20.0


def be_dhmv_urls(kind: str, bounds_lb72: tuple[float, float, float, float]) -> list[str]:
    """GetCoverage URL po dlaždicích nejvýš ``BE_MAX_SIDE_M`` (Lambert 72, hrany na celé metry, pixel 1 m)."""
    out = []
    for x0, y0, x1, y1 in tile_boxes(bounds_lb72, max_side=BE_MAX_SIDE_M, buffer=BE_BUFFER_M, step=1.0):
        out.append(
            f"{BE_WCS}?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage&COVERAGE={BE_COVERAGES[kind]}&CRS={BE_CRS}"
            f"&BBOX={x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}&WIDTH={round(x1 - x0)}&HEIGHT={round(y1 - y0)}&FORMAT=GeoTIFF"
        )
    return out


def be_dhmv_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """DHMV II: DTM (terén) a DSM (povrch) 1 m z WCS Digitaal Vlaanderen po dlaždicích 2 km."""
    from concurrent.futures import ThreadPoolExecutor

    bounds_lb72 = _bounds_in(bounds, CRS_WKT, BE_CRS)
    jobs = [(kind, k, url) for kind in BE_COVERAGES for k, url in enumerate(be_dhmv_urls(kind, bounds_lb72))]
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="be-dhmv") as ex:
        tiles = list(ex.map(lambda j: _download_retry(j[2], out_dir / f"be_{j[0]}_{j[1]}.tif", log=log), jobs))
    out = {}
    for kind in BE_COVERAGES:
        vrt = out_dir / f"be_dhmv_{kind}.vrt"
        mine = [str(t) for (k, _i, _u), t in zip(jobs, tiles) if k == kind]
        run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", BE_CRS, str(vrt), *mine], log=log)
        out[kind] = vrt
    return SourceRasters(out["dtm"], out["dsm"])


PROVIDERS["be_dhmv"] = be_dhmv_provider


# --------------------------------------------------------------------------- FI: MML DTM 2 m + CHM Metsäkeskus 1 m

# Terén: Korkeusmalli 2 m (Maanmittauslaitos, CC BY 4.0) ze zrcadla Funet (bez klíče; přímo u MML je potřeba API
# klíč). Povrch otevřeně není (mračno 5 b/m² je placené), proto DSM = DTM + výška porostu: Latvusmalli (CHM) 1 m
# Metsäkeskus (CC BY 4.0, nejnovější ročník listu ``uusin``). Obojí po listech TM35 1 : 10 000 (6 × 6 km, GeoTIFF
# po blocích 256 px) – čte se jen okno výřezu přes /vsicurl. Výšky N2000, nic se nepřevádí.
FI_CRS = "EPSG:3067"
FI_DTM_URL = "https://mirrors.nic.funet.fi/index/geodata/mml/dem2m/2008_latest/{a}/{b}/{sheet}.tif"
FI_CHM_URL = "https://avoin.metsakeskus.fi/aineistot/Latvusmalli/Karttalehti/uusin/CHM_{sheet}_uusin.tif"
FI_BUFFER_M = 20.0
FI_SHEET_M = 6000
FI_GRID_E0 = -76000  # západní okraj mřížky listů TM35 (sloupec 2 začíná na E = -76 000 + 192 000)
_FI_ROWS = "KLMNPQRSTUVWX"  # řady listů 1 : 200 000 po 96 km od N 6 570 000 (bez O)


def fi_sheet(east: float, north: float) -> str:
    """List TM35 1 : 10 000 (např. ``L4133B``): 1 : 200 000 (192 × 96 km) → 3× čtvrtiny (1 vlevo dole, 2 vlevo
    nahoře, 3 vpravo dole, 4 vpravo nahoře) → 1 : 25 000 (24 × 12 km) rozdělený na 4 × 2 listy A–H po 6 km."""
    import math

    row = math.floor((north - 6570000) / 96000)
    col = math.floor((east - FI_GRID_E0) / 192000)
    name = f"{_FI_ROWS[row]}{col + 2}"
    x0, y0, w, h = FI_GRID_E0 + col * 192000, 6570000 + row * 96000, 192000.0, 96000.0
    for _ in range(3):
        w, h = w / 2, h / 2
        c, r = int((east - x0) // w), int((north - y0) // h)
        name += str(1 + 2 * c + r)
        x0, y0 = x0 + c * w, y0 + r * h
    c, r = int((east - x0) // FI_SHEET_M), int((north - y0) // FI_SHEET_M)
    return name + "ABCDEFGH"[c * 2 + r]


def fi_sheets(window: tuple[float, float, float, float]) -> list[str]:
    """Listy 6 × 6 km, které zasahují do okna (EPSG:3067)."""
    import math

    xmin, ymin, xmax, ymax = window
    step = FI_SHEET_M
    e0 = FI_GRID_E0 % step  # mřížka listů začíná na E = -76 000 (posun 2 km proti násobkům 6 km), N na násobcích 6 km
    out: list[str] = []
    for e in range(math.floor((xmin - e0) / step) * step + e0, math.floor((xmax - e0) / step) * step + e0 + 1, step):
        for n in range(math.floor(ymin / step) * step, math.floor(ymax / step) * step + 1, step):
            sheet = fi_sheet(e + step / 2, n + step / 2)
            if sheet not in out:
                out.append(sheet)
    return out


def fi_sheet_url(kind: str, sheet: str) -> str:
    if kind == "dtm":
        return FI_DTM_URL.format(a=sheet[:2], b=sheet[:3], sheet=sheet)
    return FI_CHM_URL.format(sheet=sheet)


# DSM = DTM + výška porostu; buňka bez CHM (NoData) = holý terén, bez DTM NoData.
_FI_DSM_CALC = "where(A<-9000,-9999,A+where(B>0,B,0))"


def _fi_surface_numpy(dtm1: Path, chm1: Path, dest: Path) -> None:
    """Totéž co ``_FI_DSM_CALC`` přes osgeo + numpy (prostředí bez gdal_calc)."""
    import numpy as np
    from osgeo import gdal

    gdal.UseExceptions()
    dtm_ds, chm_ds = gdal.Open(str(dtm1)), gdal.Open(str(chm1))
    a = dtm_ds.GetRasterBand(1).ReadAsArray().astype("float32")
    b = chm_ds.GetRasterBand(1).ReadAsArray().astype("float32")
    out = np.where(a < -9000, NODATA, a + np.where(b > 0, b, 0)).astype("float32")
    ds = gdal.GetDriverByName("GTiff").Create(str(dest), dtm_ds.RasterXSize, dtm_ds.RasterYSize, 1, gdal.GDT_Float32,
                                              ["COMPRESS=DEFLATE"])
    ds.SetGeoTransform(dtm_ds.GetGeoTransform())
    ds.SetProjection(dtm_ds.GetProjection())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteArray(out)
    band.FlushCache()
    ds = dtm_ds = chm_ds = None


def _fi_surface(dtm: Path, chm: Path, window, out_dir: Path, *, log=None) -> Path:
    """DSM na mřížce 1 m okna: DTM (2 m, bilineárně) + CHM."""
    xmin, ymin, xmax, ymax = window
    grid = ["-te", f"{xmin:.1f}", f"{ymin:.1f}", f"{xmax:.1f}", f"{ymax:.1f}", "-tr", "1", "1", "-t_srs", FI_CRS]
    dtm1 = out_dir / "fi_dtm_1m.tif"
    chm1 = out_dir / "fi_chm_1m.tif"
    run_cmd([_gdal_tool("gdalwarp"), "-q", "-overwrite", *grid, "-r", "bilinear", "-dstnodata", f"{NODATA:g}",
             "-ot", "Float32", "-co", "COMPRESS=DEFLATE", str(dtm), str(dtm1)])
    run_cmd([_gdal_tool("gdalwarp"), "-q", "-overwrite", *grid, "-r", "near", "-dstnodata", f"{NODATA:g}",
             "-ot", "Float32", "-co", "COMPRESS=DEFLATE", str(chm), str(chm1)])
    dest = out_dir / "fi_dsm.tif"
    for name in ("gdal_calc", "gdal_calc.py"):  # jako CHM v ``dem_prep``: gdal_calc, jinak osgeo + numpy
        try:
            tool = find_tool(name)
        except RuntimeError:
            continue
        run_cmd([tool, "-A", str(dtm1), "-B", str(chm1), "--outfile", str(dest), "--calc", _FI_DSM_CALC,
                 "--hideNoData", "--type", "Float32", "--NoDataValue", f"{NODATA:g}", "--co", "COMPRESS=DEFLATE",
                 "--overwrite"])
        break
    else:
        _fi_surface_numpy(dtm1, chm1, dest)
    if log:
        log("  DSM = terén MML + výška porostu Metsäkeskus (1 m)")
    return dest


def fi_mml_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """Terén MML 2 m a povrch složený z terénu a výšky porostu Metsäkeskus 1 m, bez klíče."""
    window = _buffered(bounds, FI_CRS, FI_BUFFER_M)
    sheets = fi_sheets(window)
    dtm = cog_windows([fi_sheet_url("dtm", s) for s in sheets], window, FI_CRS, out_dir, "fi_dtm", log=log,
                      nodata=NODATA)
    if dtm is None:
        raise ElevationUnavailable("výřez je mimo pokrytí výškového modelu MML")
    chm = cog_windows([fi_sheet_url("chm", s) for s in sheets], window, FI_CRS, out_dir, "fi_chm", log=log,
                      nodata=NODATA)
    if chm is None:
        if log:
            log("  ⚠ Výška porostu Metsäkeskus pro výřez chybí – vegetace bez výšky porostu.")
        return SourceRasters(dtm, None)
    return SourceRasters(dtm, _fi_surface(dtm, chm, window, out_dir, log=log))


PROVIDERS["fi_mml"] = fi_mml_provider


# --------------------------------------------------------------------------- LU: LiDAR 2024 MNT + MNS 0,5 m (ACT)

# Relevé 3D du territoire luxembourgeois 2024 (ACT, CC0, bez klíče): MNT (terén) a MNS (povrch) 0,5 m, každý jeden COG
# na celou zemi v EPSG:2169 (LUREF), výšky nadmořské (nic se nepřevádí). Čte se jen okno výřezu (~11 s na km²).
LU_CRS = "EPSG:2169"
LU_BUFFER_M = 20.0


def lu_act_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """MNT a MNS 0,5 m z LiDARu 2024 – okno výřezu z celostátních COG data.public.lu."""
    from app.pipeline.reference_lu import LU_DSM_URL, LU_DTM_URL

    window = _buffered(bounds, LU_CRS, LU_BUFFER_M)
    dtm = cog_windows([LU_DTM_URL], window, LU_CRS, out_dir, "lu_dtm", log=log, nodata=NODATA)
    if dtm is None:
        raise ElevationUnavailable("výřez je mimo pokrytí LiDARu 2024 (Lucembursko)")
    dsm = cog_windows([LU_DSM_URL], window, LU_CRS, out_dir, "lu_dsm", log=log, nodata=NODATA)
    if dsm is None:
        return SourceRasters(dtm, None)
    try:
        from app.pipeline.lu_copc import vegetation_surface

        veg = vegetation_surface(window, out_dir, log=log)
    except Exception as exc:  # noqa: BLE001 – bez mračna zůstane MNS (listnatý les slabší), job běží dál
        veg = None
        if log:
            log(f"  Mračno LiDAR 2024 nedostupné: {str(exc)[:160]}")
    if veg is None:
        log_issue(log, "Lucembursko: mračno LiDAR 2024 se nestáhlo, výška porostu jen ze zimního MNS "
                       "(listnatý les může vyjít jako otevřený prostor)")
        return SourceRasters(dtm, dsm)
    return SourceRasters(dtm, _lu_surface(dsm, veg, window, out_dir, log=log))


def _lu_surface(dsm: Path, veg: Path, window, out_dir: Path, *, log=None) -> Path:
    """Povrch 1 m = maximum z MNS (budovy, jehličnany) a nejvyššího bodu vegetace z mračna (listnáče bez listí)."""
    from osgeo import gdal

    xmin, ymin, xmax, ymax = window
    grid = ["-te", f"{xmin:.1f}", f"{ymin:.1f}", f"{xmax:.1f}", f"{ymax:.1f}", "-tr", "1", "1", "-t_srs", LU_CRS]
    dsm1, veg1 = out_dir / "lu_dsm_1m.tif", out_dir / "lu_veg_1m.tif"
    run_cmd([_gdal_tool("gdalwarp"), "-q", "-overwrite", *grid, "-r", "max", "-dstnodata", f"{NODATA:g}",
             "-ot", "Float32", "-co", "COMPRESS=DEFLATE", str(dsm), str(dsm1)])
    run_cmd([_gdal_tool("gdalwarp"), "-q", "-overwrite", *grid, "-r", "near", "-dstnodata", f"{NODATA:g}",
             "-ot", "Float32", "-co", "COMPRESS=DEFLATE", str(veg), str(veg1)])
    gdal.UseExceptions()
    a_ds, b_ds = gdal.Open(str(dsm1)), gdal.Open(str(veg1))
    a = a_ds.GetRasterBand(1).ReadAsArray().astype("float32")
    b = b_ds.GetRasterBand(1).ReadAsArray().astype("float32")
    out = np.where(a <= NODATA + 1, NODATA, np.where(b > NODATA + 1, np.maximum(a, b), a)).astype("float32")
    dest = out_dir / "lu_dsm_veg.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(dest), a_ds.RasterXSize, a_ds.RasterYSize, 1, gdal.GDT_Float32,
                                              ["COMPRESS=DEFLATE"])
    ds.SetGeoTransform(a_ds.GetGeoTransform())
    ds.SetProjection(a_ds.GetProjection())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteArray(out)
    band.FlushCache()
    ds = a_ds = b_ds = None
    if log:
        raised = float(((b > a) & (a > NODATA + 1)).mean() * 100)
        log(f"  Povrch = MNS + nejvyšší body vegetace z mračna ({raised:.0f} % buněk zvýšeno)")
    return dest


PROVIDERS["lu_act"] = lu_act_provider


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
    "de_nrw": "DGM1, DOM1 a nDOM50, Geobasis NRW",
    "pl_gugik": "NMT a NMPT, GUGiK",
    "gb_ea": "LIDAR Composite DTM a DSM, Environment Agency",
    "fr_lidarhd": "LiDAR HD MNT a MNS, IGN",
    "ee_maaamet": "DTM a DSM, Maa- ja Ruumiamet",
    "it_trentino": "LiDAR DTM a DSM, Provincia autonoma di Trento",
    "be_dhmv": "DHMV II DTM a DSM, Digitaal Vlaanderen",
    "fi_mml": "Korkeusmalli 2 m MML a latvusmalli Metsäkeskus",
    "lu_act": "LiDAR 2024 MNT a MNS, ACT Luxembourg",
}


# Pokrytí výřezu výškami ještě před jobem (zdroje, které nejsou celoplošné): podíl 0–1.
COVERAGE: dict[str, Callable[[tuple[float, float, float, float]], float]] = {
    "fr_lidarhd": fr_lidarhd_coverage,
    "it_trentino": it_trentino_coverage,
}
# Strop počtu stahovaných dlaždic výšek na výřez (zdroje po celých velkých dlaždicích): zdroj → (počet, strop).
TILE_LIMITS: dict[str, Callable[[tuple[float, float, float, float]], tuple[int, int]]] = {
    "gb_ea": lambda bbox: (gb_ea_tile_count(bbox), GB_EA_MAX_TILES),
}
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
