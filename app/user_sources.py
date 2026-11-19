"""Vlastní výšková data zadavatele (Omapic mimo Česko, návrh evropa-data/uzivatelske-zdroje-navrh.md, Petr 2026-11-18).

Některé zdroje nesmíme stahovat a sdílet za sebe (licence vázaná na uživatele, placená data) nebo je nejde číst
po výřezech. Zadavatel si je stáhne sám a k jobu nahraje dva malé GeoTIFFy: terén (DTM) a povrch (DSM). Použijí se
jen pro jeho job:

- soubor je nedůvěryhodný: kontroluje se hlavička TIFF, otevírá jen ovladač GTiff (žádné VRT ani ``/vsi*`` odkazy),
  bez síťového přístupu GDAL, v samostatném procesu s časovým a paměťovým limitem; dál se pracuje jen s čistou
  kopií oříznutou na výřez (``gdal_translate``), originál se hned maže;
- uložení jen ve složce jobu ``jobs/<id>/user_sources/{dtm,dsm}/`` (nikdy pod ``cache/``), job je vždy soukromý,
  nenabízí se k převzetí dat a nededuplikuje se s jiným jobem;
- po doběhnutí jobu (úspěch i chyba) se nahraná data i jejich zdrojové VRT smažou (``cleanup_job``), nespuštěné
  nahrávky po ``STAGING_MAX_AGE_S`` (``prune_staging``);
- log jobu nese jen velikost, CRS a rozlišení – žádné názvy souborů ani metadata z TIFF tagů.

Vypínač ``USER_SOURCES`` (výchozí vypnuto); v Česku se nahrávání nenabízí ani nepřijímá.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from app import settings

logger = logging.getLogger("podkladarna")

KINDS = ("dtm", "dsm")
SOURCES_DIR_NAME = "user_sources"
STAGING_DIR_NAME = "user_uploads"
# Cloudflare (zdarma) propustí tělo požadavku do 100 MB → oba soubory dohromady pod tímto stropem.
MAX_TOTAL_MB = settings.USER_SOURCE_MAX_MB
MAX_SIDE_PX = 20_000
PIXEL_MIN_M = 0.1
PIXEL_MAX_M = 5.0
MIN_COVERAGE = 0.9
CROP_BUFFER_M = 200.0
ELEV_MIN_M = -500.0
ELEV_MAX_M = 9000.0
CHECK_TIMEOUT_S = 120
CHECK_MEMORY_BYTES = 2 * 1024**3
STAGING_MAX_AGE_S = 2 * 3600
_CHUNK = 1024 * 1024
# Hlavičky TIFF a BigTIFF (little / big endian).
_TIFF_MAGIC = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")


class UserSourceError(ValueError):
    """Nahraný soubor nelze použít; ``key`` je klíč hlášky ``api.user_sources.*``, ``params`` její proměnné."""

    def __init__(self, key: str, **params: Any) -> None:
        super().__init__(key)
        self.key = key
        self.params = params


@dataclass
class RasterInfo:
    epsg: str
    pixel_m: float
    width: int
    height: int
    coverage: float
    size_mb: float

    def log_text(self) -> str:
        return f"{self.epsg}, {self.pixel_m:g} m, pokrytí výřezu {self.coverage:.0%}, {self.size_mb:.0f} MB"


def enabled() -> bool:
    return settings.USER_SOURCES_ENABLED


def staging_root() -> Path:
    return settings.DATA_ROOT / STAGING_DIR_NAME


def job_sources_dir(job_dir: Path) -> Path:
    return Path(job_dir) / SOURCES_DIR_NAME


def new_staging_dir() -> Path:
    path = staging_root() / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    return path


def save_upload(src: BinaryIO, dest: Path, *, max_bytes: int) -> int:
    """Zkopíruje nahraný soubor do ``dest`` se stropem velikosti; vrací počet bajtů. Kontroluje hlavičku TIFF."""
    head = src.read(4)
    if head not in _TIFF_MAGIC:
        raise UserSourceError("api.user_sources.not_geotiff")
    written = len(head)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        out.write(head)
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                out.close()
                dest.unlink(missing_ok=True)
                raise UserSourceError("api.user_sources.too_big", max=MAX_TOTAL_MB)
            out.write(chunk)
    return written


def _gdal_env() -> dict[str, str]:
    """Prostředí GDAL bez sítě a bez čtení okolních souborů (jen nahraný TIFF)."""
    from app.tool_env import gis_subprocess_env

    env = dict(gis_subprocess_env("gdalinfo"))
    env.update(
        {
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".none-allowed",
            "GDAL_HTTP_MAX_RETRY": "0",
            "GDAL_HTTP_TIMEOUT": "1",
            "CPL_GS_CREDENTIALS_FILE": "",
            "GDAL_PAM_ENABLED": "NO",
            "GTIFF_REPORT_COMPD_CS": "YES",
            "GDAL_CACHEMAX": "256",
        }
    )
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        env.pop(key, None)
    env["NO_PROXY"] = env["no_proxy"] = "*"
    return env


def _limit_resources() -> None:  # pragma: no cover – běží v potomkovi
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (CHECK_MEMORY_BYTES, CHECK_MEMORY_BYTES))
        os.nice(10)
    except Exception:
        pass


def _run_gdal(cmd: list[str]) -> subprocess.CompletedProcess:
    """GDAL nástroj nad nedůvěryhodným souborem: vlastní proces, limit času a paměti, bez sítě."""
    kwargs: dict[str, Any] = {}
    if os.name == "posix":
        kwargs["preexec_fn"] = _limit_resources
    try:
        result = subprocess.run(
            cmd,
            check=False,
            text=True,
            capture_output=True,
            env=_gdal_env(),
            timeout=CHECK_TIMEOUT_S,
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        raise UserSourceError("api.user_sources.unreadable") from exc
    if result.returncode != 0:
        raise UserSourceError("api.user_sources.unreadable")
    return result


def _gdal_tool(name: str) -> str:
    from app.pipeline.elevation_raster import _gdal_tool as tool

    return tool(name)


def _crs_check(wkt: str) -> str:
    """Projektovaný CRS v metrech → ``EPSG:xxxx`` (nebo „vlastní CRS“); jinak chyba."""
    from pyproj import CRS

    if not wkt.strip():
        raise UserSourceError("api.user_sources.no_crs")
    try:
        crs = CRS.from_wkt(wkt)
    except Exception as exc:  # noqa: BLE001 – nečitelný CRS = nepoužitelný soubor
        raise UserSourceError("api.user_sources.no_crs") from exc
    horizontal = crs.sub_crs_list[0] if crs.is_compound and crs.sub_crs_list else crs
    if not horizontal.is_projected:
        raise UserSourceError("api.user_sources.not_projected")
    units = {(ax.unit_name or "").lower() for ax in horizontal.axis_info[:2]}
    if not units or not units <= {"metre", "meter", "m"}:
        raise UserSourceError("api.user_sources.not_projected")
    epsg = horizontal.to_epsg(min_confidence=70)
    return f"EPSG:{epsg}" if epsg else "vlastní CRS"


def bbox_in_crs(bbox_wgs84, wkt: str) -> tuple[float, float, float, float]:
    from pyproj import CRS, Transformer

    tr = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_wkt(wkt), always_xy=True)
    return tuple(tr.transform_bounds(*bbox_wgs84, densify_pts=21))  # type: ignore[return-value]


def coverage_share(raster_bounds, area_bounds) -> float:
    rx0, ry0, rx1, ry1 = raster_bounds
    ax0, ay0, ax1, ay1 = area_bounds
    area = max(ax1 - ax0, 0.0) * max(ay1 - ay0, 0.0)
    if area <= 0:
        return 0.0
    ix = max(min(rx1, ax1) - max(rx0, ax0), 0.0)
    iy = max(min(ry1, ay1) - max(ry0, ay0), 0.0)
    return ix * iy / area


def inspect(path: Path, bbox_wgs84) -> tuple[RasterInfo, dict]:
    """Kontrola nahraného TIFFu (jen ovladač GTiff) → informace a ``gdalinfo`` JSON. Vyhazuje ``UserSourceError``."""
    res = _run_gdal([_gdal_tool("gdalinfo"), "-json", "-mm", "-if", "GTiff", "-nofl", str(path)])
    try:
        info = json.loads(res.stdout)
    except ValueError as exc:
        raise UserSourceError("api.user_sources.unreadable") from exc
    if str(info.get("driverShortName") or "") != "GTiff":
        raise UserSourceError("api.user_sources.not_geotiff")
    files = [Path(f).name for f in (info.get("files") or [])]
    if files and files != [path.name]:
        raise UserSourceError("api.user_sources.not_geotiff")
    width, height = (int(v) for v in (info.get("size") or [0, 0]))
    if width <= 0 or height <= 0 or max(width, height) > MAX_SIDE_PX:
        raise UserSourceError("api.user_sources.dimensions", max=MAX_SIDE_PX)
    bands = info.get("bands") or []
    if not bands:
        raise UserSourceError("api.user_sources.unreadable")
    gt = info.get("geoTransform") or []
    if len(gt) != 6 or abs(float(gt[2])) > 1e-9 or abs(float(gt[4])) > 1e-9:
        raise UserSourceError("api.user_sources.rotated")
    wkt = str((info.get("coordinateSystem") or {}).get("wkt") or "")
    epsg = _crs_check(wkt)
    pixel = abs(float(gt[1]))
    if not (PIXEL_MIN_M <= pixel <= PIXEL_MAX_M) or not (PIXEL_MIN_M <= abs(float(gt[5])) <= PIXEL_MAX_M):
        raise UserSourceError("api.user_sources.resolution", min=PIXEL_MIN_M, max=PIXEL_MAX_M)
    band = bands[0]
    lo, hi = band.get("computedMin"), band.get("computedMax")
    if lo is None or hi is None or float(lo) < ELEV_MIN_M or float(hi) > ELEV_MAX_M:
        raise UserSourceError("api.user_sources.values")
    x0, y0 = float(gt[0]), float(gt[3])
    x1, y1 = x0 + width * float(gt[1]), y0 + height * float(gt[5])
    raster_bounds = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    try:
        area = bbox_in_crs(bbox_wgs84, wkt)
    except Exception as exc:  # noqa: BLE001
        raise UserSourceError("api.user_sources.no_crs") from exc
    share = coverage_share(raster_bounds, area)
    if share < MIN_COVERAGE:
        raise UserSourceError("api.user_sources.coverage", share=round(share * 100), min=round(MIN_COVERAGE * 100))
    size_mb = path.stat().st_size / 1e6 if path.exists() else 0.0
    return RasterInfo(epsg, pixel, width, height, share, size_mb), {"wkt": wkt, "area": area}


def clean_copy(src: Path, dest: Path, area: tuple[float, float, float, float]) -> Path:
    """Čistá kopie: první pásmo, Float32, oříznuté na výřez s přesahem; originál se smaže."""
    ax0, ay0, ax1, ay1 = area
    b = CROP_BUFFER_M
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run_gdal(
            [
                _gdal_tool("gdal_translate"),
                "-q",
                "-if",
                "GTiff",
                "-of",
                "GTiff",
                "-b",
                "1",
                "-ot",
                "Float32",
                "-projwin",
                *(f"{v:.3f}" for v in (ax0 - b, ay1 + b, ax1 + b, ay0 - b)),
                "-co",
                "COMPRESS=DEFLATE",
                "-co",
                "TILED=YES",
                "-co",
                "BIGTIFF=IF_SAFER",
                str(src),
                str(dest),
            ]
        )
    finally:
        src.unlink(missing_ok=True)
    if not dest.is_file() or dest.stat().st_size < 200:
        raise UserSourceError("api.user_sources.unreadable")
    return dest


def prepare(staging: Path, bbox_wgs84) -> dict[str, RasterInfo]:
    """Kontrola a čistá kopie obou nahraných souborů ve ``staging/<kind>.upload`` → ``staging/<kind>/<kind>.tif``."""
    out: dict[str, RasterInfo] = {}
    for kind in KINDS:
        raw = staging / f"{kind}.upload"
        try:
            info, meta = inspect(raw, bbox_wgs84)
        except UserSourceError as exc:
            exc.params.setdefault("kind", kind.upper())
            raise
        clean_copy(raw, staging / kind / f"{kind}.tif", meta["area"])
        out[kind] = info
    return out


def attach(staging: Path, job_dir: Path) -> Path:
    """Přesune připravená data do složky jobu."""
    dest = job_sources_dir(job_dir)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        src = staging / kind
        if src.is_dir():
            shutil.move(str(src), str(dest / kind))
    shutil.rmtree(staging, ignore_errors=True)
    return dest


def discard(staging: Path | None) -> None:
    if staging is not None:
        shutil.rmtree(staging, ignore_errors=True)


def cleanup_job(job_dir: Path) -> bool:
    """Smaže nahraná data jobu a zdrojové VRT, které na ně odkazují; vrací True, pokud něco smazal."""
    removed = False
    src = job_sources_dir(job_dir)
    if src.exists():
        shutil.rmtree(src, ignore_errors=True)
        removed = True
    dem_src = Path(job_dir) / "work" / "dem" / "src"
    if removed and dem_src.exists():
        shutil.rmtree(dem_src, ignore_errors=True)
    return removed


def prune_staging(*, now: float | None = None) -> int:
    """Nespuštěné nahrávky starší než ``STAGING_MAX_AGE_S`` pryč (start a denní úklid)."""
    root = staging_root()
    if not root.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for path in root.iterdir():
        try:
            if now - path.stat().st_mtime > STAGING_MAX_AGE_S:
                shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def prune_finished_jobs() -> int:
    """Pojistka: nahraná data jobů, které už neběží ani nečekají (pád workeru, smazaný job), pryč."""
    from app import db

    root = settings.JOBS_DIR
    if not root.is_dir():
        return 0
    removed = 0
    for src in root.glob(f"*/{SOURCES_DIR_NAME}"):
        job_dir = src.parent
        try:
            status = db.get_job(job_dir.name).get("status")
        except KeyError:
            status = None
        if status in ("running", "queued", "pending"):
            continue
        if cleanup_job(job_dir):
            removed += 1
    return removed
