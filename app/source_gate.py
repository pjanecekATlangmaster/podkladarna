"""Odmítnutí jobu při potvrzeném výpadku zdroje výšek (Petr 2026-11-20).

Při zadání jobu (``POST /api/jobs``) se ověří jen zdroje, bez kterých se mapa nevytvoří a které nemají
zálohu: výšky dané země mimo Česko (``source_status`` sondy s ``fatal`` a rolí výšek – GUGiK NMT/NMPT,
BEV ALS, Metsäkeskus, Hesensko DGM1/DOM1 …). OSM (Overpass) kritický není, má zrcadla a startovní sonda
se u něj mýlila (4.0.5). Česko je beze změny: LiDAR ČÚZK má cache listů a předstahování mimo frontu
a sonda na pevný list PRAH77 neříká nic o listech výřezu; DTM má zálohu (kresba z OSM a ZABAGED).

Postup: čerstvý uložený stav (``source_status.json``), který zdroj vidí v provozu, stačí bez dotazu.
Jinak jde na zdroj jeden malý dotaz z ``source_status`` (vzorek GetCoverage / HEAD dlaždice) a po
selhání po krátké pauze druhý (``check_probe``). Odmítá se až po dvou selháních za sebou. Výsledek
se drží v procesu webu: běží ``SOURCE_GATE_TTL_MIN`` (5 min), výpadek kratší dobu (``DOWN_TTL_S``),
ať se po obnovení zdroje job brzy zase přijme; souběžná kliknutí sdílejí jeden dotaz.

Odmítnutý job nevznikne (``db.create_job`` se nevolá), takže se nezapočítá do limitů e-mailu, IP,
sítě /24 ani fronty – ty se počítají z jobů v databázi.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from app import countries, i18n, settings, source_status

logger = logging.getLogger(__name__)

# Výpadek se drží kratší dobu než „běží“, ať se zdroj po obnovení brzy zase pustí.
DOWN_TTL_S = 120.0

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="source-gate")
_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}
_inflight: dict[str, Future] = {}


def enabled() -> bool:
    return bool(settings.SOURCE_GATE) and ttl_s() > 0


def ttl_s() -> float:
    return max(0.0, float(settings.SOURCE_GATE_TTL_MIN)) * 60


def critical_probes(country_code: str | None) -> list[source_status.Probe]:
    """Zdroje výšek země bez zálohy (Česko žádné – viz docstring modulu)."""
    from app.pipeline.job_progress import SOURCE_ELEVATION

    code = (country_code or countries.DEFAULT_COUNTRY).strip().upper()
    if code == countries.DEFAULT_COUNTRY:
        return []
    return [
        p
        for p in source_status.probes_for(code)
        if p.fatal and source_status.probe_role(p.key) == SOURCE_ELEVATION
    ]


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _cached(key: str) -> dict | None:
    with _lock:
        hit = _cache.get(key)
    if hit is None:
        return None
    ts, result = hit
    limit = ttl_s() if result.get("ok") else min(ttl_s(), DOWN_TTL_S)
    return result if time.monotonic() - ts < limit else None


def _fresh_status_ok(key: str) -> bool:
    """Uložený stav kontroly zdrojů (web / job) mladší než TTL brány, který zdroj vidí v provozu."""
    state = source_status._load()
    age = source_status._age_s(state)
    if age is None or age >= ttl_s():
        return False
    svc = state["services"].get(key)
    return bool(svc and svc.get("ok"))


def _check(probe: source_status.Probe) -> dict:
    try:
        result = source_status.check_probe(probe)  # dva pokusy s pauzou PROBE_RETRY_WAIT_S
    except Exception:  # noqa: BLE001 – chyba sondy job neblokuje
        logger.warning("Kontrola zdroje %s při zadání jobu selhala", probe.key, exc_info=True)
        result = {"key": probe.key, "ok": True, "detail": None}
    with _lock:
        _cache[probe.key] = (time.monotonic(), result)
    return result


def _status(probe: source_status.Probe) -> dict:
    hit = _cached(probe.key)
    if hit is not None:
        return hit
    if _fresh_status_ok(probe.key):
        return {"key": probe.key, "ok": True, "detail": None}
    with _lock:
        fut = _inflight.get(probe.key)
        if fut is None:
            fut = _pool.submit(_check, probe)
            _inflight[probe.key] = fut

            def _done(_f: Future, key=probe.key) -> None:
                with _lock:
                    if _inflight.get(key) is _f:
                        del _inflight[key]

            fut.add_done_callback(_done)
    try:
        return fut.result()
    except Exception:  # noqa: BLE001
        return {"key": probe.key, "ok": True, "detail": None}


def _label(probe: source_status.Probe) -> Any:
    key = f"api.source.{probe.key}.label"
    return i18n.Msg(key) if key in i18n.catalog("en") and key in i18n.catalog("cs") else probe.label


def check(country_code: str | None, *, has_fallback: bool = False) -> dict | None:
    """Potvrzený výpadek kritického zdroje → ``{key, label, detail, planned, message}``, jinak None.

    ``has_fallback``: job má výšky jinak (vlastní data zadavatele, převzatý LAZ předchozího jobu).
    """
    if has_fallback or not enabled():
        return None
    for probe in critical_probes(country_code):
        result = _status(probe)
        if result.get("ok"):
            continue
        planned = bool(result.get("planned"))
        label = _label(probe)
        msg = i18n.Msg("api.job.source_planned" if planned else "api.job.source_down", source=label)
        logger.warning(
            "Job odmítnut: zdroj %s (%s) neodpovídá – %s", probe.key, country_code, result.get("detail")
        )
        return {
            "key": probe.key,
            "label": probe.label,
            "country": (country_code or "").upper(),
            "detail": result.get("detail"),
            "planned": planned,
            "message": msg,
        }
    return None
