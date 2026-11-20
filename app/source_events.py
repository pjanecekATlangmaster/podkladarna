"""Deník problémů zdrojů dat a souhrnný e-mail provozovateli (Petr 2026-11-20).

Každá kontrola zdrojů (``source_status.check_all``) zapíše do ``DATA_ROOT/source_events.jsonl``
jeden řádek za zdroj, který neodpověděl (``down``), a jeden při obnovení (``up``). Řádek obsahuje
jen údaje o zdroji (klíč, název, země, HTTP kód, krátký popis chyby, druh problému) – žádné
e-maily, IP adresy ani údaje o jobech.

Druh problému (``kind``):

* ``persistent`` – odpověď 4xx (kromě 408 / 429) nebo chyba certifikátu: spíš změna URL nebo
  služby, tedy oprava u nás;
* ``planned`` – plánovaná odstávka (5xx s textem „odstávka“);
* ``transient`` – 5xx, timeout, síť: výpadek zdroje, který nezmůžeme.

Souhrn (``summary``) seskupí události podle zdroje: počet, první a poslední výskyt, poslední
odpověď a stav (běží / mimo provoz / vypadá trvale). Jednou za ``SOURCE_DIGEST_HOURS`` (výchozí 24,
test B 0 = nic) odejde na ``FEEDBACK_TO`` jeden souhrnný e-mail, jen když je co hlásit. Odesílá ho
líně kontrola zdrojů nebo smyčka úklidu (``cleanup``), dvojímu mailu z více procesů brání značka
založená výhradně (``O_EXCL``).

Deník čte provozovatel (a Claude) přes ``GET /api/source_events`` s hlavičkou
``Authorization: Bearer <SOURCE_EVENTS_TOKEN>``; bez nastaveného tokenu endpoint neexistuje (404).
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import settings

logger = logging.getLogger(__name__)

_EVENTS_FILE = "source_events.jsonl"
_DIGEST_FILE = "source_digest.json"
_CLAIM_DIR = "source_alerts"
_write_lock = threading.Lock()

# Zdroj mimo provoz nepřetržitě aspoň tolik hodin vypadá trvale i bez kódu 4xx.
PERSISTENT_AFTER_H = 24
# HTTP 4xx, které jsou přechodné (timeout požadavku, příliš mnoho dotazů).
TRANSIENT_4XX = (408, 429)
# Chyby sítě, které znamenají spíš chybu konfigurace než výpadek.
PERSISTENT_ERRORS = ("CERTIFICATE_VERIFY_FAILED", "SSLCertVerificationError", "certificate verify failed")
MAX_EVENTS_RETURNED = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None


def events_path() -> Path:
    return settings.DATA_ROOT / _EVENTS_FILE


def classify(code: int | None, error: str | None, planned: bool) -> str:
    """Druh problému: ``planned``, ``persistent`` (spíš naše věc) nebo ``transient`` (výpadek zdroje)."""
    if planned:
        return "planned"
    if code is not None and 400 <= code < 500 and code not in TRANSIENT_4XX:
        return "persistent"
    if code is None and error and any(m in error for m in PERSISTENT_ERRORS):
        return "persistent"
    return "transient"


def record(results: list[dict], prev: dict, now: str | None = None) -> None:
    """Zapíše výsledek kontroly: ``down`` za každý nedostupný zdroj, ``up`` při obnovení."""
    ts = now or _now().isoformat()
    lines = []
    for r in results:
        old = prev.get(r["key"]) or {}
        if r.get("ok"):
            if old and not old.get("ok", True):
                lines.append({"ts": ts, "event": "up", "key": r["key"], "label": r.get("label"),
                              "countries": r.get("countries") or [], "down_since": old.get("down_since")})
            continue
        lines.append({
            "ts": ts,
            "event": "down",
            "key": r["key"],
            "label": r.get("label"),
            "countries": r.get("countries") or [],
            "code": r.get("code"),
            "detail": r.get("detail"),
            "kind": classify(r.get("code"), r.get("detail"), bool(r.get("planned"))),
            "fatal": bool(r.get("fatal")),
        })
    if not lines:
        return
    path = events_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines)
        with _write_lock, open(path, "a", encoding="utf-8") as f:
            f.write(payload)  # jeden zápis v režimu append: řádky z více procesů se neprolnou
    except OSError:
        logger.warning("Deník zdrojů nejde zapsat do %s", path, exc_info=True)


def read_events(since: datetime | None = None) -> list[dict]:
    """Události od ``since`` (vadné řádky přeskočí)."""
    out: list[dict] = []
    try:
        with open(events_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                ts = _parse(ev.get("ts")) if isinstance(ev, dict) else None
                if ts is None or (since is not None and ts < since):
                    continue
                out.append(ev)
    except OSError:
        pass
    return out


def prune(keep_days: int | None = None) -> None:
    """Smaže z deníku události starší než ``SOURCE_EVENTS_KEEP_DAYS``."""
    days = settings.SOURCE_EVENTS_KEEP_DAYS if keep_days is None else keep_days
    if days <= 0:
        return
    path = events_path()
    if not path.is_file():
        return
    keep = read_events(_now() - timedelta(days=days))
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with _write_lock:
            tmp.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in keep), encoding="utf-8")
            os.replace(tmp, path)
    except OSError:
        logger.warning("Deník zdrojů nejde zkrátit", exc_info=True)


def summary(events: list[dict], services: dict | None = None) -> list[dict]:
    """Skupiny podle zdroje, nejdřív trvalé a nejčastější.

    ``services`` = aktuální stav ze ``source_status.json`` (běží / mimo provoz od)."""
    services = services or {}
    groups: dict[str, dict] = {}
    for ev in events:
        if ev.get("event") != "down":
            continue
        g = groups.setdefault(ev["key"], {
            "key": ev["key"], "label": ev.get("label"), "countries": ev.get("countries") or [],
            "count": 0, "first": ev["ts"], "last": ev["ts"], "kinds": {}, "fatal": bool(ev.get("fatal")),
        })
        g["count"] += 1
        g["first"] = min(g["first"], ev["ts"])
        g["last"] = max(g["last"], ev["ts"])
        kind = ev.get("kind") or "transient"
        g["kinds"][kind] = g["kinds"].get(kind, 0) + 1
        g["last_detail"] = ev.get("detail")
        g["last_kind"] = kind
    now = _now()
    for key, g in groups.items():
        svc = services.get(key) or {}
        g["ok_now"] = bool(svc.get("ok")) if svc else None
        g["down_since"] = None if svc.get("ok", True) else svc.get("down_since")
        since = _parse(g["down_since"])
        long_down = since is not None and now - since >= timedelta(hours=PERSISTENT_AFTER_H)
        if g["ok_now"]:
            g["state"] = "ok"
        elif g["last_kind"] == "persistent" or long_down:
            g["state"] = "persistent"
        elif g["last_kind"] == "planned":
            g["state"] = "planned"
        else:
            g["state"] = "down"
    order = {"persistent": 0, "down": 1, "planned": 2, "ok": 3}
    return sorted(groups.values(), key=lambda g: (order[g["state"]], -g["count"], g["key"]))


# --- souhrnný e-mail ---------------------------------------------------------

_STATE_TEXT = {
    "persistent": "VYPADÁ TRVALE (nejspíš změna u zdroje, oprava u nás)",
    "down": "mimo provoz",
    "planned": "plánovaná odstávka",
    "ok": "teď běží",
}


def _cs_time(iso: str | None) -> str:
    dt = _parse(iso)
    if dt is None:
        return "?"
    dt = dt.astimezone(ZoneInfo("Europe/Prague"))
    return f"{dt.day}. {dt.month}. {dt:%H:%M}"


def build_digest_email(groups: list[dict], since: datetime) -> tuple[str, str]:
    """Předmět a text souhrnu problémů zdrojů od ``since``."""
    bad = [g for g in groups if g["state"] != "ok"]
    subject = f"Podkladárna: souhrn zdrojů dat – {len(groups)} se problémy"
    if bad:
        subject += f", {len(bad)} stále mimo provoz"
    lines = [f"Problémy zdrojů dat od {_cs_time(since.isoformat())} (počet neúspěšných kontrol).", ""]
    for g in groups:
        where = ", ".join(g["countries"]) or "?"
        kinds = ", ".join(f"{k} {n}×" for k, n in sorted(g["kinds"].items()))
        lines.append(f"- {g['label'] or g['key']} [{where}]: {_STATE_TEXT[g['state']]}")
        lines.append(
            f"  {g['count']}× ({kinds}), první {_cs_time(g['first'])}, poslední {_cs_time(g['last'])}, "
            f"odpověď: {g.get('last_detail') or '?'}"
            + (f", mimo provoz od {_cs_time(g['down_since'])}" if g.get("down_since") else "")
        )
    base = (settings.PUBLIC_BASE_URL or "").strip()
    lines += [
        "",
        "persistent = odpověď 4xx nebo certifikát (spíš změna URL / služby, oprava u nás); "
        "transient = 5xx, timeout, síť (výpadek zdroje).",
        "Kontrola běží jen při otevření webu nebo startu jobu. Další souhrn nejdřív za "
        f"{settings.SOURCE_DIGEST_HOURS:g} h, jen když bude co hlásit.",
    ]
    if base:
        lines.append(f"Stav zdrojů: {base.rstrip('/')}/")
    return subject, "\n".join(lines) + "\n"


def _digest_state_path() -> Path:
    return settings.DATA_ROOT / _DIGEST_FILE


def _load_digest_state() -> dict:
    try:
        data = json.loads(_digest_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_digest_state(state: dict) -> None:
    path = _digest_state_path()
    try:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.warning("Stav souhrnu zdrojů nejde uložit", exc_info=True)


def _claim(name: str) -> bool:
    folder = settings.DATA_ROOT / _CLAIM_DIR
    try:
        folder.mkdir(parents=True, exist_ok=True)
        os.close(os.open(folder / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False
    except OSError:
        logger.warning("Značku souhrnu zdrojů nejde založit", exc_info=True)
        return False


def maybe_send_digest(services: dict | None = None) -> bool:
    """Pošle souhrn, když od posledního uplynulo ``SOURCE_DIGEST_HOURS`` a je co hlásit; True = odeslán."""
    hours = float(settings.SOURCE_DIGEST_HOURS or 0)
    if hours <= 0:
        return False
    now = _now()
    state = _load_digest_state()
    last = _parse(state.get("last"))
    if last is None:
        # První běh: období začíná teď (deník zpětně nehlásí).
        _save_digest_state({"last": now.isoformat()})
        return False
    if now - last < timedelta(hours=hours):
        return False
    stamp = "".join(ch for ch in last.isoformat() if ch.isalnum())
    if not _claim(f"digest-{stamp}"):
        return False
    _save_digest_state({"last": now.isoformat()})
    if services is None:
        services = _current_services()
    groups = summary(read_events(last), services)
    sent = False
    to = (settings.FEEDBACK_TO or "").strip()
    if groups and to:
        from app.mail import send_mail  # líně: modul čte SMTP nastavení

        subject, body = build_digest_email(groups, last)
        if settings.INSTANCE_CHANNEL == "test":
            subject = "[test] " + subject
        try:
            send_mail(to, subject, body)
            sent = True
            logger.info("Souhrn zdrojů dat odeslán (%d zdrojů)", len(groups))
        except Exception:
            logger.warning("Souhrn zdrojů dat nejde odeslat", exc_info=True)
    prune()
    return sent


def _current_services() -> dict:
    try:
        data = json.loads((settings.DATA_ROOT / "source_status.json").read_text(encoding="utf-8"))
        return data.get("services") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def report(days: float = 7) -> dict:
    """Obsah pro ``GET /api/source_events``: souhrn a události za posledních ``days`` dní."""
    days = float(days) if math.isfinite(float(days)) else 7.0
    days = min(max(days, 0.01), float(max(settings.SOURCE_EVENTS_KEEP_DAYS, 1)))
    since = _now() - timedelta(days=days)
    events = read_events(since)
    services = _current_services()
    return {
        "generated_at": _now().isoformat(),
        "app_version": settings.APP_VERSION,
        "instance": settings.INSTANCE_CHANNEL,
        "since": since.isoformat(),
        "summary": summary(events, services),
        "events": events[-MAX_EVENTS_RETURNED:],
        "events_total": len(events),
    }
