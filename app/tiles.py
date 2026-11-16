from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

from app import settings
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


def is_cached(z: int, x: int, y: int) -> bool:
    """Dlaždice je v cache (``fetch_tile`` ji vrátí bez stahování)."""
    dest = tile_cache_path(z, x, y)
    try:
        return dest.is_file() and dest.stat().st_size > 50
    except OSError:
        return False


# Limit stahování dlaždic mimo cache (revize 2026-11-16): okno 1 min v paměti procesu, na IP a celkem.
_FETCH_WINDOW_S = 60.0
_ALL = "*"
_fetch_lock = threading.Lock()
_fetch_hits: dict[str, deque[float]] = {}


def reset_fetch_limits() -> None:
    """Jen pro testy – vyprázdní limity stahování dlaždic."""
    with _fetch_lock:
        _fetch_hits.clear()


def allow_fetch(client_ip: str) -> bool:
    """Smí server pro tuto IP stáhnout další dlaždici mimo cache? (``TILE_FETCH_PER_IP_MIN``, ``TILE_FETCH_PER_MIN``)"""
    from app.rate_limit import is_exempt

    if is_exempt(client_ip):
        return True
    now = time.monotonic()
    limits = (
        ((client_ip or "unknown").strip() or "unknown", settings.TILE_FETCH_PER_IP_MIN),
        (_ALL, settings.TILE_FETCH_PER_MIN),
    )
    with _fetch_lock:
        if len(_fetch_hits) > 10_000:  # mnoho IP: zahodit prošlá okna
            for key in [k for k, q in _fetch_hits.items() if not q or now - q[-1] > _FETCH_WINDOW_S]:
                del _fetch_hits[key]
        for key, limit in limits:
            q = _fetch_hits.setdefault(key, deque())
            while q and now - q[0] > _FETCH_WINDOW_S:
                q.popleft()
            if limit > 0 and len(q) >= limit:
                return False
        for key, _limit in limits:
            _fetch_hits[key].append(now)
    return True


def prune_tile_cache(*, now: float | None = None, log=None) -> int:
    """Úklid cache dlaždic (revize 2026-11-16, dřív se nemazala nikdy): starší verze cache, dlaždice starší
    než ``TILE_CACHE_MAX_AGE_DAYS`` a nad ``TILE_CACHE_MAX_GB`` nejstarší; vrací uvolněné bajty.
    Smazaná dlaždice se při dalším zobrazení mapy stáhne znovu."""
    from app.disk_space import _entry_bytes, _remove

    root = CACHE_DIR / "tiles"
    current = root / _TILE_CACHE_VER
    if not root.is_dir():
        return 0
    now = time.time() if now is None else now
    freed = 0
    removed = 0
    for child in list(root.iterdir()):
        if child.name != _TILE_CACHE_VER:  # v1 apod.: nic je nečte
            freed += _entry_bytes(child)
            _remove(child)
            removed += 1
    files: list[tuple[float, int, Path]] = []
    for dirpath, _dirs, names in os.walk(current):
        for name in names:
            path = Path(dirpath) / name
            try:
                st = path.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, path))
    files.sort()
    max_age = settings.TILE_CACHE_MAX_AGE_DAYS * 86400
    cap = int(settings.TILE_CACHE_MAX_GB * 1e9)
    total = sum(size for _m, size, _p in files)
    for mtime, size, path in files:
        too_old = max_age > 0 and now - mtime > max_age
        over_cap = cap > 0 and total > cap
        if not too_old and not over_cap:
            break  # od nejstarší: dál jsou jen mladší a cache je pod stropem
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        freed += size
        removed += 1
    for dirpath, _dirs, _names in os.walk(current, topdown=False):
        # Prázdné složky z/x; čerstvou ne (fetch_tile ji mohl právě založit).
        try:
            if Path(dirpath) != current and now - os.stat(dirpath).st_mtime > 3600:
                os.rmdir(dirpath)  # neprázdná složka zůstane (OSError)
        except OSError:
            pass
    if removed and log:
        log(f"Cache dlaždic: smazáno {removed} položek ({freed / 1e6:.0f} MB)")
    return freed


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
