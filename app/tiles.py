from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

from app.pipeline.fetch_openzu import USER_AGENT
from app.settings import CACHE_DIR

# Carto Voyager od 2024 vyžaduje API klíč a vrací šedé dlaždice „API KEY REQUIRED“.
# Používáme veřejná OSM zrcadla (vhodný User-Agent je povinný).
TILE_SOURCES = (
    "https://tile.openstreetmap.de/{z}/{x}/{y}.png",
    "https://a.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png",
    "https://b.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png",
    "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
)
TILE_TIMEOUT_S = 20
# v2: invaliduje cache po Carto watermark dlaždicích (cache je jen z/x/y).
_TILE_CACHE_VER = "v2"


class TileError(RuntimeError):
    pass


def tile_cache_path(z: int, x: int, y: int) -> Path:
    return CACHE_DIR / "tiles" / _TILE_CACHE_VER / str(z) / str(x) / f"{y}.png"


def validate_tile(z: int, x: int, y: int) -> None:
    if not (0 <= z <= 18):
        raise TileError("Neplatný zoom dlaždice")
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):
        raise TileError("Dlaždice mimo rozsah")


def _looks_like_blocked_tile(data: bytes) -> bool:
    """Odmítne neplatné PNG (Carto watermark detekujeme hlavně tím, že Carto nepoužíváme)."""
    return len(data) < 50 or data[:8] != b"\x89PNG\r\n\x1a\n"

def fetch_tile(z: int, x: int, y: int) -> Path:
    """Stáhne OSM dlaždici přes NAS (prohlížeč nemluví s CDN)."""
    validate_tile(z, x, y)
    dest = tile_cache_path(z, x, y)
    if dest.exists() and dest.stat().st_size > 50 and not _looks_like_blocked_tile(dest.read_bytes()):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err = "žádný zdroj"
    for template in TILE_SOURCES:
        url = template.format(z=z, x=x, y=y)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "image/png",
                "Referer": "https://podkladarna.kibos.link/",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TILE_TIMEOUT_S) as resp:
                data = resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_err = str(getattr(exc, "reason", exc))
            continue
        if _looks_like_blocked_tile(data):
            last_err = "blokovaná / watermark dlaždice"
            continue
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return dest
    raise TileError(f"Dlaždice se nepodařilo stáhnout: {last_err}")
