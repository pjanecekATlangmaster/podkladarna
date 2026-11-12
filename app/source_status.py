"""Kontrola dostupnosti zdrojů dat (ČÚZK, DTM, OSM, Sentinel-2) pro web.

Jednou za ``SOURCE_CHECK_INTERVAL_MIN`` (výchozí 10 min) se na každý zdroj pošle
jeden lehký dotaz (HEAD na soubor, popis služby ``?f=json``, malý JSON) – žádné
stahování dat, služby se nezatěžují. Výsledek drží proces v paměti; web ho čte
z ``GET /api/source_status`` a před spuštěním jobu varuje, že mapa nebude úplná.

Zdroj je „mimo provoz“, když neodpoví (timeout, síť) nebo vrátí HTTP 5xx; po
chybě se dotaz jednou zopakuje, ať krátký výpadek nevyvolá planý poplach.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from app.pipeline.dtm_sources import DMVS_API
from app.pipeline.fetch_openzu import OPENZU_DMPOK, OPENZU_DMR, USER_AGENT
from app.pipeline.osm_paths import OVERPASS_URLS
from app.pipeline.sentinel2 import STAC_SEARCH_URL

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 15
PROBE_RETRY_WAIT_S = 3.0
# Overpass: stačí, když odpoví některé z prvních zrcadel (pipeline zkouší další).
OSM_PROBE_MIRRORS = 3
# List SM5 v Praze, který na OpenZU určitě je (HEAD bez stahování).
LIDAR_PROBE_SHEET = "PRAH77"


@dataclass(frozen=True)
class Probe:
    key: str
    label: str
    urls: tuple[str, ...]
    impact: str
    method: str = "GET"
    # fatal = bez zdroje job nejspíš selže; jinak jen chybí část kresby / podkladu.
    fatal: bool = False
    # Volba formuláře, bez které se zdroj nepoužije (web podle ní varování skryje).
    needs: str | None = None
    # Vedle 2xx/3xx bere jako „běží“ i 4xx (Overpass bez dotazu vrací 400).
    any_4xx_ok: bool = False


PROBES: tuple[Probe, ...] = (
    Probe(
        "lidar",
        "LiDAR ČÚZK (DMR 5G, DMP OK)",
        (OPENZU_DMR.format(mapnom=LIDAR_PROBE_SHEET), OPENZU_DMPOK.format(mapnom=LIDAR_PROBE_SHEET)),
        "Bez LiDARu se mapa nevytvoří (pomůže jen list, který už je v cache).",
        method="HEAD",
        fatal=True,
    ),
    Probe(
        "dtm",
        "DTM krajů (DMVS ČÚZK)",
        (f"{DMVS_API}/is-dtmk/info",),
        "Mapa bude nejspíš bez DTM (pomůže jen balík, který už je v cache): budovy, ploty, "
        "chodníky a šířky cest jen z OSM a ZABAGED.",
        needs="dtm",
    ),
    Probe(
        "osm",
        "OpenStreetMap (Overpass)",
        OVERPASS_URLS[:OSM_PROBE_MIRRORS],
        "Bez OSM se nestáhne základ kresby (cesty, budovy, objekty); job nejspíš selže.",
        fatal=True,
        any_4xx_ok=True,
    ),
    Probe(
        "zabaged",
        "ZABAGED ČÚZK",
        ("https://ags.cuzk.gov.cz/arcgis/rest/services/ZABAGED_POLOHOPIS/MapServer?f=json",),
        "Mapa bude bez doplňků ze ZABAGED (vodstvo, areály, srázy, kříže …).",
    ),
    Probe(
        "ruian",
        "RÚIAN ČÚZK (obce, parcely)",
        ("https://ags.cuzk.gov.cz/arcgis/rest/services/RUIAN/MapServer/12?f=json",),
        "Bez RÚIAN se nenajdou balíky DTM ani parcely ulic (sprint).",
    ),
    Probe(
        "ortofoto",
        "Ortofoto ČÚZK",
        ("https://ags.cuzk.gov.cz/arcgis1/rest/services/ORTOFOTO/MapServer?f=json",),
        "Bez ortofota chybí stromy na dlažbě, herní prvky a ulice z parcel (sprint), "
        "louky v lese a šablona ortofota.",
    ),
    Probe(
        "katastr",
        "Katastrální mapa ČÚZK",
        ("https://services.cuzk.cz/dxf/ku/",),
        "Šablona katastrální mapy bude chybět.",
        method="HEAD",
    ),
    Probe(
        "sentinel",
        "Sentinel-2 (AWS)",
        (STAC_SEARCH_URL.rsplit("/", 1)[0] + "/",),
        "Šablona se snímky Sentinel-2 bude chybět.",
    ),
)

_lock = threading.Lock()
_state: dict = {"checked_at": None, "services": {}}
_started = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _probe_url(url: str, method: str) -> tuple[int | None, str | None]:
    """HTTP kód (nebo None) a krátký popis chyby sítě."""
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            if method != "HEAD":
                resp.read(64 * 1024)
            return resp.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception as exc:  # timeout, DNS, TLS, odmítnuté spojení
        reason = getattr(exc, "reason", None) or exc
        return None, f"{type(exc).__name__}: {reason}"[:200]


def _is_up(code: int | None, probe: Probe) -> bool:
    if code is None:
        return False
    if 200 <= code < 400:
        return True
    return probe.any_4xx_ok and 400 <= code < 500


def check_probe(probe: Probe) -> dict:
    """Jeden zdroj: běží, když odpoví aspoň jedna URL (po chybě jeden opakovaný pokus)."""
    t0 = time.monotonic()
    code: int | None = None
    error: str | None = None
    ok = False
    for attempt in range(2):
        for url in probe.urls:
            code, error = _probe_url(url, probe.method)
            if _is_up(code, probe):
                ok = True
                break
        if ok or attempt:
            break
        time.sleep(PROBE_RETRY_WAIT_S)
    detail = None
    if not ok:
        detail = f"HTTP {code}" if code is not None else (error or "neodpovídá")
    return {
        "key": probe.key,
        "label": probe.label,
        "ok": ok,
        "detail": detail,
        "impact": probe.impact,
        "fatal": probe.fatal,
        "needs": probe.needs,
        "latency_ms": int((time.monotonic() - t0) * 1000),
    }


def check_all() -> dict:
    """Zkontroluje všechny zdroje souběžně a uloží výsledek; vrací snímek stavu."""
    with ThreadPoolExecutor(max_workers=len(PROBES), thread_name_prefix="src-check") as pool:
        results = list(pool.map(check_probe, PROBES))
    now = _now_iso()
    with _lock:
        prev = _state["services"]
        services = {}
        for r in results:
            old = prev.get(r["key"]) or {}
            if r["ok"]:
                r["down_since"] = None
            else:
                r["down_since"] = old.get("down_since") if not old.get("ok", True) else now
            services[r["key"]] = r
        _state["services"] = services
        _state["checked_at"] = now
    down = [r["label"] for r in results if not r["ok"]]
    if down:
        logger.warning("Zdroje dat mimo provoz: %s", ", ".join(down))
    return snapshot()


def snapshot() -> dict:
    """Poslední známý stav pro API (``services`` v pořadí ``PROBES``)."""
    with _lock:
        services = [dict(_state["services"][p.key]) for p in PROBES if p.key in _state["services"]]
        return {"checked_at": _state["checked_at"], "services": services}


def down_services(*, use_dtm: bool = True) -> list[dict]:
    """Zdroje mimo provoz podle posledního měření (pro log jobu)."""
    return [
        s
        for s in snapshot()["services"]
        if not s["ok"] and (use_dtm or s.get("needs") != "dtm")
    ]


def start_scheduler(interval_min: float) -> None:
    """Vlákno na pozadí: první kontrola hned po startu, pak jednou za ``interval_min``."""
    global _started
    if _started or interval_min <= 0:
        return
    _started = True
    wait_s = max(interval_min, 1.0) * 60

    def _loop() -> None:
        while True:
            try:
                check_all()
            except Exception:
                logger.exception("Kontrola zdrojů dat selhala")
            time.sleep(wait_s)

    threading.Thread(target=_loop, name="source-status", daemon=True).start()
