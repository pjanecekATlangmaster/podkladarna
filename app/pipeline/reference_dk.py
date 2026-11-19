"""Služby pro Dánsko (zahraniční build): výšky DHM z Datafordeleru (Klimadatastyrelsen).

DHM/Terræn (terén) a DHM/Overflade (povrch) z lidaru, mřížka 0,4 m, WCS ``dhm_wcs`` v EPSG:25832, výšky DVR90
(nadmořské, nic se nepřevádí). Licence CC BY 4.0 (Klimadatastyrelsen, volná geodata od 16. 5. 2024, i komerčně).

Datafordeler chce API klíč (IT systém v Datafordeler Administration, obnova po dvou letech). Klíč se čte jen
z proměnné prostředí ``DK_DATAFORDELER_API_KEY`` a do URL se dosadí až těsně před dotazem (``with_key``): nikdy
se nezapisuje do logu, chyb, cache, šablon ani ZIPu (proto Dánsko nemá online šablony – WMS s klíčem by klíč
v .omap prozradil). Bez klíče se země nenabízí (``countries.elevation_ready``).
Odpovědi Datafordeleru samy klíč ozvěnou vracejí (GetCapabilities v odkazech), proto se jejich text nikde
neukládá a chybové hlášky prochází ``redact``.
"""

from __future__ import annotations

import os
from pathlib import Path

DK_API_KEY_ENV = "DK_DATAFORDELER_API_KEY"
# Zástupný text v URL; skutečný klíč dosadí ``with_key`` až při dotazu (sonda, stahování).
KEY_PLACEHOLDER = "{dk_apikey}"

DK_CRS = "EPSG:25832"
DK_WCS = "https://wcs.datafordeler.dk/DHMNedboer/dhm_wcs/1.0.0/WCS"
DK_COVERAGES = {"dtm": "dhm_terraen", "dsm": "dhm_overflade"}
# Sonda: okno 8 × 8 m terénu (Dyrehaven u Kodaně).
DK_PROBE_BOX = (723400, 6185400, 723408, 6185408)
DK_ELEVATION_PROBE = (
    f"{DK_WCS}?apikey={KEY_PLACEHOLDER}&SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage&COVERAGE={DK_COVERAGES['dtm']}"
    f"&CRS={DK_CRS}&RESPONSE_CRS={DK_CRS}&BBOX={','.join(map(str, DK_PROBE_BOX))}&WIDTH=8&HEIGHT=8&FORMAT=GTiff"
)


def api_key() -> str:
    return os.environ.get(DK_API_KEY_ENV, "").strip()


def with_key(url: str) -> str:
    """URL se skutečným klíčem (jen pro samotný dotaz; výsledek nikam neukládat ani nelogovat)."""
    if KEY_PLACEHOLDER not in url:
        return url
    key = api_key()
    if not key:
        raise RuntimeError(f"chybí API klíč Datafordeleru ({DK_API_KEY_ENV})")
    return url.replace(KEY_PLACEHOLDER, key)


def redact(text: str) -> str:
    """Text bez klíče (chybové hlášky, odpovědi služby)."""
    key = api_key()
    return text.replace(key, "***") if key else text


# Ortofoto (jarní, GeoDanmark) jen pro masku zpevněných ploch ve sprintu (``open_paved``), stahuje server sám:
# WMS s klíčem nesmí do šablony .omap ani do XML zdroje GDAL ve work/. Dlaždice nejvýš ``DK_ORTHO_TILE_PX``
# (služba dovolí 10 000 px), JPEG + world file, CRS EPSG:25832; klíč jen v samotném dotazu.
DK_ORTHO_WMS = "https://wms.datafordeler.dk/GeoDanmarkOrto/orto_foraar/1.0.0/WMS"
DK_ORTHO_LAYER = "orto_foraar"
DK_ORTHO_TILE_PX = 4000
DK_ORTHO_RETRIES = 4
DK_ORTHO_MAX_BYTES = 80_000_000


def ortho_tile_urls(grid: tuple[float, float, float, float], width: int, height: int,
                    tile_px: int = DK_ORTHO_TILE_PX) -> list[tuple[str, tuple[float, float, float, float], int, int]]:
    """GetMap URL (se zástupným klíčem) po dlaždicích: (url, obálka EPSG:25832, šířka px, výška px)."""
    xmin, ymin, xmax, ymax = grid
    mx, my = (xmax - xmin) / width, (ymax - ymin) / height
    out = []
    for c0 in range(0, width, tile_px):
        for r0 in range(0, height, tile_px):
            w, h = min(tile_px, width - c0), min(tile_px, height - r0)
            box = (xmin + c0 * mx, ymax - (r0 + h) * my, xmin + (c0 + w) * mx, ymax - r0 * my)
            url = (
                f"{DK_ORTHO_WMS}?apikey={KEY_PLACEHOLDER}&SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS={DK_ORTHO_LAYER}"
                f"&STYLES=&CRS={DK_CRS}&BBOX={box[0]:.2f},{box[1]:.2f},{box[2]:.2f},{box[3]:.2f}"
                f"&WIDTH={w}&HEIGHT={h}&FORMAT=image/jpeg"
            )
            out.append((url, box, w, h))
    return out


def _get_image(url: str) -> bytes:
    import time
    import urllib.error
    import urllib.request

    from app.pipeline.fetch_openzu import USER_AGENT

    for attempt in range(DK_ORTHO_RETRIES + 1):
        try:
            req = urllib.request.Request(with_key(url), headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=180) as resp:
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read(DK_ORTHO_MAX_BYTES + 1)
            if len(raw) > DK_ORTHO_MAX_BYTES or not ctype.startswith("image/"):
                raise RuntimeError(f"ortofoto Datafordeler: nečekaná odpověď ({ctype}, {len(raw)} B)")
            return raw
        except urllib.error.HTTPError as exc:  # nese URL s klíčem – dál jen kód
            code = exc.code
            if attempt >= DK_ORTHO_RETRIES or not (code in (401, 403, 429) or code >= 500):
                raise RuntimeError(f"ortofoto Datafordeler: HTTP {code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt >= DK_ORTHO_RETRIES:
                raise RuntimeError(redact(f"ortofoto Datafordeler: {type(exc).__name__}")) from None
        time.sleep(3.0 * (attempt + 1))
    raise AssertionError("nedosažitelné")


def fetch_ortho(dest: Path, grid: tuple[float, float, float, float], width: int, height: int, dst_wkt: str,
                *, get=_get_image) -> Path:
    """Ortofoto výřezu → GeoTIFF ``dest`` (Byte, RGB) v souřadnicích jobu; dočasné dlaždice se smažou."""
    from osgeo import gdal

    tiles: list[Path] = []
    try:
        for k, (url, box, w, h) in enumerate(ortho_tile_urls(grid, width, height)):
            jpg = dest.with_name(f"_dk_ortho_{os.getpid()}_{k}.jpg")
            jpg.write_bytes(get(url))
            mx, my = (box[2] - box[0]) / w, (box[3] - box[1]) / h
            jpg.with_suffix(".wld").write_text(f"{mx}\n0\n0\n{-my}\n{box[0] + mx / 2}\n{box[3] - my / 2}\n", encoding="ascii")
            tiles.append(jpg)
        gdal.UseExceptions()
        ds = gdal.Warp(
            str(dest), [str(t) for t in tiles], format="GTiff", outputBounds=grid, width=width, height=height,
            srcSRS=DK_CRS, dstSRS=dst_wkt, resampleAlg="bilinear", outputType=gdal.GDT_Byte,
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
        )
        ds = None
    finally:
        for t in tiles:
            t.unlink(missing_ok=True)
            t.with_suffix(".wld").unlink(missing_ok=True)
    return dest
