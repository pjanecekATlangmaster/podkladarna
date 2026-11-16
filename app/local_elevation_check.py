"""Kontrola ručně stažených výšek na disku před zařazením země (Slovensko: ``sk_ugkk_local``).

Spouští se na serveru v kontejneru, do mapy nic nekreslí::

    docker exec podkladarna python -m app.local_elevation_check [zdroj] [--at LON,LAT] [--size M]

Ověří soubory terénu (``dtm``) a povrchu (``dsm``), souřadnice (Křovák), dlaždicování a přehledy, připraví
výřez stejnou cestou jako job (``prepare_raster_surfaces``) a porovná povrch s terénem na holé zemi: 5. percentil
rozdílu DSM − DTM má být kolem nuly. Posun výškových systémů (např. elipsoidické výšky proti Bpv) by dal
desítky metrů, posun polohy záporné hodnoty na hranách. Vypíše čas a paměť. Návratový kód 0 = v pořádku.
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

BARE_P5_MAX_M = 0.3  # 5. percentil DSM − DTM: holá země má povrch = terén
BARE_P5_MIN_M = -0.3
VALID_MIN = 0.9  # podíl buněk s daty ve výřezu uvnitř země


def _info(path: Path) -> dict:
    from app.pipeline.gdal_cli_raster import _gdalinfo_json

    return _gdalinfo_json(path)


def _describe(path: Path, meta: dict) -> tuple[str, bool]:
    wkt = (meta.get("coordinateSystem") or {}).get("wkt") or ""
    band = (meta.get("bands") or [{}])[0]
    block = band.get("block") or [0, 0]
    struct = (meta.get("metadata") or {}).get("IMAGE_STRUCTURE") or {}
    overviews = len(band.get("overviews") or [])
    size = meta.get("size") or [0, 0]
    gt = meta.get("geoTransform") or [0, 0, 0, 0, 0, 0]
    krovak = "Krovak" in wkt
    tiled = block[0] > 1 and block[1] > 1 and block[0] < size[0]
    text = (
        f"  {path.name}: {path.stat().st_size / 1e9:.1f} GB, {size[0]} × {size[1]} px, pixel {abs(gt[1]):g} m, "
        f"bloky {block[0]}×{block[1]}, komprese {struct.get('COMPRESSION', '–')}, přehledů {overviews}, "
        f"Křovák {'ano' if krovak else 'NE'}"
    )
    return text, krovak and tiled


def _extent(meta: dict) -> tuple[float, float, float, float]:
    size = meta["size"]
    gt = meta["geoTransform"]
    x0, y0 = gt[0], gt[3]
    x1, y1 = x0 + gt[1] * size[0], y0 + gt[5] * size[1]
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _read(path: Path) -> np.ndarray:
    from app.pipeline.gdal_cli_raster import read_float32_geotiff

    arr, _gt, nodata = read_float32_geotiff(path)
    arr = arr.astype(np.float32)
    bad = ~np.isfinite(arr) | (np.abs(arr) > 1e6)
    if nodata is not None:
        bad |= arr == np.float32(nodata)
    arr[bad] = np.nan
    return arr


def run(source: str, lon: float, lat: float, size_m: float, out=print) -> bool:
    from pyproj import Transformer

    from app.countries import local_elevation_dir
    from app.pipeline.crs_5514 import CRS_WKT
    from app.pipeline.elevation_raster import ElevationUnavailable, prepare_raster_surfaces

    ok = True
    base = local_elevation_dir(source)
    if base is None:
        out(f"✗ {source}: není zdroj výšek z disku")
        return False
    out(f"Zdroj {source}: {base}")
    extents = []
    for sub in ("dtm", "dsm"):
        tifs = sorted((base / sub).glob("*.tif")) if (base / sub).is_dir() else []
        if not tifs:
            out(f"✗ {sub}: ve složce {base / sub} není žádný GeoTIFF")
            ok = False
            continue
        out(f"{sub}:")
        for tif in tifs:
            try:
                meta = _info(tif)
            except Exception as exc:  # noqa: BLE001 – kontrola má vypsat všechno
                out(f"✗ {tif.name}: nejde přečíst ({exc})")
                ok = False
                continue
            text, good = _describe(tif, meta)
            out(("✓" if good else "✗") + text[1:])
            ok &= good
            extents.append(_extent(meta))
    if not ok:
        return False

    x, y = Transformer.from_crs("EPSG:4326", CRS_WKT, always_xy=True).transform(lon, lat)
    half = size_m / 2
    bounds = (x - half, y - half, x + half, y + half)
    if not all(e[0] <= bounds[0] and e[1] <= bounds[1] and e[2] >= bounds[2] and e[3] >= bounds[3] for e in extents):
        out(f"✗ výřez {lon:.4f}, {lat:.4f} ({size_m:g} m) leží mimo rozsah souborů")
        return False

    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="elev_check_") as tmp:
        try:
            res = prepare_raster_surfaces(Path(tmp), bounds, source=source, resolution_m=1.0, log=lambda _m: None)
        except ElevationUnavailable as exc:
            out(f"✗ výřez {lon:.4f}, {lat:.4f} nejde připravit: {exc} (zkus --at uvnitř pokrytí)")
            return False
        seconds = time.monotonic() - t0
        dem = _read(res.dem_raw)
        dsm = _read(res.dsm_raw)
        chm = _read(res.chm) if res.chm else None
    valid = float(np.mean(np.isfinite(dem) & np.isfinite(dsm)))
    diff = (dsm - dem)[np.isfinite(dem) & np.isfinite(dsm)]
    p5, p50, p95 = (float(np.percentile(diff, q)) for q in (5, 50, 95)) if diff.size else (np.nan,) * 3
    tall = float(np.nanmean(chm > 3)) if chm is not None else float("nan")
    rss = max(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    )
    out(
        f"Výřez {size_m:g} × {size_m:g} m u {lon:.4f}, {lat:.4f}: příprava {seconds:.1f} s, paměť nejvýš "
        f"{rss / 1024:.0f} MB, buněk s daty {valid:.0%}"
    )
    out(f"Terén {np.nanmin(dem):.1f}–{np.nanmax(dem):.1f} m n. m.; DSM − DTM: 5. percentil {p5:.2f} m, "
        f"medián {p50:.2f} m, 95. percentil {p95:.1f} m; porost nad 3 m {tall:.0%}")
    if valid < VALID_MIN:
        out(f"✗ málo buněk s daty (pod {VALID_MIN:.0%}) – výřez na hranici nebo chybí data")
        ok = False
    if not BARE_P5_MIN_M <= p5 <= BARE_P5_MAX_M:
        out(f"✗ povrch a terén na holé zemi nesedí (5. percentil {p5:.2f} m mimo ±{BARE_P5_MAX_M} m) – "
            "jiný výškový systém nebo posun")
        ok = False
    out("✓ v pořádku" if ok else "✗ kontrola neprošla")
    return ok


def main(argv: list[str] | None = None) -> int:
    from app.countries import LOCAL_ELEVATION_DIRS

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", nargs="?", default=next(iter(LOCAL_ELEVATION_DIRS)))
    ap.add_argument("--at", default="19.1500,48.7400", help="střed výřezu LON,LAT (výchozí Banská Bystrica)")
    ap.add_argument("--size", type=float, default=2000.0, help="strana výřezu v m")
    ap.add_argument("--json", action="store_true", help="jen návratový kód a JSON souhrn")
    args = ap.parse_args(argv)
    lon, lat = (float(v) for v in args.at.split(","))
    lines: list[str] = []
    ok = run(args.source, lon, lat, args.size, out=lines.append if args.json else print)
    if args.json:
        print(json.dumps({"ok": ok, "lines": lines}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
