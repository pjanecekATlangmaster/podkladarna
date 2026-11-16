"""Sonda zdrojů pro konkrétní výřez ještě před jobem (Petr 2026-11-15).

Sjednocuje kontroly, které dřív stály každá zvlášť: pokrytí výškami u zdrojů, které nejsou celoplošné
(``elevation_raster.elevation_coverage``, FR LiDAR HD), podíl výřezu s technickou mapou (``technical_map_area`` /
``technical_map_gaps`` v ``configs/countries.yaml``) a v Česku odhad kvality DTM obcí z velikosti balíků
(``app.dtm_coverage``). Stav služeb (``app.source_status``) sem nepatří – ten je globální a web i job ho ukazují samy.

Sondy jsou dotazy omezené na výřez (index dlaždic, HEAD balíků), ne GetCapabilities. Běží souběžně s celkovým
limitem: co nestihne, je „neznámé“ a nic neblokuje; dotaz doběhne na pozadí a naplní cache, takže další volání
(POST /api/jobs po /api/sheets) má výsledek hned. Kresbu sonda nemění: OSM je základ, technická mapa a DTM
doplňují, kde jsou; sonda jen předem varuje a výřez bez výšek odmítne.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any, Callable

from app import countries, i18n

# Limit sondy pro web (/api/sheets); POST /api/jobs čeká kratší dobu (obvykle má výsledek z cache).
DEADLINE_S = 8.0
JOB_DEADLINE_S = 3.0
# Pod tímto podílem se varuje, že část výřezu zdroj nepokrývá (pod COVERAGE_MIN_SHARE se výřez odmítá).
PARTIAL_SHARE = 0.95

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="area-probe")
_inflight: dict[tuple, Future] = {}
_lock = threading.Lock()


def _shared(key: tuple, fn: Callable[[], Any]) -> Future:
    """Jedna běžící sonda na klíč: souběžné dotazy (GUI tahá výřez, /api/sheets i /api/dtm_coverage) ji sdílejí."""
    with _lock:
        fut = _inflight.get(key)
        if fut is not None:
            return fut
        fut = _pool.submit(fn)
        _inflight[key] = fut

    def _done(_f: Future, key=key) -> None:
        with _lock:
            if _inflight.get(key) is _f:
                del _inflight[key]

    fut.add_done_callback(_done)
    return fut


def _box_key(bbox) -> tuple:
    return tuple(round(float(v), 5) for v in bbox)


def dtm_coverage_future(bbox) -> Future:
    from app import dtm_coverage

    box = tuple(float(v) for v in bbox)
    return _shared(("dtm", _box_key(box)), lambda: dtm_coverage.coverage_cached(box))


def dtm_coverage(bbox, timeout: float | None = None) -> dict[str, Any]:
    """Odhad kvality DTM (``/api/dtm_coverage``) přes sdílenou sondu; po limitu ``available`` False."""
    try:
        return dtm_coverage_future(bbox).result(timeout=timeout)
    except Exception:  # noqa: BLE001 – timeout i chyba: web nic neukáže
        return {"available": False, "reason": "timeout", "units": []}


def technical_map_share(country: countries.Country, bbox) -> float:
    """Podíl výřezu s technickou mapou podle obrysů v countries.yaml (bez sítě)."""
    if not country.has_technical_map:
        return 0.0
    box = tuple(float(v) for v in bbox)
    share = 1.0
    if country.technical_map_area:
        share = countries.rings_share(box, country.technical_map_area)
    if country.technical_map_gaps:
        share = max(0.0, share - countries.rings_share(box, country.technical_map_gaps))
    return share


def technical_map_label(key: str) -> str:
    from app.pipeline.technical_maps import FOREIGN

    return "DTM" if key == "cz_dtm" else (FOREIGN[key].short if key in FOREIGN else key)


def probe(
    country: countries.Country, bbox, *, deadline_s: float = DEADLINE_S, include_dtm: bool = True
) -> dict[str, Any]:
    """Sonda výřezu → ``{verdict, elevation, technical_map, warnings, block}``.

    ``verdict``: ``ok`` | ``warn`` (část zdrojů výřez nepokrývá, kreslí se z OSM) | ``block`` (výšky pro většinu
    výřezu chybí). ``block`` je hláška ``i18n.Msg`` pro odmítnutí, ``warnings`` seznam ``i18n.Msg``.
    ``include_dtm`` False: bez kvality DTM obcí (/api/sheets – GUI se na ni ptá souběžně přes /api/dtm_coverage).
    """
    from app.pipeline import elevation_raster

    box = tuple(float(v) for v in bbox)
    t0 = time.monotonic()
    futures: dict[str, Future] = {}
    if country.elevation in elevation_raster.COVERAGE:
        src = country.elevation
        futures["elevation"] = _shared(
            ("elev", src, _box_key(box)), lambda: elevation_raster.elevation_coverage(src, box)
        )
    tech_key = country.technical_map if country.has_technical_map else ""
    if include_dtm and tech_key == "cz_dtm" and country.code == "CZ":
        futures["dtm"] = dtm_coverage_future(box)
    if futures:
        wait(list(futures.values()), timeout=deadline_s)

    def _result(name: str):
        fut = futures.get(name)
        if fut is None or not fut.done():
            return None
        try:
            return fut.result()
        except Exception:  # noqa: BLE001 – sonda je jen pojistka
            return None

    warnings: list[i18n.Msg] = []
    block: i18n.Msg | None = None

    # Výšky: celoplošný zdroj (CZ, NL, CH…) = 1,0; nestihnuto / bez odpovědi = None (neblokovat).
    elev_share: float | None = 1.0
    elev_status = "full"
    if "elevation" in futures:
        elev_share = _result("elevation")
        elev_status = "unknown" if elev_share is None else "checked"
    elif country.code != countries.DEFAULT_COUNTRY:
        # Celoplošný národní zdroj (NL, CH, AT…) končí na hranici země: přesah výřezu za ni je bez terénu
        # a porostu. Česko beze změny (mimo polygon tam varuje kontrola listů LiDARu).
        inside = countries.bbox_share(box, country)
        if inside < PARTIAL_SHARE:
            elev_share, elev_status = inside, "checked"
    # Popisek zdroje v jazyce stránky (vnořená ``Msg``); česky = ``SOURCE_LABELS`` (log jobu beze změny).
    elev_label = (
        i18n.Msg(f"api.elevation_source.{country.elevation}")
        if country.elevation in elevation_raster.SOURCE_LABELS
        else country.elevation
    )
    limit = elevation_raster.TILE_LIMITS.get(country.elevation)
    if limit is not None:
        try:
            n_tiles, max_tiles = limit(box)
        except Exception:  # noqa: BLE001 – sonda je jen pojistka
            n_tiles, max_tiles = 0, 1
        if n_tiles > max_tiles:
            block = i18n.Msg("api.sheets.too_many_tiles", source=elev_label, tiles=n_tiles, max=max_tiles)
    if block is None and elev_share is not None and elev_status == "checked":
        pct = int(round(elev_share * 100))
        if elev_share < elevation_raster.COVERAGE_MIN_SHARE:
            block = i18n.Msg("api.sheets.no_elevation", source=elev_label, share=pct)
        elif elev_share < PARTIAL_SHARE:
            warnings.append(i18n.Msg("api.probe.elevation_partial", source=elev_label, share=pct))

    # Technická mapa: obrysy z countries.yaml; v Česku navíc kvalita DTM obcí (živě z velikosti balíků).
    tech: dict[str, Any] = {"key": tech_key or "none", "share": None}
    if tech_key:
        label = technical_map_label(tech_key)
        share = technical_map_share(country, box)
        tech["share"] = round(share, 2)
        if share <= 0.0:
            warnings.append(i18n.Msg("api.probe.techmap_none", source=label))
        elif share < PARTIAL_SHARE:
            warnings.append(i18n.Msg("api.probe.techmap_partial", source=label, share=int(round(share * 100))))
        if "dtm" in futures:
            cov = _result("dtm")
            if cov and cov.get("available"):
                level = cov.get("level")
                tech["level"] = level
                weak = [u["name"] for u in cov.get("units") or [] if u.get("quality") in ("sparse", "none")]
                if level in ("partial", "sparse", "none") and weak:
                    warnings.append(i18n.Msg("api.probe.dtm_sparse", names=", ".join(weak)))
            else:
                tech["level"] = "unknown"

    verdict = "block" if block is not None else ("warn" if warnings else "ok")
    return {
        "verdict": verdict,
        "elevation": {
            "source": country.elevation,
            "share": None if elev_share is None else round(elev_share, 2),
            "status": elev_status,
        },
        "technical_map": tech,
        "warnings": warnings,
        "block": block,
        "seconds": round(time.monotonic() - t0, 1),
    }


def job_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Výsledek sondy do voleb jobu (JSON, česky – log jobu zůstává česky)."""
    return {
        "verdict": result.get("verdict"),
        "elevation_share": (result.get("elevation") or {}).get("share"),
        "technical_map": result.get("technical_map"),
        "warnings": [i18n.localize(w, "cs") for w in result.get("warnings") or []],
    }


def log_lines(summary: dict[str, Any] | None) -> list[str]:
    """Řádky logu jobu z uložené sondy (bez sondy nic)."""
    if not isinstance(summary, dict) or not summary.get("verdict"):
        return []
    warnings = [str(w) for w in summary.get("warnings") or [] if w]
    if not warnings:
        return ["Sonda výřezu: zdroje pokrývají celý výřez."]
    return [f"Sonda výřezu: {w}" for w in warnings]
