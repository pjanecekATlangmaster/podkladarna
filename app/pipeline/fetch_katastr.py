"""Vektorová katastrální mapa (KM-KU-DXF ČÚZK) jako podklad pro OOM / OCAD.

Výřez → katastrální území (RÚIAN vrstva KatastralniUzemi) → DXF po KÚ
z ``services.cuzk.cz/dxf/ku/<kód>.zip`` (cache po KÚ) → linie oříznuté na
výřez s přesahem:

- ``katastr.gpkg`` (EPSG:5514) – OOM ho načte jako podklad (OgrTemplate) se
  správnou polohou; jde zapnout/vypnout a nastavit průhlednost. DXF samo CRS
  nenese, proto GeoPackage.
- ``katastr.dxf`` – totéž pro OCAD (podklad / import DXF).

Jen linie (hranice parcel, budovy, KÚ); body značek druhů pozemků a texty
vynecháváme – v podkladu jen zahlcují. Vrstva ``Layer`` = kód značky KM.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from app import settings
from app.download_cache import is_fresh, read_meta, utcnow_iso, write_meta
from app.pipeline.crs_5514 import CRS_PROJ4
from app.pipeline.fetch_openzu import (
    VECTOR_FETCH_BUFFER_M,
    FetchError,
    _http_download,
    crop_bounds_5514,
    expand_bbox_wgs84,
)
from app.pipeline.fetch_ruian import RUIAN_SERVICE
from app.pipeline.fetch_zabaged import query_layer_geojson
from app.pipeline.prepare_lidar import find_tool, run_cmd

KM_DXF_URL = "https://services.cuzk.cz/dxf/ku/{kod}.zip"
RUIAN_KU_LAYER_ID = 7  # KatastralniUzemi
KATASTR_GPKG = "katastr.gpkg"
KATASTR_DXF = "katastr.dxf"
# Linie bez bodů/textů (OGR DXF: vše v jedné vrstvě "entities").
_LINES_SQL = (
    "SELECT Layer FROM entities "
    "WHERE OGR_GEOMETRY IN ('LINESTRING', 'MULTILINESTRING')"
)


def katastr_cache_dir(kod: int | str) -> Path:
    return settings.DOWNLOADS_DIR / "katastr_dxf" / str(kod)


def ku_codes_for_bbox(bbox: tuple[float, float, float, float]) -> list[int]:
    """Kódy KÚ, která protínají výřez (WGS84 bbox)."""
    west, south, east, north = bbox
    gj = query_layer_geojson(RUIAN_SERVICE, RUIAN_KU_LAYER_ID, west, south, east, north)
    codes: list[int] = []
    for feat in gj.get("features") or []:
        props = feat.get("properties") or {}
        try:
            kod = int(props.get("kod"))
        except (TypeError, ValueError):
            continue
        if kod not in codes:
            codes.append(kod)
    return sorted(codes)


def fetch_ku_dxf(kod: int, *, log=None) -> Path | None:
    """DXF jednoho KÚ do cache (RUIAN_CACHE_MAX_AGE_DAYS). Při chybě None."""
    folder = katastr_cache_dir(kod)
    dxf = folder / f"{kod}.dxf"
    if is_fresh(folder, dxf, settings.RUIAN_CACHE_MAX_AGE_DAYS, min_size=100):
        return dxf
    folder.mkdir(parents=True, exist_ok=True)
    zpath = folder / f"{kod}.zip"
    try:
        _http_download(KM_DXF_URL.format(kod=kod), zpath)
        with zipfile.ZipFile(zpath) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".dxf")]
            if not names:
                raise FetchError(f"ZIP KÚ {kod} neobsahuje DXF")
            with zf.open(names[0]) as src, dxf.open("wb") as dst:
                dst.write(src.read())
    except (FetchError, OSError, zipfile.BadZipFile) as exc:
        if log:
            log(f"Katastr KÚ {kod}: přeskočeno ({exc})")
        return None
    finally:
        zpath.unlink(missing_ok=True)
    write_meta(folder, kind="katastr_dxf", kod=kod, downloaded_at=utcnow_iso())
    return dxf


def build_katastr_vectors(
    bbox: tuple[float, float, float, float],
    out_dir: Path,
    *,
    log=None,
) -> dict[str, Path]:
    """Katastr pro výřez → ``out_dir/katastr.gpkg`` + ``katastr.dxf``.

    Vrací {"katastr_vector": gpkg, "katastr_dxf": dxf} – co se povedlo.
    """
    out_dir = Path(out_dir)
    fetch_bbox = expand_bbox_wgs84(bbox, VECTOR_FETCH_BUFFER_M)
    try:
        codes = ku_codes_for_bbox(fetch_bbox)
    except FetchError as exc:
        if log:
            log(f"Katastr (vektor): přeskočeno – KÚ nezjištěna ({exc})")
        return {}
    sources = [p for p in (fetch_ku_dxf(k, log=log) for k in codes) if p]
    if not sources:
        if log:
            log("Katastr (vektor): žádné DXF ke stažení")
        return {}

    xmin, ymin, xmax, ymax = crop_bounds_5514(*bbox, buffer_m=VECTOR_FETCH_BUFFER_M)
    ogr2ogr = find_tool("ogr2ogr")
    gpkg = out_dir / KATASTR_GPKG
    gpkg.unlink(missing_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, src in enumerate(sources):
        run_cmd(
            [
                ogr2ogr,
                "-f", "GPKG",
                *(["-append"] if i else []),
                # Ne holé EPSG:5514: bez +towgs84 OOM převede datum jinak než
                # u georeferencovaných PNG a vektor je posunutý o metry.
                "-a_srs", CRS_PROJ4,
                "-dialect", "OGRSQL",
                "-sql", _LINES_SQL,
                "-clipsrc", str(xmin), str(ymin), str(xmax), str(ymax),
                "-nlt", "MULTILINESTRING",
                "-nln", "katastr",
                str(gpkg),
                str(src),
            ],
            log=log,
        )
    if not gpkg.is_file():
        if log:
            log("Katastr (vektor): ogr2ogr nevytvořil GeoPackage")
        return {}
    built = {"katastr_vector": gpkg}
    dxf = out_dir / KATASTR_DXF
    dxf.unlink(missing_ok=True)
    run_cmd([ogr2ogr, "-f", "DXF", str(dxf), str(gpkg)], log=log)
    if dxf.is_file():
        built["katastr_dxf"] = dxf
    if log:
        meta = [read_meta(katastr_cache_dir(k)) or {} for k in codes]
        dates = sorted({(m.get("downloaded_at") or "?")[:10] for m in meta})
        log(
            f"Katastr (vektor): {len(sources)} KÚ ({', '.join(map(str, codes))}), "
            f"staženo {', '.join(dates)} → {gpkg.name} + {dxf.name}"
        )
    return built
