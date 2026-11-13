"""Kontrola dostupnosti zdrojů dat (ČÚZK, DTM, OSM, Sentinel-2) pro web.

Kontrola běží líně (Petr 2026-11-12): až když někdo otevře stránku (``GET /api/source_status``)
nebo začne job, a jen když je poslední výsledek starší než ``SOURCE_CHECK_TTL_MIN`` (výchozí 3 min).
Na každý zdroj jde jeden lehký dotaz (HEAD na soubor, popis služby ``?f=json``, malý JSON) –
žádné stahování dat; víc návštěvníků naráz spustí nejvýš jednu kontrolu. Výsledek se ukládá
do ``DATA_ROOT/source_status.json``, takže ho vidí i proces jobu.

Zdroj je „mimo provoz“, když neodpoví (timeout, síť) nebo vrátí HTTP 5xx; po
chybě se dotaz jednou zopakuje, ať krátký výpadek nevyvolá planý poplach. Odpověď 5xx s textem
„odstávka“ (DMVS 2026-11: „Plánovaná odstávka“) je plánovaná odstávka (``planned``).

Trvá-li výpadek nepřetržitě aspoň ``SOURCE_ALERT_AFTER_MIN`` (od prvního zjištěného selhání
``down_since``), pošle se provozovateli (``FEEDBACK_TO``) jeden e-mail a po obnovení druhý.
Měří se líně při kontrolách (žádný běh na pozadí); proti dvojímu mailu z webu a jobu
chrání značka v ``DATA_ROOT/source_alerts/`` založená výhradně (``O_EXCL``).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from app import settings

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
# Text odpovědi 5xx, podle kterého se výpadek pozná jako plánovaná odstávka.
PLANNED_OUTAGE_MARKERS = ("odstávk",)
_ALERT_DIR = "source_alerts"
_ALERT_KEEP_DAYS = 60


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

_STATUS_FILE = "source_status.json"
_refresh_lock = threading.Lock()  # jedna kontrola naráz v procesu webu


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _status_path() -> Path:
    return settings.DATA_ROOT / _STATUS_FILE


def _load() -> dict:
    try:
        data = json.loads(_status_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("services"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"checked_at": None, "services": {}}


def _save(state: dict) -> None:
    path = _status_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.warning("Stav zdrojů dat nejde uložit do %s", path, exc_info=True)


def _age_s(state: dict) -> float | None:
    try:
        checked = datetime.fromisoformat(state["checked_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - checked).total_seconds()


def ttl_s() -> float:
    return max(0.0, float(settings.SOURCE_CHECK_TTL_MIN)) * 60


def _probe_url(url: str, method: str) -> tuple[int | None, str | None, str]:
    """HTTP kód (nebo None), krátký popis chyby sítě a začátek textu chybové odpovědi."""
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            if method != "HEAD":
                resp.read(64 * 1024)
            return resp.status, None, ""
    except urllib.error.HTTPError as exc:
        text = ""
        try:
            text = exc.read(2048).decode("utf-8", "replace")
        except Exception:
            pass
        return exc.code, None, text
    except Exception as exc:  # timeout, DNS, TLS, odmítnuté spojení
        reason = getattr(exc, "reason", None) or exc
        return None, f"{type(exc).__name__}: {reason}"[:200], ""


def _is_planned(code: int | None, text: str) -> bool:
    low = (text or "").lower()
    return code is not None and code >= 500 and any(m in low for m in PLANNED_OUTAGE_MARKERS)


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
    planned = False
    for attempt in range(2):
        planned = False
        for url in probe.urls:
            code, error, text = _probe_url(url, probe.method)
            if _is_up(code, probe):
                ok = True
                break
            planned = planned or _is_planned(code, text)
        if ok or attempt:
            break
        time.sleep(PROBE_RETRY_WAIT_S)
    detail = None
    if not ok:
        detail = f"HTTP {code}" if code is not None else (error or "neodpovídá")
        if planned:
            detail = f"plánovaná odstávka, {detail}"
    return {
        "key": probe.key,
        "label": probe.label,
        "ok": ok,
        "planned": planned and not ok,
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
    prev = _load()["services"]
    services = {}
    for r in results:
        old = prev.get(r["key"]) or {}
        if r["ok"]:
            r["down_since"] = None
            r["alerted"] = False
        else:
            r["down_since"] = (old.get("down_since") if not old.get("ok", True) else None) or now
            r["alerted"] = bool(old.get("alerted")) and r["down_since"] == old.get("down_since")
        services[r["key"]] = r
    alerts = _due_alerts(prev, services)
    _save({"checked_at": now, "services": services})
    down = [r["label"] for r in results if not r["ok"]]
    if down:
        logger.warning("Zdroje dat mimo provoz: %s", ", ".join(down))
    if alerts:
        _send_alerts(alerts)
    return snapshot()


def alert_after_s() -> float:
    return max(0.0, float(settings.SOURCE_ALERT_AFTER_MIN)) * 60


def _since_s(iso: str | None) -> float | None:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return None


def _claim(name: str) -> bool:
    """Značka odeslání (jednou napříč procesy webu a jobů); False = už ji má někdo jiný."""
    folder = settings.DATA_ROOT / _ALERT_DIR
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(folder / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        logger.warning("Značku upozornění na výpadek nejde založit", exc_info=True)
        return False


def _prune_claims() -> None:
    folder = settings.DATA_ROOT / _ALERT_DIR
    limit = time.time() - _ALERT_KEEP_DAYS * 86400
    try:
        for f in folder.iterdir():
            if f.stat().st_mtime < limit:
                f.unlink()
    except OSError:
        pass


def _claim_name(key: str, down_since: str, kind: str) -> str:
    stamp = "".join(ch for ch in down_since if ch.isalnum())
    return f"{key}-{stamp}.{kind}"


def _due_alerts(prev: dict, services: dict) -> list[tuple[str, dict]]:
    """Upozornění k odeslání: ("down", služba) po prahu výpadku, ("up", služba) po obnovení.

    Mail o obnovení jen tehdy, když odešel mail o výpadku (krátké výpadky ticho)."""
    after = alert_after_s()
    if after <= 0:
        return []
    out: list[tuple[str, dict]] = []
    for key, r in services.items():
        old = prev.get(key) or {}
        if not r["ok"] and not r["alerted"]:
            age = _since_s(r["down_since"])
            if age is not None and age >= after:
                r["alerted"] = True  # i když značku má jiný proces: mail už odešel
                if _claim(_claim_name(key, r["down_since"], "down")):
                    out.append(("down", r))
        elif r["ok"] and old.get("alerted") and old.get("down_since"):
            if _claim(_claim_name(key, old["down_since"], "up")):
                out.append(("up", {**r, "down_since": old["down_since"]}))
    return out


def _cs_time(iso: str | None) -> str:
    try:
        dt = datetime.fromisoformat(iso).astimezone(ZoneInfo("Europe/Prague"))
    except Exception:
        return str(iso or "?")
    return f"{dt.day}. {dt.month}. {dt.year} {dt:%H:%M}"


def _duration(iso: str | None) -> str:
    sec = _since_s(iso)
    if sec is None:
        return "?"
    minutes = int(sec // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def build_alert_email(kind: str, svc: dict) -> tuple[str, str]:
    """Předmět a text e-mailu provozovateli o výpadku / obnovení zdroje."""
    label = svc.get("label") or svc.get("key")
    since = _cs_time(svc.get("down_since"))
    base = (settings.PUBLIC_BASE_URL or "").strip()
    note = (
        "Kontrola běží jen při otevření webu nebo startu jobu, čas je první zjištěné selhání.\n"
        + (f"Stav zdrojů: {base.rstrip('/')}/\n" if base else "")
    )
    if kind == "down":
        what = "má plánovanou odstávku" if svc.get("planned") else "je mimo provoz"
        subject = f"Podkladárna: zdroj {label} {what}"
        body = (
            f"Zdroj dat {label} {what} nepřetržitě od {since} ({_duration(svc.get('down_since'))}).\n"
            f"Odpověď: {svc.get('detail') or '?'}\n"
            f"Dopad na mapy: {svc.get('impact') or '?'}\n\n"
            "Až zdroj znovu odpoví, přijde jeden e-mail o obnovení.\n" + note
        )
    else:
        subject = f"Podkladárna: zdroj {label} znovu běží"
        body = (
            f"Zdroj dat {label} znovu odpovídá (výpadek od {since}, celkem {_duration(svc.get('down_since'))}).\n\n"
            + note
        )
    return subject, body


def _send_alerts(alerts: list[tuple[str, dict]]) -> None:
    to = (settings.FEEDBACK_TO or "").strip()
    if not to:
        return
    from app.mail import send_mail  # líně: modul čte SMTP nastavení

    prefix = "[test] " if settings.INSTANCE_CHANNEL == "test" else ""
    for kind, svc in alerts:
        subject, body = build_alert_email(kind, svc)
        subject = prefix + subject
        try:
            send_mail(to, subject, body)
            logger.info("Upozornění na zdroj %s (%s) odesláno", svc.get("key"), kind)
        except Exception:
            logger.warning("Upozornění na zdroj %s (%s) nejde odeslat", svc.get("key"), kind, exc_info=True)
    _prune_claims()


def snapshot() -> dict:
    """Poslední uložený stav pro API (``services`` v pořadí ``PROBES``)."""
    state = _load()
    services = [dict(state["services"][p.key]) for p in PROBES if p.key in state["services"]]
    return {"checked_at": state.get("checked_at"), "services": services}


def is_stale() -> bool:
    age = _age_s(_load())
    return age is None or age >= ttl_s()


def refresh_if_stale() -> bool:
    """Web: je-li výsledek starší než TTL, spustí kontrolu na pozadí (nejvýš jednu naráz).

    Vrací True, když kontrola právě běží (web se za chvíli zeptá znovu)."""
    if ttl_s() <= 0:
        return False
    if not _refresh_lock.acquire(blocking=False):
        return True
    if not is_stale():
        _refresh_lock.release()
        return False

    def _run() -> None:
        try:
            check_all()
        except Exception:
            logger.exception("Kontrola zdrojů dat selhala")
        finally:
            _refresh_lock.release()

    try:
        threading.Thread(target=_run, name="source-status", daemon=True).start()
    except Exception:
        _refresh_lock.release()
        raise
    return True


def down_services(*, use_dtm: bool = True) -> list[dict]:
    """Job: zdroje mimo provoz; starý výsledek nejdřív ověří znovu (pár sekund)."""
    if ttl_s() <= 0:
        return []
    if is_stale():
        check_all()
    return [
        s
        for s in snapshot()["services"]
        if not s["ok"] and (use_dtm or s.get("needs") != "dtm")
    ]
