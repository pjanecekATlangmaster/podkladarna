"""Odhad kvality DTM pro výřez ještě před jobem (`GET /api/dtm_coverage`).

DTM (Digitální technická mapa) má balíky ve všech krajích, ale některé obce mají plochy (`ZPS_OPL`) neúplné
(okolí Prahy, Jihlava). Job to pozná až po stažení (`dtm_import.incomplete_opl_units`). Předem stačí velikost
balíků z hlavičky HTTP (HEAD, bez stahování): poměr `ZPS_OPL` / `ZPS_KR` téže obce. Kalibrace 2026-11-14 na 68 obcích
z cache na Petrově PC: neúplné obce Středočeského kraje 0,08–0,29, úplné (Praha, Liberec, Plzeň) 0,65–1,34.

Bez sítě do DMVS (výpadek podle `source_status`) se nic neodhaduje – web o výpadku varuje sám.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from app import settings
from app.pipeline import dtm_sources

# Poměr velikostí balíků OPL / KR: pod SPARSE_RATIO jsou plochy obce řídké (mezera 0,29 – 0,65 v kalibraci).
SPARSE_RATIO = 0.45
HEAD_TIMEOUT_S = 15
MAX_UNITS = 40  # víc obcí ve výřezu nebývá (MTBO 25 km²); nad tím neodhadovat
_SIZES_NAME = "package_sizes.json"
_lock = threading.Lock()


def _sizes_path() -> Path:
    return settings.DOWNLOADS_DIR / "dtm" / _SIZES_NAME


def _load_sizes() -> dict[str, int]:
    try:
        data = json.loads(_sizes_path().read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_sizes(sizes: dict[str, int]) -> None:
    path = _sizes_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sizes, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _head_size(url: str) -> int | None:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": dtm_sources.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HEAD_TIMEOUT_S) as resp:
            size = int(resp.headers.get("Content-Length") or 0)
            return size or None
    except Exception:  # noqa: BLE001 – odhad je jen orientační
        return None


def package_size(pkg: dtm_sources.DtmPackage, sizes: dict[str, int]) -> int | None:
    """Velikost balíku: stažený soubor v cache, uložená velikost, jinak HEAD (název nese datum → neměnná)."""
    local = dtm_sources.package_path(pkg)
    if local.is_file():
        return local.stat().st_size
    if pkg.filename in sizes:
        return sizes[pkg.filename]
    size = _head_size(pkg.url)
    if size:
        sizes[pkg.filename] = size
    return size


def unit_quality(opl: int | None, kr: int | None) -> tuple[str, float | None]:
    """(good | sparse | none | unknown, poměr OPL / KR)."""
    if opl is None and kr is None:
        return "none", None
    if not opl or not kr:
        return ("none", None) if kr and opl is None else ("unknown", None)
    ratio = opl / kr
    return ("sparse" if ratio < SPARSE_RATIO else "good"), round(ratio, 3)


def _dmvs_down() -> bool:
    from app import source_status

    try:
        return any(s.get("needs") == "dtm" and not s.get("ok") for s in source_status.snapshot()["services"])
    except Exception:  # noqa: BLE001
        return False


def coverage_for_bbox(bbox: tuple[float, float, float, float]) -> dict[str, Any]:
    """Obce výřezu a kvalita jejich DTM; ``available`` False při výpadku DMVS nebo chybě."""
    if _dmvs_down():
        return {"available": False, "reason": "dmvs_down", "units": []}
    try:
        units = dtm_sources.units_for_bbox(bbox)
        if not units:
            return {"available": True, "units": [], "level": "none"}
        if len(units) > MAX_UNITS:
            return {"available": False, "reason": "too_many_units", "units": []}
        regions = dtm_sources.fetch_regions()
        kraj_name = next((u.kraj_name for u in units if u.kraj_name), "")
        kraj = dtm_sources.region_code_for_name(kraj_name, regions)
        if not kraj:
            return {"available": True, "units": [], "level": "none"}
        packages = dtm_sources.fetch_url_list(kraj)
        chosen = dtm_sources.latest_packages(packages, units)
    except Exception:  # noqa: BLE001 – při chybě web nic neukáže
        return {"available": False, "reason": "error", "units": []}

    by_unit: dict[tuple[str, int], dict[str, dtm_sources.DtmPackage]] = {}
    for pkg in chosen:
        by_unit.setdefault((pkg.unit_kind, pkg.unit_code), {})[pkg.set_name] = pkg
    have_momc = any(kind == "MOMC" for kind, _ in by_unit)
    with _lock:
        sizes = _load_sizes()
        before = dict(sizes)
        out = []
        for unit in units:
            if unit.kind == "OBEC" and have_momc:
                continue  # Praha, Brno…: data jsou po městských částech
            sets = by_unit.get((unit.kind, unit.code), {})
            opl = package_size(sets[dtm_sources.SET_OPL], sizes) if dtm_sources.SET_OPL in sets else None
            kr = package_size(sets[dtm_sources.SET_KR], sizes) if dtm_sources.SET_KR in sets else None
            quality, ratio = unit_quality(opl, kr)
            out.append({"kind": unit.kind, "code": unit.code, "name": unit.name, "quality": quality, "ratio": ratio})
        if sizes != before:
            _save_sizes(sizes)
    return {"available": True, "units": out, "level": overall_level(out)}


def overall_level(units: list[dict]) -> str:
    """good (vše dobré) | partial (část řídká / bez DTM) | sparse (vše řídké / bez DTM) | none | unknown."""
    qs = [u["quality"] for u in units if u["quality"] != "unknown"]
    if not units:
        return "none"
    if not qs:
        return "unknown"
    weak = [q for q in qs if q in ("sparse", "none")]
    if not weak:
        return "good"
    if len(weak) == len(qs):
        return "none" if all(q == "none" for q in qs) else "sparse"
    return "partial"


_cache: dict[tuple, tuple[float, dict]] = {}
_CACHE_TTL_S = 600


def coverage_cached(bbox: tuple[float, float, float, float]) -> dict[str, Any]:
    """Stejný výřez (tažení úchytu tam a zpět) se 10 min neptá znovu."""
    key = tuple(round(v, 4) for v in bbox)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    result = coverage_for_bbox(bbox)
    if result.get("available"):
        if len(_cache) > 200:
            _cache.clear()
        _cache[key] = (time.monotonic(), result)
    return result
