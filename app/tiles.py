from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

from app.pipeline.fetch_openzu import USER_AGENT
from app.settings import CACHE_DIR

TILE_SOURCES = (
    "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
)
TILE_TIMEOUT_S = 20


class TileError(RuntimeError):
    pass


def tile_cache_path(z: int, x: int, y: int) -> Path:
    return CACHE_DIR / "tiles" / str(z) / str(x) / f"{y}.png"


def validate_tile(z: int, x: int, y: int) -> None:
    if not (0 <= z <= 18):
        raise TileError("Neplatný zoom dlaždice")
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):
        raise TileError("Dlaždice mimo rozsah")


def fetch_tile(z: int, x: int, y: int) -> Path:
    """Stáhne OSM/Carto dlaždici přes NAS (prohlížeč nemluví s CDN)."""
    validate_tile(z, x, y)
    dest = tile_cache_path(z, x, y)
    if dest.exists() and dest.stat().st_size > 50:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err = "žádný zdroj"
    for template in TILE_SOURCES:
        url = template.format(z=z, x=x, y=y)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/png"})
        try:
            with urllib.request.urlopen(req, timeout=TILE_TIMEOUT_S) as resp:
                data = resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_err = str(getattr(exc, "reason", exc))
            continue
        if len(data) < 50:
            last_err = "prázdná dlaždice"
            continue
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return dest
    raise TileError(f"Dlaždice se nepodařilo stáhnout: {last_err}")
