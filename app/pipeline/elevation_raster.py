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
import math
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from app import settings
from app.pipeline.crs_5514 import CRS_WKT
from app.pipeline.dem_prep import DemPrepResult, dem_work_dir
from app.pipeline.job_progress import log_issue
from app.pipeline.prepare_lidar import find_tool, log_step, run_cmd
from app.pipeline.reference_by import BY_CRS, BY_DGM1_URL, BY_DOM20_URL
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


# --------------------------------------------------------------------------- vlastní data zadavatele

USER_UPLOAD = "user_upload"


def user_upload_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """Výšky nahrané zadavatelem (``app.user_sources``): ``jobs/<id>/user_sources/{dtm,dsm}``, nikdy cache.

    ``out_dir`` je ``jobs/<id>/work/dem/src`` (``prepare_raster_surfaces``).
    """
    from app.user_sources import job_sources_dir

    base = job_sources_dir(Path(out_dir).parents[2])
    return local_provider(f"local:{base.resolve()}")(bounds, out_dir, log=log)


PROVIDERS[USER_UPLOAD] = user_upload_provider


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
    if ctype.lower().startswith("multipart/"):
        ctype = _multipart_tiff(tmp) or ctype
    if "tiff" not in ctype.lower() or tmp.stat().st_size < 500:
        detail = tmp.read_bytes()[:300].decode("utf-8", "replace")
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"WCS nevrátil GeoTIFF ({ctype}): {detail}")
    tmp.replace(dest)
    if log:
        log(f"  {dest.name}: {dest.stat().st_size / 1e6:.1f} MB")
    return dest


def _multipart_tiff(path: Path) -> str | None:
    """Z multipart odpovědi WCS 2.0.1 (GML + TIFF, MapServer) nechá v souboru jen TIFF; vrátí jeho Content-Type."""
    raw = path.read_bytes()
    if not raw.startswith(b"--"):
        return None
    boundary = raw[: raw.find(b"\n")].rstrip(b"\r")
    head = raw.find(b"Content-Type: image/tiff")
    if head < 0:
        return None
    m = re.compile(rb"\r?\n\r?\n").search(raw, head)
    if m is None or raw[m.end():m.end() + 4] not in (b"II*\x00", b"MM\x00*"):
        return None
    end = raw.find(b"\n" + boundary, m.end())
    end = len(raw) if end < 0 else end - (raw[end - 1:end] == b"\r")
    path.write_bytes(raw[m.end():end])
    return "image/tiff"


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
    resolution: str | None = None,
) -> Path | None:
    """Okno ``window`` (v ``crs``) z každého vzdáleného COG přes /vsicurl → VRT; None, když žádné okno nevyšlo.

    Čte se jen část dlaždice v okně (COG: swisstopo 1 km ~15 MB, BEV 50 km ~10 GB). Dlaždice, která do okna
    nezasahuje, gdal_translate odmítne – přeskočí se. ``nodata`` pro zdroje bez NoData: okno přesahující list se jinak
    doplní nulou a ta ve VRT přepíše sousední list (Estonsko DSM). ``resolution`` pro gdalbuildvrt (``highest`` při
    listech s různým rozlišením; výchozí průměr). Při překryvu má přednost dlaždice později v ``urls``."""
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
    res_args = ["-resolution", resolution] if resolution else []
    run_cmd([_gdal_tool("gdalbuildvrt"), *res_args, "-a_srs", crs, str(vrt), *map(str, tiles)], log=log)
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


def _de_nw_surface(dtm: Path, ndom: Path | None, dsm: Path | None, dest: Path, *, log=None,
                   label: str = "Severní Porýní-Vestfálsko") -> Path:
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
            log_issue(log, f"{label}: {path.name} má jinou mřížku než DGM "
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


# --------------------------------------------------------------------------- DE: země s DGM1 + DOM1 z WCS 2.0.1

# Sasko-Anhaltsko a další země se stejným typem služby (tabulka ``reference_de_states.DE_WCS_STATES``): terén DGM1
# a povrch DOM1 (ALS) po 1 m z WCS zemského úřadu po dlaždicích 2 km v nativním UTM. Povrch je nepovinný: bez něj
# mapa vznikne bez výšky porostu (log ⚠).
DE_WCS_MAX_SIDE_M = 2000.0
DE_WCS_BUFFER_M = 20.0


def de_wcs_tile_urls(key: str, kind: str, bounds_utm: tuple[float, float, float, float]) -> list[str]:
    from app.pipeline.reference_de_states import DE_WCS_STATES, wcs_coverage_url

    st = DE_WCS_STATES[key]
    service = st.dtm if kind == "dtm" else st.dsm
    boxes = tile_boxes(bounds_utm, max_side=DE_WCS_MAX_SIDE_M, buffer=DE_WCS_BUFFER_M, step=1.0)
    return [wcs_coverage_url(service, b, st.axes) for b in boxes]


def de_wcs_provider(key: str) -> Provider:
    """Provider výšek země z ``DE_WCS_STATES`` (DGM1 + DOM1 z WCS po dlaždicích 2 km)."""

    def provider(bounds, out_dir: Path, log=None) -> SourceRasters:
        from concurrent.futures import ThreadPoolExecutor

        from app.pipeline.reference_de_states import DE_WCS_STATES

        st = DE_WCS_STATES[key]
        bounds_utm = _bounds_in(bounds, CRS_WKT, st.crs)

        def fetch(kind: str) -> Path:
            urls = de_wcs_tile_urls(key, kind, bounds_utm)
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix=f"{key}-{kind}") as ex:
                tiles = list(ex.map(lambda j: _download_retry(j[1], out_dir / f"{key}_{kind}_{j[0]}.tif", log=log),
                                    enumerate(urls)))
            vrt = out_dir / f"{key}_{kind}.vrt"
            # -a_srs: MapServer Saska-Anhaltska vrací TIFF bez CRS
            run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", st.crs, str(vrt), *map(str, tiles)], log=log)
            return vrt

        dtm = fetch("dtm")
        try:
            dsm: Path | None = fetch("dsm")
        except Exception as exc:  # noqa: BLE001 – bez povrchu mapa vznikne, jen bez výšky porostu
            log_issue(log, f"{st.name}: povrch DOM1 se nestáhl ({str(exc)[:120]}), výška porostu chybí")
            dsm = None
        return SourceRasters(dtm, dsm)

    return provider


# Dlaždice 1 km podle indexu Metalink (Porýní-Falc): index (~12 MB) v cache, obnova po týdnu, při výpadku starý.
DE_META4_MAX_AGE_S = 7 * 86400
DE_META4_MAX_BYTES = 60_000_000
DE_TILE_KM = 1000


def _meta4_cache(url: str) -> Path:
    return settings.CACHE_DIR / "de_meta4" / url.rsplit("/", 1)[-1]


def parse_meta4_tiles(text: str, prefix: str) -> dict[tuple[int, int], str]:
    """URL dlaždic z indexu Metalink: ``<prefix>_32_<E km>_<N km>_1_….tif`` → {(E, N): URL} (levý dolní roh v km)."""
    pat = re.compile(rf"<url>(https://[^<]*/{re.escape(prefix)}_\d{{2}}_(\d+)_(\d+)_1_[^<]*\.tif)</url>")
    return {(int(e), int(n)): url for url, e, n in pat.findall(text)}


def de_meta4_index(url: str, prefix: str, *, log=None) -> dict[tuple[int, int], str]:
    import time

    path = _meta4_cache(url)
    fresh = path.is_file() and time.time() - path.stat().st_mtime < DE_META4_MAX_AGE_S
    if not fresh:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}")
        try:
            import urllib.request

            from app.pipeline.fetch_openzu import USER_AGENT

            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as fh:
                size = 0
                while chunk := resp.read(1 << 20):
                    size += len(chunk)
                    if size > DE_META4_MAX_BYTES:
                        raise RuntimeError("index dlaždic je příliš velký")
                    fh.write(chunk)
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001 – starý index stačí, bez něj výšky nejdou
            tmp.unlink(missing_ok=True)
            if not path.is_file():
                raise ElevationUnavailable(f"index dlaždic {url.rsplit('/', 1)[-1]} nedostupný ({str(exc)[:80]})") from exc
            log_issue(log, f"Index dlaždic {path.name} se neobnovil, beru starší ({str(exc)[:80]})")
    return parse_meta4_tiles(path.read_text(encoding="utf-8", errors="replace"), prefix)


def de_meta4_provider(key: str) -> Provider:
    """Provider výšek země s dlaždicemi 1 km podle indexu Metalink (``tiles="meta4"`` v ``DE_WCS_STATES``)."""

    def provider(bounds, out_dir: Path, log=None) -> SourceRasters:
        import math
        from concurrent.futures import ThreadPoolExecutor

        from app.pipeline.reference_de_states import DE_WCS_STATES

        st = DE_WCS_STATES[key]
        xmin, ymin, xmax, ymax = _buffered(bounds, st.crs, DE_WCS_BUFFER_M)
        need = [(e, n) for e in range(math.floor(xmin / DE_TILE_KM), math.ceil(xmax / DE_TILE_KM))
                for n in range(math.floor(ymin / DE_TILE_KM), math.ceil(ymax / DE_TILE_KM))]

        def fetch(kind: str) -> Path | None:
            index_url, prefix = st.dtm if kind == "dtm" else st.dsm
            index = de_meta4_index(index_url, prefix, log=log)
            urls = [index[t] for t in need if t in index]
            if not urls:
                return None
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix=f"{key}-{kind}") as ex:
                tiles = list(ex.map(lambda j: _download_retry(j[1], out_dir / f"{key}_{kind}_{j[0]}.tif", log=log),
                                    enumerate(urls)))
            vrt = out_dir / f"{key}_{kind}.vrt"
            run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", st.crs, str(vrt), *map(str, tiles)], log=log)
            return vrt

        dtm = fetch("dtm")
        if dtm is None:
            raise ElevationUnavailable(f"výřez je mimo dlaždice DGM1 ({st.name})")
        try:
            dsm = fetch("dsm")
        except Exception as exc:  # noqa: BLE001 – bez povrchu mapa vznikne, jen bez výšky porostu
            log_issue(log, f"{st.name}: povrch DOM1 se nestáhl ({str(exc)[:120]}), výška porostu chybí")
            dsm = None
        return SourceRasters(dtm, dsm)

    return provider


# Bádensko-Württembersko: ZIP po 2 km (E liché, N sudé), uvnitř 4 dlaždice po 1 km. Terén je text XYZ (středy
# pixelů na .5, řádky od severu, 29 MB na km²) → GeoTIFF; nDOM je GeoTIFF výšky nad terénem. Obojí v cache
# ``de_bw/<E>_<N>`` (jako dlaždice Berlína), povrch = DGM + nDOM se skládá v jobu (``_de_nw_surface``).
BW_TILE_KM = 2
BW_MAX_ZIP_BYTES = 100_000_000  # dlaždice má 9–14 MB
BW_MAX_MEMBER_BYTES = 200_000_000  # rozbalený soubor (XYZ 1 km ~29 MB, TIF ~3 MB); větší = podezřelý ZIP


def bw_tiles(window: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """ZIP dlaždice 2 km (E liché, N sudé; levý dolní roh v km), které zasahují do okna (EPSG:25832)."""
    import math

    e0 = math.floor((window[0] / 1000 - 1) / BW_TILE_KM) * BW_TILE_KM + 1
    n0 = math.floor(window[1] / 1000 / BW_TILE_KM) * BW_TILE_KM
    return [(e, n) for e in range(e0, math.ceil(window[2] / 1000), BW_TILE_KM)
            for n in range(n0, math.ceil(window[3] / 1000), BW_TILE_KM)]


def bw_tile_dir(east_km: int, north_km: int) -> Path:
    return settings.CACHE_DIR / "de_bw" / f"{east_km}_{north_km}"


XYZ_MAX_SIDE = 2000  # dlaždice XYZ má 1 000–1 001 bodů na stranu; víc = vadný soubor (pole by nemělo strop)


def xyz_grid(text: bytes) -> tuple[np.ndarray, float, float]:
    """Mřížka 1 m z XYZ (body = středy pixelů, libovolné pořadí) → (pole, levý okraj, horní okraj); chybějící body NoData.

    Hlavička „x y z“ na prvním řádku a východní souřadnice s číslem pásu 32 (Brémy, Bremerhaven) se přijmou."""
    if text[:1].isalpha():
        text = text.split(b"\n", 1)[1] if b"\n" in text else b""
    vals = np.array(text.split(), dtype="float64").reshape(-1, 3)
    if len(vals) and vals[0, 0] >= 32_000_000:
        vals[:, 0] -= 32_000_000
    xs, ys = np.unique(vals[:, 0]), np.unique(vals[:, 1])
    if not len(vals) or not (np.allclose(np.diff(xs), 1.0) and np.allclose(np.diff(ys), 1.0)):
        raise RuntimeError("XYZ není mřížka 1 m")
    if len(xs) > XYZ_MAX_SIDE or len(ys) > XYZ_MAX_SIDE:
        raise RuntimeError(f"XYZ mřížka {len(xs)} × {len(ys)} je větší než {XYZ_MAX_SIDE} × {XYZ_MAX_SIDE}")
    grid = np.full((len(ys), len(xs)), NODATA, dtype="float32")
    col = np.rint(vals[:, 0] - xs[0]).astype(int)
    row = np.rint(ys[-1] - vals[:, 1]).astype(int)
    grid[row, col] = vals[:, 2]
    return grid, float(xs[0] - 0.5), float(ys[-1] + 0.5)


def _write_tif(dest: Path, grid: np.ndarray, left: float, top: float, crs: str) -> None:
    from osgeo import gdal, osr

    gdal.UseExceptions()
    ds = gdal.GetDriverByName("GTiff").Create(str(dest), grid.shape[1], grid.shape[0], 1, gdal.GDT_Float32,
                                              ["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform((left, 1.0, 0.0, top, 0.0, -1.0))
    srs = osr.SpatialReference()
    srs.SetFromUserInput(crs)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteArray(grid)
    ds = None


def _bw_tile(key: str, east_km: int, north_km: int, *, log=None) -> Path | None:
    """Složka s ``dtm_<E>_<N>.tif`` a ``ndom_<E>_<N>.tif`` (po 1 km) z cache, jinak stáhne a převede; None mimo zemi."""
    import tempfile
    import urllib.error
    import urllib.request
    import zipfile

    from app.download_cache import touch_last_used
    from app.pipeline.fetch_openzu import USER_AGENT
    from app.pipeline.reference_de_states import DE_WCS_STATES, bw_zip_url

    st = DE_WCS_STATES[key]
    folder = bw_tile_dir(east_km, north_km)
    done = folder / ".complete"
    if done.is_file():
        touch_last_used(folder)
        return folder
    folder.mkdir(parents=True, exist_ok=True)
    for kind, service in (("dtm", st.dtm), ("ndom", st.dsm)):
        fd, zname = tempfile.mkstemp(prefix=f".{kind}-", suffix=".zip", dir=folder)
        os.close(fd)
        zpath = Path(zname)
        try:
            req = urllib.request.Request(bw_zip_url(service, east_km, north_km), headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=300) as resp, zpath.open("wb") as fh:
                    size = 0
                    while chunk := resp.read(1 << 20):
                        size += len(chunk)
                        if size > BW_MAX_ZIP_BYTES:
                            raise RuntimeError(f"{st.name} {kind} {east_km}_{north_km}: ZIP přes limit")
                        fh.write(chunk)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    if kind == "dtm":
                        return None
                    log_issue(log, f"{st.name}: nDOM {east_km}_{north_km} chybí, výška porostu tam chybí")
                    continue
                raise
            with zipfile.ZipFile(zpath) as zf:
                for name in zf.namelist():
                    m = re.search(r"_32_(\d+)_(\d+)_1_bw[^/]*\.(xyz|tif)$", name)
                    if not m:
                        continue
                    if zf.getinfo(name).file_size > BW_MAX_MEMBER_BYTES:
                        raise RuntimeError(f"{st.name} {kind} {east_km}_{north_km}: soubor v ZIPu přes limit")
                    dest = folder / f"{kind}_{m.group(1)}_{m.group(2)}.tif"
                    tmp = dest.with_name(f".{dest.name}.{os.getpid()}")
                    if m.group(3) == "xyz":
                        grid, left, top = xyz_grid(zf.read(name))
                        _write_tif(tmp, grid, left, top, st.crs)
                    else:
                        tmp.write_bytes(zf.read(name))
                    tmp.replace(dest)
        finally:
            zpath.unlink(missing_ok=True)
    done.touch()
    touch_last_used(folder)
    if log:
        log(f"  {st.name} {east_km}_{north_km}: dlaždice převedeny do cache")
    return folder


def de_bw_provider(key: str) -> Provider:
    """Provider výšek z ZIP dlaždic LGL BW: DGM1 (XYZ → GeoTIFF) + nDOM1, povrch = DGM + nDOM po 1 km."""

    def provider(bounds, out_dir: Path, log=None) -> SourceRasters:
        from concurrent.futures import ThreadPoolExecutor

        from app.pipeline.reference_de_states import DE_WCS_STATES

        st = DE_WCS_STATES[key]
        window = _buffered(bounds, st.crs, DE_WCS_BUFFER_M)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix=key) as ex:
            folders = [f for f in ex.map(lambda t: _bw_tile(key, t[0], t[1], log=log), bw_tiles(window)) if f]
        dtms: list[Path] = []
        surfs: list[Path] = []
        for folder in folders:
            for dtm in sorted(folder.glob("dtm_*.tif")):
                e, n = (int(v) for v in dtm.stem.split("_")[1:3])
                if e * 1000 >= window[2] or (e + 1) * 1000 <= window[0] or n * 1000 >= window[3] or (n + 1) * 1000 <= window[1]:
                    continue
                dtms.append(dtm)
                ndom = folder / f"ndom_{e}_{n}.tif"
                if ndom.is_file():
                    try:
                        surfs.append(_de_nw_surface(dtm, ndom, None, out_dir / f"{key}_surface_{e}_{n}.tif", log=log,
                                                    label=st.name))
                    except RuntimeError as exc:
                        log_issue(log, f"{st.name}: povrch {e}_{n} nejde složit ({str(exc)[:80]})")
        if not dtms:
            raise ElevationUnavailable(f"výřez je mimo dlaždice DGM1 ({st.name})")

        def vrt(name: str, tiles: list[Path]) -> Path:
            path = out_dir / f"{key}_{name}.vrt"
            run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", st.crs, str(path), *map(str, tiles)], log=log)
            return path

        if len(surfs) < len(dtms):
            log_issue(log, f"{st.name}: výška porostu chybí na {len(dtms) - len(surfs)} z {len(dtms)} km²")
        return SourceRasters(vrt("dtm", dtms), vrt("dsm", surfs) if surfs else None)

    return provider


# Sasko: ZIP po 2 km (E i N sudé) s jedním GeoTIFF 1 m (DGM1, DOM1 absolutní výšky) → cache ``de_sn/<E>_<N>``.
SN_TILE_KM = 2
SN_MAX_ZIP_BYTES = 100_000_000  # dlaždice má ~14 MB
SN_MAX_MEMBER_BYTES = 400_000_000  # rozbalený GeoTIFF 2 km (float32 ~16 MB); větší = podezřelý ZIP


def sn_tiles(window: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """ZIP dlaždice 2 km GeoSN (levý dolní roh v km, obojí sudé), které zasahují do okna (EPSG:25833)."""
    import math

    e0 = math.floor(window[0] / 1000 / SN_TILE_KM) * SN_TILE_KM
    n0 = math.floor(window[1] / 1000 / SN_TILE_KM) * SN_TILE_KM
    return [(e, n) for e in range(e0, math.ceil(window[2] / 1000), SN_TILE_KM)
            for n in range(n0, math.ceil(window[3] / 1000), SN_TILE_KM)]


def sn_tile_dir(east_km: int, north_km: int) -> Path:
    return settings.CACHE_DIR / "de_sn" / f"{east_km}_{north_km}"


def _zip_tif(url: str, dest: Path, limit: int, label: str) -> bool:
    """Stáhne ZIP a vyjme z něj jediný GeoTIFF do ``dest``; False při 404 (dlaždice neexistuje)."""
    import tempfile
    import urllib.error
    import urllib.request
    import zipfile

    from app.pipeline.fetch_openzu import USER_AGENT

    fd, zname = tempfile.mkstemp(prefix=f".{dest.stem}-", suffix=".zip", dir=dest.parent)
    os.close(fd)
    zpath = Path(zname)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp, zpath.open("wb") as fh:
                size = 0
                while chunk := resp.read(1 << 20):
                    size += len(chunk)
                    if size > limit:
                        raise RuntimeError(f"{label}: ZIP přes limit")
                    fh.write(chunk)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise
        with zipfile.ZipFile(zpath) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".tif")]
            if len(names) != 1:
                raise RuntimeError(f"{label}: v ZIPu není právě jeden GeoTIFF ({len(names)})")
            if zf.getinfo(names[0]).file_size > SN_MAX_MEMBER_BYTES:
                raise RuntimeError(f"{label}: GeoTIFF v ZIPu přes limit")
            tmp = dest.with_name(f".{dest.name}.{os.getpid()}")
            with zf.open(names[0]) as src, tmp.open("wb") as out:
                while chunk := src.read(1 << 20):
                    out.write(chunk)
            tmp.replace(dest)
        return True
    finally:
        zpath.unlink(missing_ok=True)


def _sn_tile(key: str, east_km: int, north_km: int, *, log=None) -> Path | None:
    """Složka s ``dtm.tif`` (a ``dsm.tif``) dlaždice 2 km z cache, jinak stáhne; None mimo zemi."""
    from app.download_cache import touch_last_used
    from app.pipeline.reference_de_states import DE_WCS_STATES, sn_zip_url

    st = DE_WCS_STATES[key]
    folder = sn_tile_dir(east_km, north_km)
    done = folder / ".complete"
    if done.is_file():
        touch_last_used(folder)
        return folder
    folder.mkdir(parents=True, exist_ok=True)
    label = f"{st.name} {east_km}_{north_km}"
    if not _zip_tif(sn_zip_url(st.dtm, east_km, north_km), folder / "dtm.tif", SN_MAX_ZIP_BYTES, label):
        return None
    if not _zip_tif(sn_zip_url(st.dsm, east_km, north_km), folder / "dsm.tif", SN_MAX_ZIP_BYTES, label):
        log_issue(log, f"{st.name}: DOM1 {east_km}_{north_km} chybí, výška porostu tam chybí")
    done.touch()
    touch_last_used(folder)
    if log:
        log(f"  {label}: dlaždice uloženy do cache")
    return folder


def de_sn_provider(key: str) -> Provider:
    """Provider výšek z ZIP dlaždic GeoSN: DGM1 + DOM1 po 2 km (GeoTIFF s CRS, absolutní výšky)."""
    return _folder_tiles_provider(key, sn_tiles, lambda e, n, log: _sn_tile(key, e, n, log=log))


# Sársko, Hamburk a Brémy: ZIP na okres / město (0,6–1,4 GB) s GeoTIFF po 1 km. Z ZIPu se čte rozsahy HTTP jen centrální adresář (index se drží
# ``ZIP_INDEX_MAX_AGE_S``) a potřebné dlaždice; cache ``de_sl/<E>_<N>`` jako u Saska.
ZIP_INDEX_MAX_AGE_S = 30 * 86400
ZIP_RANGE_RETRIES = 3
ZIP_RANGE_MAX_BYTES = 30_000_000  # dlaždice 1 km má komprimovaně ~2 MB (XYZ Brém ~3 MB)
ZIP_RANGE_MAX_MEMBER_BYTES = 200_000_000  # rozbalená dlaždice 1 km (XYZ ~29 MB); víc = podezřelý ZIP


class _RangeRefused(RuntimeError):
    """Server neodpověděl 206 – opakování nepomůže."""


def _http_range(url: str, start: int, end: int) -> bytes:
    """Bajty ``start``–``end`` (včetně, za koncem souboru zkráceno podle Content-Range); server musí odpovědět 206.

    Spojení se občas přeruší a ``read`` pak vrátí méně bajtů bez výjimky → délka se ověří proti Content-Range
    a zkrácená odpověď se opakuje.
    """
    import time
    import urllib.request

    from app.pipeline.fetch_openzu import USER_AGENT

    for attempt in range(ZIP_RANGE_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                if resp.status != 206:
                    raise _RangeRefused(f"server nevrátil rozsah ({resp.status})")
                m = re.match(r"bytes (\d+)-(\d+)/", resp.headers.get("Content-Range") or "")
                if not m or int(m.group(1)) != start or int(m.group(2)) > end:
                    raise _RangeRefused(f"server vrátil jiný rozsah ({resp.headers.get('Content-Range')})")
                want = int(m.group(2)) - start + 1
                data = resp.read(want)
            if len(data) != want:
                raise OSError(f"rozsah zkrácen ({len(data)} z {want} B)")
            return data
        except _RangeRefused:
            raise
        except OSError:
            if attempt == ZIP_RANGE_RETRIES - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


class _RangeFile:
    """Jen čtení pro ``zipfile``: každé ``read`` je jeden dotaz Range (stačí na centrální adresář)."""

    def __init__(self, url: str, size: int):
        self.url, self.size, self.pos = url, size, 0

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        return self.pos

    def read(self, n: int = -1) -> bytes:
        end = self.size if n is None or n < 0 else min(self.size, self.pos + n)
        if end <= self.pos:
            return b""
        data = _http_range(self.url, self.pos, end - 1)
        self.pos += len(data)
        return data


def parse_zip_tile_names(entries, pattern: re.Pattern) -> dict[str, list]:
    """{„E_N“: [offset lokální hlavičky, komprimovaná velikost, metoda, CRC32, rozbalená velikost, xyz?]} pro členy
    ZIPu podle vzoru se skupinami E, N."""
    out: dict[str, list] = {}
    for info in entries:
        m = pattern.search(info.filename)
        if m:
            out.setdefault(f"{m.group(1)}_{m.group(2)}",
                           [info.header_offset, info.compress_size, info.compress_type, info.CRC, info.file_size,
                            info.filename.lower().endswith(".xyz")])
    return out


_ZIP_INDEX_LOCK = threading.Lock()


def zip_range_index(key: str, kind: str) -> dict[str, list]:
    """Index dlaždic 1 km ze všech ZIPů vrstvy: {„E_N“: [URL, offset, velikost, metoda, CRC32, rozbaleno]} (cache, 30 dní).

    Zámek: vlákna provideru by jinak při prvním běhu stahovala adresáře dvakrát.
    """
    with _ZIP_INDEX_LOCK:
        return _zip_range_index(key, kind)


def _zip_range_index(key: str, kind: str) -> dict[str, list]:
    import time
    import urllib.request
    import zipfile

    from app.pipeline.fetch_openzu import USER_AGENT
    from app.pipeline.reference_de_states import DE_WCS_STATES, zip_range_parts

    st = DE_WCS_STATES[key]
    service = st.dtm if kind == "dtm" else st.dsm
    path = settings.CACHE_DIR / f"{key}_index" / f"{kind}.json"
    if path.is_file() and time.time() - path.stat().st_mtime < ZIP_INDEX_MAX_AGE_S:
        return json.loads(path.read_text(encoding="utf-8"))
    # „dgm1_32_466_5973_1_hh_2022.tif“ (SL, HH), „dgm1_32465_5896_1_hb.xyz“ i „dgm1_32466__5924_1_hb.xyz“ (Brémy)
    pattern = re.compile(rf"{service[1]}_\d{{2}}_?(\d+)_+(\d+)_1_[^/]*\.(?:tif|xyz)$", re.IGNORECASE)
    index: dict[str, list] = {}
    for url in zip_range_parts(st, kind):
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            size = int(resp.headers["Content-Length"])
        with zipfile.ZipFile(_RangeFile(url, size)) as zf:  # type: ignore[arg-type]
            for name, entry in parse_zip_tile_names(zf.infolist(), pattern).items():
                index.setdefault(name, [url, *entry])
    if not index:
        raise RuntimeError(f"{st.name}: v ZIPech nejsou dlaždice {service[1]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(index), encoding="utf-8")
    tmp.replace(path)
    return index


def zip_member_bytes(url: str, offset: int, csize: int, method: int, crc: int | None = None,
                     usize: int | None = None) -> bytes:
    """Obsah jednoho členu ZIPu jedním dotazem Range (lokální hlavička + data, deflate nebo stored).

    Ověří délku dat, konec proudu deflate a (u indexu s CRC) CRC32 a rozbalenou velikost – vadná dlaždice
    by jinak zůstala v cache natrvalo.
    """
    import struct
    import zlib

    if csize > ZIP_RANGE_MAX_BYTES:
        raise RuntimeError("člen ZIPu přes limit")
    if usize is not None and usize > ZIP_RANGE_MAX_MEMBER_BYTES:
        raise RuntimeError("rozbalený člen ZIPu přes limit")
    raw = _http_range(url, offset, offset + 30 + 1024 + csize - 1)
    if len(raw) < 30:
        raise RuntimeError("ZIP: zkrácená lokální hlavička")
    sig, name_len, extra_len = raw[:4], *struct.unpack("<HH", raw[26:30])
    if sig != b"PK\x03\x04":
        raise RuntimeError("ZIP: chybná lokální hlavička")
    start = 30 + name_len + extra_len
    if len(raw) < start + csize:
        raw += _http_range(url, offset + len(raw), offset + start + csize - 1)
    data = raw[start:start + csize]
    if len(data) != csize:
        raise RuntimeError(f"ZIP: člen zkrácen ({len(data)} z {csize} B)")
    if method == 0:
        out = data
    elif method == 8:
        dec = zlib.decompressobj(-15)
        out = dec.decompress(data, ZIP_RANGE_MAX_MEMBER_BYTES)
        if dec.unconsumed_tail:
            raise RuntimeError("rozbalený člen ZIPu přes limit")
        if not dec.eof:
            raise RuntimeError("ZIP: neúplný proud deflate")
    else:
        raise RuntimeError(f"ZIP: nepodporovaná komprese {method}")
    if usize is not None and len(out) != usize:
        raise RuntimeError(f"ZIP: rozbaleno {len(out)} B místo {usize} B")
    if crc is not None and zlib.crc32(out) != crc:
        raise RuntimeError("ZIP: nesedí CRC32")
    return out


def zip_range_tiles(window: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    import math

    return [(e, n) for e in range(math.floor(window[0] / 1000), math.ceil(window[2] / 1000))
            for n in range(math.floor(window[1] / 1000), math.ceil(window[3] / 1000))]


def _zip_range_tile(key: str, east_km: int, north_km: int, *, log=None) -> Path | None:
    from app.download_cache import touch_last_used
    from app.pipeline.reference_de_states import DE_WCS_STATES

    st = DE_WCS_STATES[key]
    folder = settings.CACHE_DIR / key / f"{east_km}_{north_km}"
    if (folder / ".complete").is_file():
        touch_last_used(folder)
        return folder
    name = f"{east_km}_{north_km}"
    entry = zip_range_index(key, "dtm").get(name)
    if not entry:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    for kind in ("dtm", "dsm"):
        if kind == "dsm":
            try:
                entry = zip_range_index(key, "dsm").get(name)
            except Exception as exc:  # noqa: BLE001 – bez povrchu mapa vznikne
                log_issue(log, f"{st.name}: index DOM1 nejde načíst ({str(exc)[:80]})")
                entry = None
            if not entry:
                log_issue(log, f"{st.name}: DOM1 {name} chybí, výška porostu tam chybí")
                continue
        url, offset, csize, method, *rest = entry  # rest: CRC32, rozbaleno, xyz? (starší index jen část)
        crc, usize, is_xyz = (list(rest) + [None, None, False])[:3]
        data = zip_member_bytes(url, offset, csize, method, crc, usize)
        dest = folder / f"{kind}.tif"
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}")
        if is_xyz:  # XYZ (Brémy) → GeoTIFF
            grid, left, top = xyz_grid(data)
            _write_tif(tmp, grid, left, top, st.crs)
        else:
            tmp.write_bytes(data)
        tmp.replace(dest)
    (folder / ".complete").touch()
    touch_last_used(folder)
    if log:
        log(f"  {st.name} {name}: dlaždice uloženy do cache")
    return folder


def de_zip_range_provider(key: str) -> Provider:
    """Provider výšek z dlaždic 1 km čtených rozsahy z velkých ZIPů (Sársko, Hamburk, Brémy)."""
    return _folder_tiles_provider(key, zip_range_tiles, lambda e, n, log: _zip_range_tile(key, e, n, log=log))


def _folder_tiles_provider(key: str, tiles_fn, tile_fn) -> Provider:
    """Společný provider pro dlaždice v cache: složka s ``dtm.tif`` (a ``dsm.tif``) → VRT terénu a povrchu."""

    def provider(bounds, out_dir: Path, log=None) -> SourceRasters:
        from concurrent.futures import ThreadPoolExecutor

        from app.pipeline.reference_de_states import DE_WCS_STATES

        st = DE_WCS_STATES[key]
        window = _buffered(bounds, st.crs, DE_WCS_BUFFER_M)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix=key) as ex:
            folders = [f for f in ex.map(lambda t: tile_fn(t[0], t[1], log), tiles_fn(window)) if f]
        if not folders:
            raise ElevationUnavailable(f"výřez je mimo dlaždice DGM1 ({st.name})")
        dtms = [f / "dtm.tif" for f in folders]
        dsms = [f / "dsm.tif" for f in folders if (f / "dsm.tif").is_file()]

        def vrt(name: str, tiles: list[Path]) -> Path:
            path = out_dir / f"{key}_{name}.vrt"
            run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", st.crs, str(path), *map(str, tiles)], log=log)
            return path

        if len(dsms) < len(dtms):
            log_issue(log, f"{st.name}: výška porostu chybí na {len(dtms) - len(dsms)} z {len(dtms)} dlaždic")
        return SourceRasters(vrt("dtm", dtms), vrt("dsm", dsms) if dsms else None)

    return provider


def _register_de_wcs() -> None:
    from app.pipeline.reference_de_states import DE_WCS_STATES

    kinds = {"meta4": de_meta4_provider, "bw_zip": de_bw_provider, "sn_zip": de_sn_provider,
             "zip_range": de_zip_range_provider}
    for key, st in DE_WCS_STATES.items():
        PROVIDERS[key] = kinds.get(st.tiles, de_wcs_provider)(key)


_register_de_wcs()


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


# --------------------------------------------------------------------------- DE-BY: Bavorsko DGM1 + DOM20 (LDBV OpenData)

# LDBV OpenData (CC BY 4.0, bez klíče): DGM1 z ALS a DOM20 ze snímků (letní nálet), dlaždice 1 km v EPSG:25832
# (``reference_by``). DOM20 má 20 cm (~45 MB na km²): čte se jen okno výřezu přes /vsicurl a gdalwarp ho rovnou
# převzorkuje maximem na 1 m. Povrch ze snímků v létě: listnáče jsou, obilí a kukuřice vyjdou jako porost (jako nDOM NRW).
BY_BUFFER_M = 20.0
BY_TILE_M = 1000
BY_DSM_WORKERS = 4


def by_tiles(window: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """Dlaždice 1 km (E, N levého dolního rohu v km), které zasahují do okna (EPSG:25832)."""
    import math

    xmin, ymin, xmax, ymax = window
    return [(e, n) for e in range(math.floor(xmin / BY_TILE_M), math.ceil(xmax / BY_TILE_M))
            for n in range(math.floor(ymin / BY_TILE_M), math.ceil(ymax / BY_TILE_M))]


BY_RETRIES = 3  # bayernwolke při souběhu občas vrátí 403 / 5xx; GDAL 3.8 opakuje jen 429 a 5xx
_BY_TRANSIENT = re.compile(r"HTTP response code: (403|429|5\d\d)|timed out|Couldn't connect|reset", re.I)


def _by_tile_window(
    url: str, tile: tuple[int, int], window: tuple[float, float, float, float], dest: Path, *, resample: str, log=None
) -> Path | None:
    """Okno jedné dlaždice 1 km přes /vsicurl → 1 m (``resample``: ``near`` terén, ``max`` povrch 20 cm).

    None, když dlaždice není (404) nebo okno dlaždici jen lízne; přechodné chyby (403 při souběhu, 5xx) se opakují."""
    import math
    import time

    e0, n0 = tile[0] * BY_TILE_M, tile[1] * BY_TILE_M
    xmin, ymin = math.floor(max(window[0], e0)), math.floor(max(window[1], n0))
    xmax, ymax = math.ceil(min(window[2], e0 + BY_TILE_M)), math.ceil(min(window[3], n0 + BY_TILE_M))
    if xmax - xmin < 2 or ymax - ymin < 2:  # okno jen lízne dlaždici (pod 2 m) – nic nečíst
        return None
    cmd = [_gdal_tool("gdalwarp"), "-q", "-overwrite",
           "--config", "GDAL_HTTP_MAX_RETRY", "3", "--config", "GDAL_HTTP_RETRY_DELAY", "2",
           "--config", "GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR",
           "-te", str(xmin), str(ymin), str(xmax), str(ymax), "-tr", "1", "1", "-r", resample,
           "-dstnodata", str(NODATA), "-co", "COMPRESS=DEFLATE", f"/vsicurl/{url}", str(dest)]
    err = ""
    for attempt in range(BY_RETRIES + 1):
        try:
            run_cmd(cmd, log=log)
            return dest if dest.is_file() else None
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or "").strip()
            if "HTTP response code: 404" in err or "outside raster extent" in err:
                return None
            if attempt >= BY_RETRIES or not _BY_TRANSIENT.search(err):
                break
            if log:
                log(f"    {url.rsplit('/', 1)[-1]}: přechodná chyba služby, pokus {attempt + 2}/{BY_RETRIES + 1}")
            time.sleep(3.0 * (attempt + 1))
    log_issue(log, f"Bavorsko: dlaždice {url.rsplit('/', 1)[-1]} se nestáhla, část výřezu může být bez terénu nebo "
                   f"porostu ({err[-120:]})")
    return None


def de_by_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """Bavorsko: DGM1 (terén, ALS) a DOM20 (povrch ze snímků) LDBV, okna dlaždic 1 km."""
    from concurrent.futures import ThreadPoolExecutor

    window = _buffered(bounds, BY_CRS, BY_BUFFER_M)
    tiles = by_tiles(window)

    def fetch(kind: str, url_tpl: str, resample: str, workers: int) -> Path | None:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            parts = list(ex.map(
                lambda j: _by_tile_window(url_tpl.format(e=j[1][0], n=j[1][1]), j[1], window,
                                          out_dir / f"by_{kind}_{j[0]}.tif", resample=resample, log=log),
                enumerate(tiles)))
        parts = [p for p in parts if p is not None]
        if not parts:
            return None
        vrt = out_dir / f"by_{kind}.vrt"
        run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", BY_CRS, str(vrt), *map(str, parts)], log=log)
        if log:
            log(f"  by_{kind}: {len(parts)} dlaždic (okno výřezu, 1 m)")
        return vrt

    dtm = fetch("dtm", BY_DGM1_URL, "near", 2)
    if dtm is None:
        raise ElevationUnavailable("výřez je mimo pokrytí DGM1 Bavorsko")
    dsm = fetch("dsm", BY_DOM20_URL, "max", BY_DSM_WORKERS)
    if dsm is None:
        log_issue(log, "Bavorsko: povrch DOM20 pro výřez chybí, výška porostu se nespočítá")
    return SourceRasters(dtm, dsm)


PROVIDERS["de_by"] = de_by_provider


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


# MapServer (GUGiK) při výpadku své databáze odpovídá 400 s ExceptionReportem „msPostGISLayerOpen(): … Database
# connection failed“ (2026-11-20, job 6b949bdee97b; i kontrolní dotaz ve Varšavě): chyba serveru, ne dotazu.
SERVER_FAULT_MARKERS = ("database connection failed", "mspostgislayeropen", "is the database running")


def _ows_exception_text(exc) -> str:
    """Text ``ows:ExceptionText`` (nebo začátek těla) chybové odpovědi WCS; prázdný, když tělo není."""
    try:
        raw = exc.read(4096) if exc.fp is not None else b""
    except Exception:  # noqa: BLE001 – tělo chyby je jen doplněk hlášky
        raw = b""
    text = raw.decode("utf-8", "replace")
    m = re.search(r"<(?:\w+:)?ExceptionText>(.*?)</(?:\w+:)?ExceptionText>", text, re.S)
    return " ".join((m.group(1) if m else text).split())[:200]


def _download_retry(url: str, dest: Path, *, retries: int = FR_RETRIES, log=None, wait_s: float = 3.0) -> Path:
    """``_download`` s opakováním po přechodné chybě (5xx, 429, síť, výpadek databáze služby); jiná 4xx a odpověď
    bez GeoTIFFu hned dál."""
    import time
    import urllib.error

    for attempt in range(retries + 1):
        try:
            return _download(url, dest, log=log)
        except urllib.error.HTTPError as exc:
            detail = _ows_exception_text(exc) if 400 <= exc.code < 500 else ""
            server_fault = any(m in detail.lower() for m in SERVER_FAULT_MARKERS)
            if server_fault and attempt >= retries:
                raise RuntimeError(
                    f"výšková služba má výpadek na své straně (HTTP {exc.code}: {detail}); zkuste to později"
                ) from exc
            if not server_fault and (attempt >= retries or not (exc.code >= 500 or exc.code == 429)):
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


# --------------------------------------------------------------------------- NO: høydemodell DTM + DOM 1 m (Kartverket)

# Nasjonal detaljert høydemodell (Kartverket, lidar, CC BY 4.0, bez klíče): DTM (terén) a DOM (povrch) 1 m z WCS
# Geonorge v nativním EPSG:25833, výšky NN2000 (nadmořské, nic se nepřevádí). WCS 1.0.0 vrací čistý GeoTIFF Float32
# (2.0.1 subset odmítá); dlaždice 2 × 2 km ≈ 16 MB, ~8 s. DOM je většinou z letního skenování (s listím).
NO_MAX_SIDE_M = 2000.0
NO_BUFFER_M = 20.0


def no_hoyde_urls(kind: str, bounds_utm: tuple[float, float, float, float]) -> list[str]:
    """GetCoverage URL po dlaždicích nejvýš ``NO_MAX_SIDE_M`` (UTM 33N, hrany na celé metry, pixel 1 m)."""
    from app.pipeline.reference_no import NO_COVERAGES, NO_CRS, NO_WCS

    out = []
    for x0, y0, x1, y1 in tile_boxes(bounds_utm, max_side=NO_MAX_SIDE_M, buffer=NO_BUFFER_M, step=1.0):
        out.append(
            f"{NO_WCS[kind]}?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage&COVERAGE={NO_COVERAGES[kind]}&CRS={NO_CRS}"
            f"&BBOX={x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}&WIDTH={round(x1 - x0)}&HEIGHT={round(y1 - y0)}&FORMAT=GeoTIFF"
        )
    return out


def no_hoyde_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """Høydemodell: DTM (terén) a DOM (povrch) 1 m z WCS Geonorge po dlaždicích 2 km."""
    from concurrent.futures import ThreadPoolExecutor

    from app.pipeline.reference_no import NO_COVERAGES, NO_CRS

    bounds_utm = _bounds_in(bounds, CRS_WKT, NO_CRS)
    jobs = [(kind, k, url) for kind in NO_COVERAGES for k, url in enumerate(no_hoyde_urls(kind, bounds_utm))]
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="no-hoyde") as ex:
        tiles = list(ex.map(lambda j: _download_retry(j[2], out_dir / f"no_{j[0]}_{j[1]}.tif", log=log), jobs))
    out = {}
    for kind in NO_COVERAGES:
        vrt = out_dir / f"no_hoyde_{kind}.vrt"
        mine = [str(t) for (k, _i, _u), t in zip(jobs, tiles) if k == kind]
        run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", NO_CRS, str(vrt), *mine], log=log)
        out[kind] = vrt
    return SourceRasters(out["dtm"], out["dsm"])


PROVIDERS["no_hoyde"] = no_hoyde_provider


# --------------------------------------------------------------------------- SI: mračno LiDAR ARSO (GKOT) po listech 1 km

# Klasifikované mračno 2011–2015 (``reference_si``): list LAZ ~50 MB se jednou převede na terén (průměr bodů země
# v buňce 1 m) a povrch (nejvyšší bod tříd 2–6 v buňce, bez šumu 7 a nezařazených 0/1) a drží v cache jako GeoTIFF;
# LAZ se po převodu maže. Díry terénu (budovy, voda) doplní ``_fill`` jako u ostatních rastrů.
SI_TILE_KM = 1
SI_BUFFER_M = 20.0
SI_MAX_LAZ_BYTES = 300_000_000  # list má 30–80 MB; víc je chyba služby
SI_GROUND_CLASS = 2
SI_SURFACE_CLASSES = (2, 3, 4, 5, 6)
SI_BLOCKS_FILE = "blocks.json"
_SI_BLOCKS_LOCK = threading.Lock()


def si_tiles(window: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """Listy 1 km (E, N levého dolního rohu v km, D96/TM), které zasahují do okna."""
    import math

    xmin, ymin, xmax, ymax = window
    return [(e, n) for e in range(math.floor(xmin / 1000), math.ceil(xmax / 1000))
            for n in range(math.floor(ymin / 1000), math.ceil(ymax / 1000))]


def si_cache_dir() -> Path:
    """Cache listů (``<E>_<N>/dtm.tif``, ``dsm.tif``) a mapa bloků; úklid ``download_cache.prune_lidar_cache``."""
    return settings.CACHE_DIR / "si_arso"


def _http_status(url: str, *, method: str = "HEAD") -> int | None:
    """Kód odpovědi; None = síťová chyba (nelze rozlišit „list neexistuje“)."""
    import urllib.error
    import urllib.request

    from app.pipeline.fetch_openzu import USER_AGENT

    try:
        req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return None


def si_tile_block(east_km: int, north_km: int, *, status=_http_status) -> str | None:
    """Blok listu z cache, jinak HEAD po blocích (nejdřív bloky sousedních listů); None = list neexistuje."""
    from concurrent.futures import ThreadPoolExecutor

    from app.pipeline.reference_si import SI_BLOCKS, gkot_url

    key = f"{east_km}_{north_km}"
    index_path = si_cache_dir() / SI_BLOCKS_FILE
    with _SI_BLOCKS_LOCK:
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            index = {}
    if key in index:
        return index[key] or None
    near = [index.get(f"{east_km + de}_{north_km + dn}") for de in (-1, 0, 1) for dn in (-1, 0, 1)]
    order = list(dict.fromkeys([b for b in near if b] + list(SI_BLOCKS)))
    found: str | None = None
    unknown = False
    first = order[: len([b for b in near if b])]
    for batch in (first, [b for b in order if b not in first]):
        if not batch:
            continue
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="si-block") as ex:
            codes = list(ex.map(lambda b: status(gkot_url(b, east_km, north_km)), batch))
        unknown = unknown or any(c is None or c >= 500 for c in codes)
        hits = [b for b, c in zip(batch, codes) if c is not None and 200 <= c < 300]
        if hits:
            found = hits[0]
            break
    if found is None and unknown:
        raise RuntimeError(f"Slovinsko: blok listu {key} nejde zjistit (server ARSO neodpovídá)")
    with _SI_BLOCKS_LOCK:
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            index = {}
        index[key] = found or ""
        index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = index_path.with_name(f".{index_path.name}.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(index, sort_keys=True), encoding="utf-8")
        tmp.replace(index_path)
    return found


def si_laz_grids(
    laz: Path,
    east_km: int,
    north_km: int,
    *,
    side_m: int = SI_TILE_KM * 1000,
    left: float | None = None,
    bottom: float | None = None,
    ground: tuple[int, ...] = (SI_GROUND_CLASS,),
    surface: tuple[int, ...] = SI_SURFACE_CLASSES,
) -> tuple[np.ndarray, np.ndarray]:
    """Terén (průměr bodů země) a povrch (maximum tříd 2–6) v buňkách 1 m listu; prázdné buňky NODATA.

    Výchozí hodnoty = Slovinsko (list 1 km s levým dolním rohem ``east_km``/``north_km``); jiné zdroje předají
    roh listu (``left``, ``bottom``), stranu a třídy. Body mimo list (přesah sousedních listů) se nepočítají."""
    import laspy

    side = side_m
    left = east_km * 1000.0 if left is None else left
    bottom = north_km * 1000.0 if bottom is None else bottom
    total = np.zeros(side * side, dtype="float64")
    count = np.zeros(side * side, dtype="int64")
    top = np.full(side * side, -np.inf, dtype="float64")
    with laspy.open(str(laz)) as reader:
        for pts in reader.chunk_iterator(2_000_000):
            x, y, z = np.asarray(pts.x), np.asarray(pts.y), np.asarray(pts.z)
            cls = np.asarray(pts.classification)
            col = np.floor(x - left).astype(np.int64)
            row = np.floor(bottom + side - y).astype(np.int64)
            inside = (col >= 0) & (col < side) & (row >= 0) & (row < side)
            idx = row * side + col
            g = inside & np.isin(cls, ground)
            total += np.bincount(idx[g], weights=z[g], minlength=side * side)
            count += np.bincount(idx[g], minlength=side * side)
            sfc = inside & np.isin(cls, surface)
            np.maximum.at(top, idx[sfc], z[sfc])
    dtm = np.where(count > 0, total / np.maximum(count, 1), NODATA).astype("float32").reshape(side, side)
    dsm = np.where(np.isfinite(top), top, NODATA).astype("float32").reshape(side, side)
    return dtm, dsm


def _si_tile(east_km: int, north_km: int, *, log=None) -> Path | None:
    """Složka listu s ``dtm.tif`` a ``dsm.tif`` z cache, jinak stáhne LAZ a převede; None = list neexistuje."""
    import tempfile
    import urllib.request

    from app.download_cache import touch_last_used
    from app.pipeline.fetch_openzu import USER_AGENT
    from app.pipeline.reference_si import SI_CRS, gkot_url

    folder = si_cache_dir() / f"{east_km}_{north_km}"
    if (folder / ".complete").is_file():
        touch_last_used(folder)
        return folder
    block = si_tile_block(east_km, north_km)
    if block is None:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    fd, lname = tempfile.mkstemp(prefix=".gkot-", suffix=".laz", dir=folder)
    os.close(fd)
    lpath = Path(lname)
    try:
        req = urllib.request.Request(gkot_url(block, east_km, north_km), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=600) as resp, lpath.open("wb") as fh:
            size = 0
            while chunk := resp.read(1 << 20):
                size += len(chunk)
                if size > SI_MAX_LAZ_BYTES:
                    raise RuntimeError(f"Slovinsko {east_km}_{north_km}: LAZ větší než {SI_MAX_LAZ_BYTES // 1_000_000} MB")
                fh.write(chunk)
        dtm, dsm = si_laz_grids(lpath, east_km, north_km)
        top = (north_km + SI_TILE_KM) * 1000.0
        for name, grid in (("dtm", dtm), ("dsm", dsm)):
            tmp = folder / f".{name}.{os.getpid()}.{threading.get_ident()}.tif"
            _write_tif(tmp, grid, east_km * 1000.0, top, SI_CRS)
            tmp.replace(folder / f"{name}.tif")
        (folder / ".complete").touch()
    finally:
        lpath.unlink(missing_ok=True)
    touch_last_used(folder)
    if log:
        log(f"  Slovinsko LiDAR {east_km}_{north_km} ({block}): {size / 1e6:.0f} MB převedeno do cache")
    return folder


def si_arso_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """Slovinsko: terén a povrch 1 m z klasifikovaného mračna ARSO (listy 1 km, cache)."""
    from concurrent.futures import ThreadPoolExecutor

    from app.pipeline.reference_si import SI_CRS

    window = _buffered(bounds, SI_CRS, SI_BUFFER_M)
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="si-gkot") as ex:
        folders = [f for f in ex.map(lambda t: _si_tile(t[0], t[1], log=log), si_tiles(window)) if f]
    if not folders:
        raise ElevationUnavailable("výřez je mimo pokrytí LiDAR ARSO (Slovinsko)")
    out: dict[str, Path] = {}
    for kind in ("dtm", "dsm"):
        vrt = out_dir / f"si_arso_{kind}.vrt"
        run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", SI_CRS, str(vrt), *(str(f / f"{kind}.tif") for f in folders)],
                log=log)
        out[kind] = vrt
    return SourceRasters(out["dtm"], out["dsm"])


PROVIDERS["si_arso"] = si_arso_provider


# --------------------------------------------------------------------------- ES: mračno LiDAR regionů (Navarra)

# Stejně jako Slovinsko: list LAZ se jednou převede na terén (průměr bodů země v buňce 1 m) a povrch (nejvyšší bod
# tříd terénu, vegetace, budov, vody a mostů) a drží v cache jako GeoTIFF; LAZ se po převodu maže. Seznam listů
# regionu z výpisu adresáře (cache 30 dní). Elipsoidické výšky (Navarra) srovná geoid EGM08-REDNAP z PROJ-data.
ES_BUFFER_M = 20.0
ES_MAX_LAZ_BYTES = 400_000_000  # list Navarry má 60–90 MB; víc je chyba služby
ES_INDEX_MAX_AGE_DAYS = 30
_ES_INDEX_LOCK = threading.Lock()


def es_cache_dir(key: str) -> Path:
    """Cache listů regionu (``<E>_<N>/dtm.tif``, ``dsm.tif``) a seznam listů; úklid ``download_cache.prune_lidar_cache``."""
    return settings.CACHE_DIR / key


def es_index(key: str, *, fetch=None, max_age_days: float = ES_INDEX_MAX_AGE_DAYS) -> set[tuple[int, int]]:
    """Listy regionu jako (E, N) v km podle jména (N podle ``top_corner``) z cache, jinak z výpisu adresáře."""
    import time
    import urllib.request

    from app.pipeline.fetch_openzu import USER_AGENT
    from app.pipeline.reference_es import ES_LAZ_REGIONS

    region = ES_LAZ_REGIONS[key]
    path = es_cache_dir(key) / "index.json"
    with _ES_INDEX_LOCK:
        try:
            if time.time() - path.stat().st_mtime < max_age_days * 86400:
                return {tuple(t) for t in json.loads(path.read_text(encoding="utf-8"))}
        except (OSError, ValueError):
            pass

    def default_fetch(url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read(50_000_000).decode("utf-8", "replace")

    try:
        body = (fetch or default_fetch)(region.listing)
        tiles = {(int(e), int(n)) for e, n in re.findall(region.name_re, body)}
        if not tiles:
            raise RuntimeError(f"{region.label}: výpis listů je prázdný")
    except Exception as exc:  # výpadek výpisu: prošlý seznam z cache je lepší než nic
        try:
            return {tuple(t) for t in json.loads(path.read_text(encoding="utf-8"))}
        except (OSError, ValueError):
            raise ElevationUnavailable(f"{region.label}: seznam listů nejde načíst ({exc})") from exc
    with _ES_INDEX_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(sorted(tiles)), encoding="utf-8")
        tmp.replace(path)
    return tiles


def es_tiles(key: str, window: tuple[float, float, float, float], index: set[tuple[int, int]]) -> list[tuple[int, int]]:
    """Listy regionu (jména E, N v km), které zasahují do okna a existují."""
    from app.pipeline.reference_es import ES_LAZ_REGIONS

    region = ES_LAZ_REGIONS[key]
    side = region.tile_m
    xmin, ymin, xmax, ymax = window
    out = []
    for e in range(math.floor(xmin / side), math.ceil(xmax / side)):
        for b in range(math.floor(ymin / side), math.ceil(ymax / side)):
            name = (e * side // 1000, (b + 1) * side // 1000 if region.top_corner else b * side // 1000)
            if name in index:
                out.append(name)
    return out


def _es_tile(key: str, e_km: int, n_km: int, *, log=None) -> Path:
    """Složka listu s ``dtm.tif`` a ``dsm.tif`` z cache, jinak stáhne LAZ a převede."""
    import tempfile
    import urllib.request

    from app.download_cache import touch_last_used
    from app.pipeline.fetch_openzu import USER_AGENT
    from app.pipeline.reference_es import ES_CRS, ES_LAZ_REGIONS

    region = ES_LAZ_REGIONS[key]
    folder = es_cache_dir(key) / f"{e_km}_{n_km}"
    if (folder / ".complete").is_file():
        touch_last_used(folder)
        return folder
    folder.mkdir(parents=True, exist_ok=True)
    fd, lname = tempfile.mkstemp(prefix=".es-", suffix=".laz", dir=folder)
    os.close(fd)
    lpath = Path(lname)
    side = region.tile_m
    left = e_km * 1000.0
    top = n_km * 1000.0 if region.top_corner else n_km * 1000.0 + side
    try:
        req = urllib.request.Request(region.url.format(e=e_km, n=n_km), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=900) as resp, lpath.open("wb") as fh:
            size = 0
            while chunk := resp.read(1 << 20):
                size += len(chunk)
                if size > ES_MAX_LAZ_BYTES:
                    raise RuntimeError(f"{region.label} {e_km}-{n_km}: LAZ větší než {ES_MAX_LAZ_BYTES // 1_000_000} MB")
                fh.write(chunk)
        dtm, dsm = si_laz_grids(lpath, e_km, n_km, side_m=side, left=left, bottom=top - side,
                                ground=region.ground, surface=region.surface)
        for name, grid in (("dtm", dtm), ("dsm", dsm)):
            tmp = folder / f".{name}.{os.getpid()}.{threading.get_ident()}.tif"
            _write_tif(tmp, grid, left, top, ES_CRS)
            tmp.replace(folder / f"{name}.tif")
        (folder / ".complete").touch()
    finally:
        lpath.unlink(missing_ok=True)
    touch_last_used(folder)
    if log:
        log(f"  {region.label} {e_km}-{n_km}: {size / 1e6:.0f} MB převedeno do cache")
    return folder


def _es_geoid_grid(log=None) -> Path | None:
    from app.pipeline.reference_es import ES_GEOID_URL

    dest = settings.CACHE_DIR / "geoid" / "es_ign_egm08-rednap.tif"
    if dest.is_file() and dest.stat().st_size > 100_000:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _download(ES_GEOID_URL, dest, log=log)
    except Exception as exc:  # bez mřížky zůstanou výšky elipsoidické (vrstevnice popsané o ~50 m výš)
        if log:
            log(f"⚠ Geoid EGM08-REDNAP se nestáhl ({exc}): výšky zůstanou elipsoidické (o ~50 m vyšší).")
        return None


def es_laz_provider(key: str) -> Callable[..., SourceRasters]:
    def provider(bounds, out_dir: Path, log=None) -> SourceRasters:
        """Španělský region: terén a povrch 1 m z klasifikovaného mračna (listy, cache)."""
        from concurrent.futures import ThreadPoolExecutor

        from app.pipeline.reference_es import ES_CRS, ES_LAZ_REGIONS

        region = ES_LAZ_REGIONS[key]
        window = _buffered(bounds, ES_CRS, ES_BUFFER_M)
        tiles = es_tiles(key, window, es_index(key))
        if not tiles:
            raise ElevationUnavailable(f"výřez je mimo pokrytí mračna {region.label}")
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix=f"{key}-laz") as ex:
            folders = list(ex.map(lambda t: _es_tile(key, t[0], t[1], log=log), tiles))
        out: dict[str, Path] = {}
        for kind in ("dtm", "dsm"):
            vrt = out_dir / f"{key}_{kind}.vrt"
            run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", ES_CRS, str(vrt), *(str(f / f"{kind}.tif") for f in folders)],
                    log=log)
            out[kind] = vrt
        offset = None
        grid = _es_geoid_grid(log=log) if region.ellipsoidal else None
        if grid is not None:

            def offset(lon: float, lat: float) -> float:
                return geoid_offsets(grid, [(lon, lat)])[0]

        return SourceRasters(out["dtm"], out["dsm"], offset)

    provider.__name__ = f"{key}_provider"
    return provider


PROVIDERS["es_nav"] = es_laz_provider("es_nav")


# --------------------------------------------------------------------------- US: USGS 3DEP (mračno EPT na AWS)

# Terén (průměr bodů země) a povrch (maximum bodů mimo šum) 1 m přímo z mračna 3DEP v souřadnicích jobu
# (NAD83 / UTM), stejně jako u Slovinska. Projekty vybírá index WESM (jen QL 0 – QL 2, od nejnovějšího), mračno
# se čte z EPT po uzlech oktalového stromu, které zasahují do výřezu (``reference_us``). Výšky NAVD88 v metrech.
US_BUFFER_M = 20.0
# Nejjemnější čtená úroveň EPT: rozestup buněk uzlu na zemi aspoň tolik metrů. Hustší úrovně QL 1 (> 8 bodů/m²)
# se nečtou – pro mřížku 1 m nic nepřidají a objem by rostl (QL 2 vyjde celé, ~90 MB/km² LAZ).
US_MIN_SPACING_M = 0.3
US_MAX_POINTS = 600_000_000  # strop bodů na výřez (~5 GB LAZ); výřez nad ním skončí hláškou
US_MAX_WORKUNITS = 3  # nejvýš tolik projektů na výřez (nejnovější, každý musí přidat pokrytí)
US_GROUND_CLASS = 2
US_NOISE_CLASSES = (7, 18)
US_FETCH_WORKERS = 8
US_OLD_YEARS = 10  # mračno starší než tolik let: upozornění v logu (porost se mohl změnit)
# Díry v terénu do tolika metrů od bodu země (pod hustým porostem QL 1 chybí země v ~28 % buněk 1 m) se doplní
# nejbližší zemí; jinak by z nich byl CHM 0 (louka v lese). Větší díry (budovy, voda) zůstanou bez dat.
US_DTM_GAP_M = 4.0


def fill_small_gaps(grid: np.ndarray, max_px: float) -> np.ndarray:
    """Buňky bez dat do ``max_px`` od platné buňky dostanou hodnotu nejbližší platné buňky."""
    from scipy import ndimage

    missing = grid <= NODATA / 2
    if not missing.any() or missing.all():
        return grid
    dist, (ri, ci) = ndimage.distance_transform_edt(missing, return_indices=True)
    near = missing & (dist <= max_px)
    out = grid.copy()
    out[near] = grid[ri[near], ci[near]]
    return out


def ept_nodes(base: str, ept: dict, window: tuple[float, float, float, float], lat: float, *,
              fetch) -> list[tuple[str, int]]:
    """Uzly EPT (klíč D-X-Y-Z, počet bodů), jejichž krychle zasahuje do okna (EPSG:3857), do úrovně
    s rozestupem ≥ ``US_MIN_SPACING_M`` na zemi (měřítko Mercatoru cos φ)."""
    b = ept["bounds"]
    size0 = float(b[3]) - float(b[0])
    span = float(ept.get("span") or 128)
    scale = math.cos(math.radians(lat))
    x0, y0, x1, y1 = window
    out: list[tuple[str, int]] = []

    def walk(hier: dict, key: str) -> None:
        d, x, y, _z = (int(v) for v in key.split("-"))
        size = size0 / 2 ** d
        nx, ny = float(b[0]) + x * size, float(b[1]) + y * size
        if nx > x1 or nx + size < x0 or ny > y1 or ny + size < y0:
            return
        count = hier.get(key)
        if count is None:
            return
        if size / span * scale < US_MIN_SPACING_M:
            return
        if count == -1:
            hier = json.loads(fetch(f"{base}/ept-hierarchy/{key}.json"))
            count = hier.get(key, 0)
        if count > 0:
            out.append((key, int(count)))
        _d, _x, _y, _zz = d + 1, 2 * x, 2 * y, 2 * _z
        for dx in (0, 1):
            for dy in (0, 1):
                for dz in (0, 1):
                    walk(hier, f"{_d}-{_x + dx}-{_y + dy}-{_zz + dz}")

    walk(json.loads(fetch(f"{base}/ept-hierarchy/0-0-0-0.json")), "0-0-0-0")
    return out


class _UsGrid:
    """Součet a počet bodů země a maximum povrchu v buňkách 1 m okna jobu."""

    def __init__(self, left: float, top: float, width: int, height: int):
        self.left, self.top, self.width, self.height = left, top, width, height
        n = width * height
        self.gsum = np.zeros(n, dtype="float64")
        self.gcount = np.zeros(n, dtype="uint32")
        self.top_z = np.full(n, -np.inf, dtype="float32")

    def add(self, x: np.ndarray, y: np.ndarray, z: np.ndarray, cls: np.ndarray, withheld: np.ndarray) -> None:
        col = np.floor(x - self.left).astype(np.int64)
        row = np.floor(self.top - y).astype(np.int64)
        inside = (col >= 0) & (col < self.width) & (row >= 0) & (row < self.height)
        idx = row * self.width + col
        g = inside & (cls == US_GROUND_CLASS) & ~withheld
        if g.any():
            u, inv = np.unique(idx[g], return_inverse=True)
            self.gsum[u] += np.bincount(inv, weights=z[g])
            self.gcount[u] += np.bincount(inv).astype("uint32")
        s = inside & ~np.isin(cls, US_NOISE_CLASSES) & ~withheld
        if s.any():
            si, sz = idx[s], z[s].astype("float32")
            order = np.argsort(si, kind="stable")
            si, sz = si[order], sz[order]
            starts = np.flatnonzero(np.r_[True, si[1:] != si[:-1]])
            cells = si[starts]
            self.top_z[cells] = np.maximum(self.top_z[cells], np.maximum.reduceat(sz, starts))

    def grids(self) -> tuple[np.ndarray, np.ndarray]:
        shape = (self.height, self.width)
        dtm = np.where(self.gcount > 0, self.gsum / np.maximum(self.gcount, 1), NODATA).astype("float32")
        dsm = np.where(np.isfinite(self.top_z), self.top_z, NODATA).astype("float32")
        return dtm.reshape(shape), dsm.reshape(shape)


def _las_arrays(raw: bytes):
    import io

    import laspy

    las = laspy.read(io.BytesIO(raw))
    try:
        withheld = np.asarray(las.withheld, dtype=bool)
    except (AttributeError, ValueError):
        withheld = np.zeros(len(las.points), dtype=bool)
    return (np.asarray(las.x), np.asarray(las.y), np.asarray(las.z), np.asarray(las.classification), withheld)


def us_ept_grids(base: str, window_job: tuple[float, float, float, float], *, fetch=None, log=None,
                 label: str = "") -> tuple[np.ndarray, np.ndarray, float, float, int, int]:
    """(dtm, dsm, left, top, bodů, bajtů) mřížky 1 m okna v souřadnicích jobu z EPT ``base``."""
    from concurrent.futures import ThreadPoolExecutor

    from pyproj import Transformer

    from app.pipeline.crs_5514 import CRS_PROJ4
    from app.pipeline.reference_us import _get

    fetch = fetch or _get
    xmin, ymin, xmax, ymax = window_job
    left, top = math.floor(xmin), math.ceil(ymax)
    width, height = int(math.ceil(xmax) - left), int(top - math.floor(ymin))
    to_web = Transformer.from_crs(CRS_PROJ4, "EPSG:3857", always_xy=True)
    to_job = Transformer.from_crs("EPSG:3857", CRS_PROJ4, always_xy=True)
    to_ll = Transformer.from_crs(CRS_PROJ4, "EPSG:4326", always_xy=True)
    edge = np.linspace(0.0, 1.0, 9)
    px = np.r_[xmin + edge * (xmax - xmin), np.full(9, xmax), xmax - edge * (xmax - xmin), np.full(9, xmin)]
    py = np.r_[np.full(9, ymin), ymin + edge * (ymax - ymin), np.full(9, ymax), ymax - edge * (ymax - ymin)]
    wx, wy = to_web.transform(px, py)
    window = (float(np.min(wx)), float(np.min(wy)), float(np.max(wx)), float(np.max(wy)))
    lat = to_ll.transform((xmin + xmax) / 2.0, (ymin + ymax) / 2.0)[1]
    ept = json.loads(fetch(f"{base}/ept.json"))
    nodes = ept_nodes(base, ept, window, lat, fetch=fetch)
    total = sum(c for _k, c in nodes)
    if total > US_MAX_POINTS:
        raise RuntimeError(f"USA: výřez má v mračnu {total / 1e6:.0f} mil. bodů, strop je {US_MAX_POINTS / 1e6:.0f} mil.")
    grid = _UsGrid(float(left), float(top), width, height)
    size = 0
    points = 0

    def load(key: str):
        raw = fetch(f"{base}/ept-data/{key}.laz")
        return len(raw), _las_arrays(raw)

    with ThreadPoolExecutor(max_workers=US_FETCH_WORKERS, thread_name_prefix="us-ept") as ex:
        for nbytes, (x, y, z, cls, withheld) in ex.map(load, [k for k, _c in nodes]):
            size += nbytes
            m = (x >= window[0]) & (x <= window[2]) & (y >= window[1]) & (y <= window[3])
            if not m.any():
                continue
            jx, jy = to_job.transform(x[m], y[m])
            grid.add(np.asarray(jx), np.asarray(jy), z[m], cls[m], withheld[m])
            points += int(m.sum())
    if log:
        log(f"  USA 3DEP {label}: {len(nodes)} uzlů EPT, {points / 1e6:.1f} mil. bodů ve výřezu, {size / 1e6:.0f} MB")
    dtm, dsm = grid.grids()
    return dtm, dsm, float(left), float(top), points, size


def us_3dep_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """USA: terén a povrch 1 m z mračna USGS 3DEP (EPT na AWS), projekty QL 0 – QL 2 podle WESM."""
    import time as _time

    from pyproj import Transformer

    from app.pipeline.crs_5514 import CRS_PROJ4
    from app.pipeline.gdal_cli_raster import write_float32_geotiff
    from app.pipeline.reference_us import candidates, ept_url

    xmin, ymin, xmax, ymax = (float(v) for v in bounds)
    window = (xmin - US_BUFFER_M, ymin - US_BUFFER_M, xmax + US_BUFFER_M, ymax + US_BUFFER_M)
    to_ll = Transformer.from_crs(CRS_PROJ4, "EPSG:4326", always_xy=True)
    lons, lats = to_ll.transform([window[0], window[2], window[0], window[2]], [window[1], window[1], window[3], window[3]])
    bbox_wgs84 = (min(lons), min(lats), max(lons), max(lats))
    from shapely.geometry import box

    area = box(*bbox_wgs84)
    chosen = []
    covered = None
    for name, wu in candidates(bbox_wgs84):
        part = wu.geometry.intersection(area)
        if part.is_empty:
            continue
        gain = part.area if covered is None else part.difference(covered).area
        if gain < 0.01 * area.area:
            continue
        chosen.append((name, wu))
        covered = part if covered is None else covered.union(part)
        if covered.area >= 0.995 * area.area or len(chosen) >= US_MAX_WORKUNITS:
            break
    if not chosen:
        raise ElevationUnavailable("výřez je mimo pokrytí kvalitním mračnem LiDAR USGS 3DEP (QL 0 – QL 2)")
    dtm = dsm = None
    left = top = 0.0
    for name, wu in chosen:
        g_dtm, g_dsm, left, top, _pts, _size = us_ept_grids(ept_url(name), window, log=log,
                                                            label=f"{wu.name} ({wu.ql}, {wu.year})" if wu.year else f"{wu.name} ({wu.ql})")
        if dtm is None:
            dtm, dsm = g_dtm, g_dsm
        else:  # starší projekt jen doplní buňky bez dat
            dtm = np.where(dtm > NODATA / 2, dtm, g_dtm)
            dsm = np.where(dsm > NODATA / 2, dsm, g_dsm)
    dtm = fill_small_gaps(dtm, US_DTM_GAP_M)
    years = sorted({wu.year for _n, wu in chosen if wu.year})
    if years and years[0] <= _time.gmtime().tm_year - US_OLD_YEARS:
        # Jen informace, ne chyba jobu: log_issue by job označil „hotovo s chybami“ s hláškou o chybějících výškách.
        if log:
            log(f"  Upozornění: mračno USGS 3DEP z roku {years[0]} – porost a stavby se od té doby mohly změnit")
    out: dict[str, Path] = {}
    gt = (left, 1.0, 0.0, top, 0.0, -1.0)
    for kind, grid in (("dtm", dtm), ("dsm", dsm)):
        out[kind] = write_float32_geotiff(out_dir / f"us_3dep_{kind}.tif", grid, gt, srs_wkt=CRS_WKT, nodata=NODATA)
    return SourceRasters(out["dtm"], out["dsm"])


def us_3dep_coverage(bbox_wgs84: tuple[float, float, float, float]) -> float:
    """Podíl výřezu s mračnem 3DEP QL 0 – QL 2, které je v bucketu EPT."""
    from app.pipeline.reference_us import candidates, coverage_share

    return coverage_share(bbox_wgs84, candidates(bbox_wgs84))


def us_3dep_pending(bbox_wgs84: tuple[float, float, float, float]) -> list[str]:
    """Projekty QL 0 – QL 2 ve výřezu, jejichž mračno USGS ještě nezveřejnil (z poslední kontroly pokrytí)."""
    from app.pipeline.reference_us import pending_workunits

    return pending_workunits(bbox_wgs84)


PROVIDERS["us_3dep"] = us_3dep_provider


# --------------------------------------------------------------------------- GB-SCT: Scottish Remote Sensing Portal

# Skotská vláda (Open Government Licence v3, bez klíče): DTM a DSM z LiDARu po fázích (Phase 1–6 2011–2022, Orkney
# 2023, National LiDAR Programme 2025, pobřeží 2026), GeoTIFF Float32 dlaždicové s přehledy v S3 bucketu – čte se
# jen okno výřezu přes /vsicurl. Výšky ODN, EPSG:27700 (nic se nepřevádí). Dlaždice mají jména podle mřížky OS
# (10 km ``NT27``, 5 km ``NT27NE``, 1 km ``NT2173``) a fáze se překrývají: seznam dlaždic se jednou za
# ``SCT_INDEX_MAX_AGE_DAYS`` načte z výpisu bucketu do cache a novější fáze má přednost. Sady HES (Historic
# Environment Scotland) jsou zčásti pod nekomerční licencí NCGL, proto se neberou.
SCT_BUCKET = "https://srsp-open-data.s3.eu-west-2.amazonaws.com"
SCT_CRS = "EPSG:27700"
SCT_BUFFER_M = 20.0
SCT_INDEX_MAX_AGE_DAYS = 30
# (složka v bucketu, popis) od nejstarší po nejnovější – ve VRT vyhrává pozdější.
SCT_DATASETS: tuple[tuple[str, str], ...] = (
    ("phase-1", "Phase 1 2011–2012"),
    ("phase-2", "Phase 2 2012–2014"),
    ("phase-3", "Phase 3 2012–2014"),
    ("phase-4", "Phase 4 2017–2019"),
    ("phase-5", "Phase 5 2020–2021"),
    ("phase-6", "Phase 6 2021–2022"),
    ("orkney-islands-council-23", "Orkney 2023"),
    ("national-lidar-programme", "National LiDAR Programme 2025"),
    ("coastal/coastal-2025-2026", "Coastal 2025–2026"),
)
_SCT_INDEX_LOCK = threading.Lock()
_SCT_REF = re.compile(r"^([A-HJ-Z]{2})(\d*)(NE|NW|SE|SW)?$")


def _os_square_origins() -> dict[str, tuple[int, int]]:
    out = {}
    for e100 in range(7):
        for n100 in range(13):
            letters = os_grid_tile(e100 * 100_000, n100 * 100_000)[:2]
            out[letters] = (e100 * 100_000, n100 * 100_000)
    return out


_OS_SQUARES = _os_square_origins()


def os_ref_bounds(ref: str) -> tuple[float, float, float, float] | None:
    """Obdélník dlaždice podle jména v mřížce OS (``NT27`` 10 km, ``NT27NE`` 5 km, ``NT2173`` 1 km); None = nečitelné."""
    m = _SCT_REF.match(ref.upper())
    if not m or m.group(1) not in _OS_SQUARES or len(m.group(2)) % 2 or not m.group(2):
        return None
    e0, n0 = _OS_SQUARES[m.group(1)]
    digits = m.group(2)
    k = len(digits) // 2
    size = 100_000 / 10**k
    e = e0 + int(digits[:k]) * size
    n = n0 + int(digits[k:]) * size
    if m.group(3):
        size /= 2
        e += size if m.group(3)[1] == "E" else 0
        n += size if m.group(3)[0] == "N" else 0
    return e, n, e + size, n + size


def _s3_keys(prefix: str, *, fetch=None) -> list[str]:
    """Všechny klíče bucketu pod ``prefix`` (ListObjectsV2 po 1000)."""
    import urllib.parse
    import urllib.request

    from app.pipeline.fetch_openzu import USER_AGENT

    def default_fetch(url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read(20_000_000).decode("utf-8")

    fetch = fetch or default_fetch
    keys: list[str] = []
    token = None
    while True:
        q = {"list-type": "2", "prefix": prefix}
        if token:
            q["continuation-token"] = token
        body = fetch(f"{SCT_BUCKET}/?{urllib.parse.urlencode(q)}")
        keys += re.findall(r"<Key>([^<]+)</Key>", body)
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", body)
        if not m:
            return keys
        token = m.group(1)


def sct_index(*, fetch=None, max_age_days: float = SCT_INDEX_MAX_AGE_DAYS) -> dict[str, dict[str, list[str]]]:
    """Seznam dlaždic po sadách: ``{sada: {"dtm": [klíč…], "dsm": [klíč…]}}`` z cache, jinak z výpisu bucketu."""
    import time

    path = settings.CACHE_DIR / "gb_sct" / "index.json"
    with _SCT_INDEX_LOCK:
        try:
            if time.time() - path.stat().st_mtime < max_age_days * 86400:
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        index: dict[str, dict[str, list[str]]] = {}
        for folder, _label in SCT_DATASETS:
            index[folder] = {}
            for kind in ("dtm", "dsm"):
                keys = _s3_keys(f"lidar/{folder}/{kind}/", fetch=fetch)
                index[folder][kind] = sorted(k for k in keys if k.lower().endswith(".tif") and "/gridded/" in k)
        if not any(v["dtm"] for v in index.values()):
            raise RuntimeError("Skotsko: výpis dlaždic LiDARu je prázdný")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(index), encoding="utf-8")
        tmp.replace(path)
        return index


def sct_tiles(kind: str, window, index) -> list[tuple[str, str, tuple[float, float, float, float]]]:
    """Dlaždice ``kind`` zasahující do okna: (sada, klíč, obdélník) od nejstarší sady po nejnovější."""
    xmin, ymin, xmax, ymax = window
    out = []
    for folder, _label in SCT_DATASETS:
        for key in index.get(folder, {}).get(kind, []):
            box = os_ref_bounds(key.rsplit("/", 1)[-1].split("_", 1)[0])
            if box and box[0] < xmax and box[2] > xmin and box[1] < ymax and box[3] > ymin:
                out.append((folder, key, box))
    return out


SCT_COVERAGE_CELL_M = 20.0  # hrubá mřížka pro podíl pokrytí (čte se přehled dlaždice, ne plné rozlišení)


def sct_valid_mask(sources: list[str], window, cell_m: float = SCT_COVERAGE_CELL_M) -> np.ndarray:
    """Maska buněk ``cell_m`` v okně, kde má aspoň jeden zdroj (cesta nebo /vsicurl adresa) platnou výšku.

    Dlaždice fází nejsou plné (Phase 1 ve 10 km čtverci třeba jen ze čtvrtiny), obdélník dlaždice pokrytí přestřelí.
    gdalwarp do hrubé mřížky čte přehledy dlaždice, ne plné rozlišení."""
    import tempfile

    xmin, ymin, xmax, ymax = window
    w = max(1, int(round((xmax - xmin) / cell_m)))
    h = max(1, int(round((ymax - ymin) / cell_m)))
    mask = np.zeros((h, w), dtype=bool)
    with tempfile.TemporaryDirectory(prefix="sct-cov-") as tmp:
        for k, src in enumerate(sources):
            dest = Path(tmp) / f"c{k}.tif"
            run_cmd([_gdal_tool("gdalwarp"), "-q", "--config", "GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR",
                     "--config", "GDAL_HTTP_MAX_RETRY", "3", "-te", f"{xmin}", f"{ymin}", f"{xmax}", f"{ymax}",
                     "-ts", str(w), str(h), "-r", "near", "-dstnodata", str(NODATA), "-ot", "Float32", src, str(dest)])
            arr = _read(dest)
            if arr.shape == mask.shape:
                mask |= arr > NODATA / 2
    return mask


def sct_coverage(bbox_wgs84: tuple[float, float, float, float]) -> float:
    """Podíl výřezu s platným terénem v dlaždicích (z přehledů dlaždic, hrubá mřížka 20 m)."""
    window = _bounds_in(bbox_wgs84, "EPSG:4326", SCT_CRS)
    tiles = sct_tiles("dtm", window, sct_index())
    if not tiles:
        return 0.0
    return float(sct_valid_mask([f"/vsicurl/{SCT_BUCKET}/{key}" for _, key, _ in tiles], window).mean())


def gb_sct_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """Skotsko: DTM a DSM 0,5–1 m ze Scottish Remote Sensing Portal (okno přes /vsicurl, novější fáze má přednost)."""
    window = _buffered(bounds, SCT_CRS, SCT_BUFFER_M)
    index = sct_index()
    out: dict[str, Path | None] = {}
    for kind in ("dtm", "dsm"):
        tiles = sct_tiles(kind, window, index)
        if log and kind == "dtm" and tiles:
            labels = dict(SCT_DATASETS)
            used = sorted({f for f, _, _ in tiles}, key=[f for f, _ in SCT_DATASETS].index)
            log(f"  Skotsko LiDAR: {', '.join(labels[f] for f in used)}")
        urls = [f"{SCT_BUCKET}/{key}" for _, key, _ in tiles]
        out[kind] = cog_windows(urls, window, SCT_CRS, out_dir, f"gb_sct_{kind}", log=log, resolution="highest") if urls else None
    if out["dtm"] is None:
        raise ElevationUnavailable("výřez je mimo pokrytí LiDARu Scottish Remote Sensing Portal")
    # Kontrola před jobem (``elevation_coverage``) mohla nestihnout výpis bucketu – tady už jsou okna stažená.
    share = float(sct_valid_mask([str(out["dtm"])], window).mean())
    if share < COVERAGE_MIN_SHARE:
        raise ElevationUnavailable(
            f"LiDAR Scottish Remote Sensing Portal pokrývá jen {share:.0%} výřezu; posuňte výřez do pokrytého území"
        )
    if out["dsm"] is None:
        log_issue(log, "Skotsko: povrch (DSM) pro výřez chybí, výška porostu se nespočítá")
    return SourceRasters(out["dtm"], out["dsm"])


PROVIDERS["gb_sct"] = gb_sct_provider


# --------------------------------------------------------------------------- GB-WLS: Welsh Government LiDAR 2020–23

# DataMapWales (OGL v3, bez klíče): kompozit DTM a DSM 1 m z národního LiDARu 2020–23 (zimní nálety), každý jeden COG
# na celý Wales v EPSG:27700 (pokrytí země 99,97 %). Čte se jen okno výřezu (~2,5 s na km² a model).
WLS_CRS = "EPSG:27700"
WLS_BUFFER_M = 20.0


def gb_wls_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """Wales: DTM a DSM 1 m – okno výřezu z celostátních COG DataMapWales."""
    from app.pipeline.reference_gb import WLS_DSM_URL, WLS_DTM_URL

    window = _buffered(bounds, WLS_CRS, WLS_BUFFER_M)
    dtm = cog_windows([WLS_DTM_URL], window, WLS_CRS, out_dir, "gb_wls_dtm", log=log, nodata=NODATA)
    if dtm is None:
        raise ElevationUnavailable("výřez je mimo pokrytí LiDARu Welsh Government (Wales)")
    dsm = cog_windows([WLS_DSM_URL], window, WLS_CRS, out_dir, "gb_wls_dsm", log=log, nodata=NODATA)
    if dsm is None:
        log_issue(log, "Wales: povrch (DSM) pro výřez chybí, výška porostu se nespočítá")
    return SourceRasters(dtm, dsm)


PROVIDERS["gb_wls"] = gb_wls_provider


# --------------------------------------------------------------------------- DK: DHM terén + povrch (Datafordeler)

# DHM/Terræn a DHM/Overflade (Klimadatastyrelsen, lidar, mřížka 0,4 m, CC BY 4.0) z WCS Datafordeleru v nativním
# EPSG:25832, výšky DVR90 (nadmořské). Stahuje se po 1 m (dlaždice 2 × 2 km ≈ 16 MB, ~30 s). Služba chce API klíč
# (``reference_dk``): v URL je jen zástupný text, klíč se dosadí až v ``_dk_download`` a chyby jdou ven bez URL.
# Datafordeler občas (hlavně po založení klíče) vrátí 401 na platný klíč – 401/403 se proto několikrát zopakuje.
DK_MAX_SIDE_M = 2000.0
DK_BUFFER_M = 20.0
DK_RETRIES = 4


def dk_dhm_urls(kind: str, bounds_utm: tuple[float, float, float, float]) -> list[str]:
    """GetCoverage URL (se zástupným klíčem) po dlaždicích nejvýš ``DK_MAX_SIDE_M``, hrany na celé metry, pixel 1 m."""
    from app.pipeline.reference_dk import DK_COVERAGES, DK_CRS, DK_WCS, KEY_PLACEHOLDER

    out = []
    for x0, y0, x1, y1 in tile_boxes(bounds_utm, max_side=DK_MAX_SIDE_M, buffer=DK_BUFFER_M, step=1.0):
        out.append(
            f"{DK_WCS}?apikey={KEY_PLACEHOLDER}&SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage"
            f"&COVERAGE={DK_COVERAGES[kind]}&CRS={DK_CRS}&RESPONSE_CRS={DK_CRS}"
            f"&BBOX={x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}&WIDTH={round(x1 - x0)}&HEIGHT={round(y1 - y0)}&FORMAT=GTiff"
        )
    return out


def _dk_download(url: str, dest: Path, *, log=None, wait_s: float = 3.0) -> Path:
    """Dlaždice DHM: klíč až tady, opakování po 401/403, 5xx, 429 a chybě sítě; chyba ven bez URL a bez klíče."""
    import time
    import urllib.error

    from app.pipeline.reference_dk import redact, with_key

    for attempt in range(DK_RETRIES + 1):
        try:
            return _download(with_key(url), dest, log=log)
        except urllib.error.HTTPError as exc:
            # HTTPError nese URL s klíčem (exc.url) – dál jde jen kód
            code = exc.code
            if attempt >= DK_RETRIES or not (code in (401, 403, 429) or code >= 500):
                raise RuntimeError(f"Datafordeler DHM: HTTP {code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt >= DK_RETRIES:
                raise RuntimeError(redact(f"Datafordeler DHM: {type(exc).__name__}: {getattr(exc, 'reason', exc)}")) from None
        except RuntimeError as exc:
            raise RuntimeError(redact(str(exc))) from None
        if log:
            log(f"  {dest.name}: přechodná chyba služby, pokus {attempt + 2}/{DK_RETRIES + 1}")
        time.sleep(wait_s * (attempt + 1))
    raise AssertionError("nedosažitelné")


def dk_dhm_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """DHM: terén (dhm_terraen) a povrch (dhm_overflade) z WCS Datafordeleru po dlaždicích 2 km."""
    from concurrent.futures import ThreadPoolExecutor

    from app.pipeline.reference_dk import DK_COVERAGES, DK_CRS

    bounds_utm = _bounds_in(bounds, CRS_WKT, DK_CRS)
    jobs = [(kind, k, url) for kind in DK_COVERAGES for k, url in enumerate(dk_dhm_urls(kind, bounds_utm))]
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="dk-dhm") as ex:
        tiles = list(ex.map(lambda j: _dk_download(j[2], out_dir / f"dk_{j[0]}_{j[1]}.tif", log=log), jobs))
    out = {}
    for kind in DK_COVERAGES:
        vrt = out_dir / f"dk_dhm_{kind}.vrt"
        mine = [str(t) for (k, _i, _u), t in zip(jobs, tiles) if k == kind]
        run_cmd([_gdal_tool("gdalbuildvrt"), "-a_srs", DK_CRS, str(vrt), *mine], log=log)
        out[kind] = vrt
    return SourceRasters(out["dtm"], out["dsm"])


PROVIDERS["dk_dhm"] = dk_dhm_provider


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
            log("  Výška porostu Metsäkeskus pro výřez chybí (latvusmalli je jen pro pevninské Finsko, ne Ålandy).")
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


def sk_ugkk_local_provider(bounds, out_dir: Path, log=None) -> SourceRasters:
    """DMR 5.0 a DMP 1.0 Slovenska z celostátních GeoTIFFů ÚGKK na disku (S-JTSK [JTSK03] + Bpv, 1 m).

    Ručně stažené ZIPy z opendata.skgeodesy.sk rozbalené do ``DATA_ROOT/cache/static/sk_ugkk/{dtm,dsm}``
    (úklid cache tuto složku nemaže). Terén i povrch jsou v témže systému a výškách Bpv, proto bez kvazigeoidu;
    JTSK03 se bere jako souřadnice mapy (``_warp_to_job``)."""
    from app.countries import LOCAL_ELEVATION_DIRS

    return local_provider(f"local:{LOCAL_ELEVATION_DIRS['sk_ugkk_local']}")(bounds, out_dir, log=log)


PROVIDERS["sk_ugkk_local"] = sk_ugkk_local_provider


SOURCE_LABELS = {
    "nl_ahn": "AHN, PDOK",
    "sk_ugkk": "DMR 5.0, ÚGKK SR",
    "sk_ugkk_local": "DMR 5.0 a DMP 1.0, ÚGKK SR",
    "ch_swisstopo": "swissALTI3D a swissSURFACE3D, swisstopo",
    "at_bev": "BEV ALS DTM a DSM",
    "de_berlin": "DGM1 a DOM1, GDI Berlin",
    "de_nrw": "DGM1, DOM1 a nDOM50, Geobasis NRW",
    "pl_gugik": "NMT a NMPT, GUGiK",
    "gb_ea": "LIDAR Composite DTM a DSM, Environment Agency",
    "de_by": "DGM1 a DOM20, LDBV Bayern",
    "fr_lidarhd": "LiDAR HD MNT a MNS, IGN",
    "ee_maaamet": "DTM a DSM, Maa- ja Ruumiamet",
    "it_trentino": "LiDAR DTM a DSM, Provincia autonoma di Trento",
    "be_dhmv": "DHMV II DTM a DSM, Digitaal Vlaanderen",
    "fi_mml": "Korkeusmalli 2 m MML a latvusmalli Metsäkeskus",
    "lu_act": "LiDAR 2024 MNT a MNS, ACT Luxembourg",
    "de_st": "DGM1 a DOM1, LVermGeo Sachsen-Anhalt",
    "de_mv": "DGM1 a DOM1, LAiV Mecklenburg-Vorpommern",
    "de_ni": "DGM1 a DOM1, LGLN Niedersachsen",
    "de_bb": "DGM1 a bDOM1, LGB Brandenburg",
    "de_rp": "DGM1 a DOM1, LVermGeo Rheinland-Pfalz",
    "de_bw": "DGM1 a nDOM1, LGL Baden-Württemberg",
    "de_sn": "DGM1 a DOM1, GeoSN Sachsen",
    "de_he": "DGM1 a DOM1, HVBG Hessen",
    "de_sl": "DGM1 a DOM1, LVGL Saarland",
    "de_hh": "DGM1 a bDOM, LGV Hamburg",
    "de_hb": "DGM1 a DOM1, GeoInformation Bremen",
    "dk_dhm": "DHM terén a povrch, Klimadatastyrelsen",
    "no_hoyde": "Høydemodell DTM a DOM, Kartverket",
    "si_arso": "mračno LiDAR, ARSO",
    "us_3dep": "mračno LiDAR 3DEP, USGS",
    "gb_sct": "LiDAR DTM a DSM, Scottish Government",
    "gb_wls": "LiDAR DTM a DSM, Welsh Government",
    "es_nav": "mračno LiDAR 2024, Gobierno de Navarra",
    USER_UPLOAD: "vlastní data zadavatele",
}


# Pokrytí výřezu výškami ještě před jobem (zdroje, které nejsou celoplošné): podíl 0–1.
COVERAGE: dict[str, Callable[[tuple[float, float, float, float]], float]] = {
    "fr_lidarhd": fr_lidarhd_coverage,
    "it_trentino": it_trentino_coverage,
    "gb_sct": sct_coverage,
    "us_3dep": us_3dep_coverage,
}
# Nasnímaná, ale ještě nezveřejněná data ve výřezu (jména projektů) – jen pro přesnější hlášku při nulovém pokrytí.
COVERAGE_PENDING: dict[str, Callable[[tuple[float, float, float, float]], list[str]]] = {
    "us_3dep": us_3dep_pending,
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


def coverage_pending(source: str, bbox_wgs84) -> list[str]:
    """Nezveřejněné projekty ve výřezu (po ``elevation_coverage``); bez podpory nebo při chybě prázdné."""
    check = COVERAGE_PENDING.get(source)
    if check is None:
        return []
    try:
        return list(check(tuple(float(v) for v in bbox_wgs84)))
    except Exception:  # noqa: BLE001 – jen text hlášky
        return []


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


# Značka v ``work/dem``: zdroj pro výřez neměl model povrchu, vegetace se kreslí z OSM (ne z CHM).
NO_SURFACE_MARK = "no_surface.txt"


def surface_missing(work_dir: Path) -> bool:
    from app.pipeline.dem_prep import DEM_DIR_NAME

    return (Path(work_dir) / DEM_DIR_NAME / NO_SURFACE_MARK).is_file()


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
    surface_mark = dem_dir / NO_SURFACE_MARK
    surface_mark.unlink(missing_ok=True)
    if dsm_src is None:
        # Bez modelu povrchu (Ålandy: latvusmalli Metsäkeskus je jen pro pevninu) job nepadá: terén z DTM,
        # vegetace z přírodního krytu OSM (``vegetation_chm.generate_vegetation_from_osm``), s varováním.
        log_issue(log, f"Výškové modely: zdroj {label} pro výřez nemá model povrchu – vegetace se kreslí z OSM "
                       "(les bílý, louky, vřesoviště a křoviny podle OSM), bez výšky porostu")
        surface_mark.write_text("no_surface\n", encoding="utf-8")

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

    dsm_raw = dsm_filled = chm = None
    for stale in ("dsm_raw.tif", "dsm_filled.tif", "chm.tif"):
        (dem_dir / stale).unlink(missing_ok=True)
    if dsm_src is not None:
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
