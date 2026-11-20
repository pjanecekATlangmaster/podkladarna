"""Ukázkové ZIPy na webu se přegenerují na serveru samy (Petr 2026-11-19).

Seznam ukázek je v ``configs/samples.yaml``. Kolo přegenerování začne po změně minor verze aplikace
(``SAMPLES_REFRESH_ON_VERSION``, na testu B výchozí vypnuto),
když jsou ukázky starší než ``SAMPLES_REFRESH_MAX_AGE_DAYS`` nebo na ruční žádost
(``python -m app.samples_refresh``). Joby se pouštějí po jednom a jen na volném serveru: v nočním okně
``SAMPLES_REFRESH_HOURS`` hned, přes den až po ``SAMPLES_REFRESH_IDLE_MIN`` minutách bez nového jobu.
Uživatelský job, který mezitím přijde, jde do fronty normálně a další ukázka počká.

Job zakládá server sám sobě přes ``POST /api/jobs`` (stejné volby jako z webu) s interním tokenem
(``DATA_ROOT/.samples_token``, jen z loopbacku): job je soukromý, bez e-mailu, mimo limity, statistiky
a kalibraci odhadu (``options["sample"]``). Nový ZIP nahradí starý atomicky jen po úspěšném jobu ve
správném jazyce a u ukázek s ``dtm: true`` s balíky DTM; jinak zůstane stará ukázka a pokus se zopakuje
další den (nejvýš ``MAX_ATTEMPTS`` za kolo).
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
import os
import secrets
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from app import settings

logger = logging.getLogger(__name__)

CONFIG_PATH = settings.CONFIG_DIR / "samples.yaml"
STATE_NAME = ".refresh_state.json"
REQUEST_NAME = ".refresh-now"
TOKEN_NAME = ".samples_token"
MANIFEST_NAME = "samples.json"  # popisky pro web (app.main._sample_labels)
TOKEN_HEADER = "X-Podkladarna-Sample"
CHECK_INTERVAL_S = 300.0
POLL_S = 20.0
MAX_ATTEMPTS = 3
# Jeden pokus za den: nepovedená ukázka (výpadek zdroje) se nezkouší dokola tutéž noc.
RETRY_AFTER_H = 20.0
_FINISHED = {"done", "done_with_errors", "failed"}

_started = False
_run_lock = threading.Lock()


# --- Konfigurace a stav -------------------------------------------------------------------------


def load_samples(path: Path | None = None) -> list[dict[str, Any]]:
    data = yaml.safe_load((path or CONFIG_PATH).read_text(encoding="utf-8")) or {}
    out = []
    for item in data.get("samples") or []:
        if not isinstance(item, dict) or not item.get("file") or not item.get("bbox"):
            continue
        out.append(item)
    return out


def samples_dir() -> Path:
    return Path(settings.SAMPLES_DIR)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_state() -> dict[str, Any]:
    return _read_json(samples_dir() / STATE_NAME)


def write_state(state: dict[str, Any]) -> None:
    _write_json(samples_dir() / STATE_NAME, state)


def minor_version(version: str) -> str:
    return ".".join(str(version or "").split(".")[:2])


def _parse_dt(raw: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def refresh_reason(state: dict[str, Any], *, now: datetime | None = None) -> str | None:
    """Proč začít nové kolo (None = ukázky jsou aktuální)."""
    now = now or datetime.now(timezone.utc)
    if (samples_dir() / REQUEST_NAME).exists():
        return "ruční žádost"
    done_version = str(state.get("version") or "")
    done_at = _parse_dt(state.get("done_at"))
    # Bez změny verze jako důvodu (test B) se první kolo bez stavu spustí až podle stáří (done_at chybí = ihned).
    if settings.SAMPLES_REFRESH_ON_VERSION and minor_version(done_version) != minor_version(settings.APP_VERSION):
        return f"verze {done_version or '–'} → {settings.APP_VERSION}"
    max_age = settings.SAMPLES_REFRESH_MAX_AGE_DAYS
    if max_age > 0 and (done_at is None or now - done_at > timedelta(days=max_age)):
        return f"starší než {max_age} dní"
    return None


def parse_hours(raw: str) -> tuple[int, int] | None:
    """„1-5“ → (1, 5): okno od 1:00 do 5:59 místního času; „22-4“ přes půlnoc; prázdné = bez okna."""
    try:
        start, end = (int(x) % 24 for x in str(raw).split("-", 1))
    except ValueError:
        return None
    return start, end


def in_window(hour: int, window: tuple[int, int] | None) -> bool:
    if window is None:
        return False
    start, end = window
    return start <= hour <= end if start <= end else hour >= start or hour <= end


def _local_hour(now: datetime) -> int:
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo(settings.SAMPLES_REFRESH_TZ)).hour
    except Exception:  # noqa: BLE001 – bez tzdata místní čas kontejneru
        return now.astimezone().hour


def last_user_job_at() -> datetime | None:
    """Čas posledního jobu, který nezaložilo přegenerování ukázek."""
    from app import db

    with db.connect() as conn:
        rows = conn.execute("SELECT created_at, options_json FROM jobs ORDER BY created_at DESC LIMIT 50").fetchall()
    for row in rows:
        try:
            opts = json.loads(row["options_json"] or "{}")
        except ValueError:
            opts = {}
        if not opts.get("sample"):
            return _parse_dt(row["created_at"])
    return None


def server_idle() -> bool:
    from app import worker

    snap = worker.queue_snapshot()
    return not (
        snap["busy"] or snap["draining"] or snap["shared_busy"] or snap["queue_size"]
        or snap["prefetching"] or snap["prefetch_queued"]
    )


def may_run_now(*, now: datetime | None = None, manual: bool = False) -> bool:
    """Volný server; v noci a na ruční žádost hned, přes den až po chvíli bez nového jobu."""
    now = now or datetime.now(timezone.utc)
    if not server_idle():
        return False
    if manual or in_window(_local_hour(now), parse_hours(settings.SAMPLES_REFRESH_HOURS)):
        return True
    idle_min = settings.SAMPLES_REFRESH_IDLE_MIN
    if idle_min <= 0:
        return False
    last = last_user_job_at()
    return last is None or now - last >= timedelta(minutes=idle_min)


# --- Interní token -----------------------------------------------------------------------------


def _token() -> str:
    path = Path(settings.DATA_ROOT) / TOKEN_NAME
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if len(value) < 32:
        value = secrets.token_hex(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return value


def request_is_internal(request: Any) -> bool:
    """Job ukázky: platný interní token a požadavek z loopbacku (token se nikdy nevrací ani neloguje)."""
    got = request.headers.get(TOKEN_HEADER)
    if not got:
        return False
    host = getattr(getattr(request, "client", None), "host", "") or ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host in {"testclient", "localhost"}
    if not loopback:
        return False
    return hmac.compare_digest(got.strip().encode("utf-8"), _token().encode("utf-8"))


# --- Jeden job ukázky -------------------------------------------------------------------------


def _site_host(sample: dict[str, Any]) -> str | None:
    """Doména jazyka ukázky, na které je výřez povolený (jinak None = bez domény, jazyk z ``?lang=``)."""
    from app import countries, i18n

    bbox = tuple(float(x) for x in sample["bbox"])
    for host, site in i18n.sites().items():
        if site.lang != sample.get("lang", "cs"):
            continue
        try:
            countries.check_bbox(bbox, site, None)
        except countries.CountryError:
            continue
        return host
    return None


def _multipart(fields: list[tuple[str, str]]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def form_fields(sample: dict[str, Any]) -> list[tuple[str, str]]:
    fields = [
        ("name", str(sample.get("name") or sample["file"])),
        ("bbox", ",".join(str(float(x)) for x in sample["bbox"])),
        ("map_scale", str(sample.get("map_scale") or 10000)),
        ("dtm", "1" if sample.get("dtm", True) else "0"),
    ]
    if sample.get("map_types"):
        fields.append(("map_types_form", "1"))
        fields += [("map_types", str(t)) for t in sample["map_types"]]
    return fields


def submit_job(sample: dict[str, Any]) -> str:
    """Založí job ukázky přes vlastní API; vrátí id jobu, jinak vyhodí RuntimeError s důvodem."""
    body, ctype = _multipart(form_fields(sample))
    lang = str(sample.get("lang") or "cs")
    url = f"{settings.SAMPLES_REFRESH_URL}/api/jobs?{urllib.parse.urlencode({'lang': lang})}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", ctype)
    req.add_header(TOKEN_HEADER, _token())
    host = _site_host(sample)
    if host:
        req.add_header("Host", host)
    try:
        # Bez proxy z prostředí (HTTP_PROXY by poslal požadavek na sebe sama ven).
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"API nedostupné: {exc}") from exc
    job_id = str(data.get("id") or "")
    if not job_id:
        raise RuntimeError(f"odpověď bez id jobu: {str(data)[:200]}")
    return job_id


def wait_for_job(job_id: str, *, timeout_s: float | None = None, poll_s: float = POLL_S) -> dict[str, Any]:
    from app import db

    timeout_s = timeout_s if timeout_s is not None else (2 * settings.JOB_TIMEOUT_MINUTES + 30) * 60
    deadline = time.monotonic() + timeout_s
    while True:
        job = db.get_job(job_id)
        if job.get("status") in _FINISHED or time.monotonic() > deadline:
            return job
        time.sleep(poll_s)


def check_result(job: dict[str, Any], sample: dict[str, Any]) -> Path:
    """Výstupní ZIP hotového jobu, který smí nahradit ukázku; jinak RuntimeError s důvodem."""
    from app.db import JOBS_DIR

    status = job.get("status")
    if status != "done":
        raise RuntimeError(f"job skončil ve stavu {status}: {job.get('error') or ''}".strip())
    opts = job.get("options") or {}
    want_lang = str(sample.get("lang") or "cs")
    if str(opts.get("lang") or "cs") != want_lang:
        raise RuntimeError(f"job v jazyce {opts.get('lang')}, ukázka chce {want_lang}")
    job_dir = Path(JOBS_DIR) / str(job["id"])
    zip_path = next(
        (p for p in (job_dir / "output" / n for n in ("podkladarna_output.zip", "podkladarna_oom.zip")) if p.is_file()),
        None,
    )
    if zip_path is None:
        raise RuntimeError("job nemá výstupní ZIP")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if zf.testzip() is not None or not any(n.endswith((".omap", ".xmap")) for n in zf.namelist()):
                raise RuntimeError("ZIP je poškozený nebo bez mapy")
    except zipfile.BadZipFile as exc:
        raise RuntimeError("ZIP je poškozený") from exc
    if sample.get("dtm"):
        fences = _read_json(job_dir / "work" / "dtm" / "fences.json")
        if not fences.get("packages"):
            raise RuntimeError("bez dat DTM (výpadek DMVS nebo kraje?)")
    return zip_path


def install(zip_path: Path, sample: dict[str, Any]) -> None:
    """Atomická výměna ZIPu a popisku v ``samples.json`` (ostatní popisky beze změny)."""
    dest_dir = samples_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / str(sample["file"])
    tmp = dest_dir / f".{dest.name}.{uuid.uuid4().hex[:8]}.tmp"
    shutil.copyfile(zip_path, tmp)
    os.replace(tmp, dest)
    manifest_path = dest_dir / MANIFEST_NAME
    manifest = _read_json(manifest_path)
    label = str(sample.get("label") or "").strip()
    if label:
        manifest[dest.name] = label
    else:
        manifest.pop(dest.name, None)
    _write_json(manifest_path, manifest)


def refresh_one(sample: dict[str, Any]) -> str:
    """Celý cyklus jedné ukázky; vrátí id jobu (RuntimeError = ukázka zůstává)."""
    job_id = submit_job(sample)
    logger.info("Ukázka %s: job %s", sample["file"], job_id)
    job = wait_for_job(job_id)
    install(check_result(job, sample), sample)
    return job_id


# --- Kolo přegenerování ------------------------------------------------------------------------


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def next_pending(state: dict[str, Any], samples: list[dict[str, Any]], *, now: datetime) -> dict[str, Any] | None:
    rnd = state.get("round") or {}
    results = rnd.get("results") or {}
    for sample in samples:
        res = results.get(sample["file"]) or {}
        if res.get("ok") or int(res.get("attempts") or 0) >= MAX_ATTEMPTS:
            continue
        last = _parse_dt(res.get("at"))
        if last is not None and now - last < timedelta(hours=RETRY_AFTER_H):
            continue
        return sample
    return None


def round_finished(state: dict[str, Any], samples: list[dict[str, Any]]) -> bool:
    results = (state.get("round") or {}).get("results") or {}
    return all(
        (results.get(s["file"]) or {}).get("ok") or int((results.get(s["file"]) or {}).get("attempts") or 0) >= MAX_ATTEMPTS
        for s in samples
    )


def tick(*, now: datetime | None = None, samples: list[dict[str, Any]] | None = None) -> str | None:
    """Jeden krok plánovače: nanejvýš jedna ukázka. Vrátí jméno zpracované ukázky (None = nic)."""
    if not _run_lock.acquire(blocking=False):
        return None
    try:
        now = now or datetime.now(timezone.utc)
        state = read_state()
        rnd = state.get("round")
        manual = (samples_dir() / REQUEST_NAME).exists()
        if not rnd:
            reason = refresh_reason(state, now=now)
            if reason is None:
                return None
            rnd = {"version": settings.APP_VERSION, "reason": reason, "started": _now_iso(now), "results": {}}
            state["round"] = rnd
            write_state(state)
            logger.info("Ukázky: nové kolo přegenerování (%s)", reason)
        samples = samples if samples is not None else load_samples()
        sample = next_pending(state, samples, now=now)
        if sample is None:
            if round_finished(state, samples):
                _finish_round(state, now=now)
            return None
        if not may_run_now(now=now, manual=manual):
            return None
        res = dict((rnd.get("results") or {}).get(sample["file"]) or {})
        res["attempts"] = int(res.get("attempts") or 0) + 1
        res["at"] = _now_iso(now)
        try:
            res["job"] = refresh_one(sample)
            res["ok"] = True
            res.pop("error", None)
            logger.info("Ukázka %s vyměněna (job %s)", sample["file"], res["job"])
        except Exception as exc:  # noqa: BLE001 – stará ukázka zůstává, důvod do stavu a logu
            res["ok"] = False
            res["error"] = str(exc)[:500]
            logger.warning("Ukázka %s nevyměněna (pokus %s): %s", sample["file"], res["attempts"], exc)
        state = read_state()
        state.setdefault("round", rnd).setdefault("results", {})[sample["file"]] = res
        write_state(state)
        if round_finished(state, samples):
            _finish_round(state)
        return str(sample["file"])
    finally:
        _run_lock.release()


def _finish_round(state: dict[str, Any], *, now: datetime | None = None) -> None:
    rnd = state.pop("round", None) or {}
    results = rnd.get("results") or {}
    failed = sorted(f for f, r in results.items() if not r.get("ok"))
    state.update(version=rnd.get("version") or settings.APP_VERSION, done_at=_now_iso(now), last_round=rnd)
    write_state(state)
    try:
        (samples_dir() / REQUEST_NAME).unlink()
    except OSError:
        pass
    if failed:
        logger.warning("Ukázky: kolo hotové, nevyměněno %s", ", ".join(failed))
    else:
        logger.info("Ukázky: kolo hotové, všechny vyměněny")


def start_scheduler() -> None:
    """Vlákno ve webovém procesu (tam běží fronta jobů); kontrola každých ``CHECK_INTERVAL_S``."""
    global _started
    if _started or not settings.SAMPLES_REFRESH:
        return
    _started = True

    def _loop() -> None:
        while True:
            time.sleep(CHECK_INTERVAL_S)
            try:
                tick()
            except Exception:  # noqa: BLE001 – plánovač nesmí spadnout
                logger.exception("Ukázky: krok plánovače selhal")

    threading.Thread(target=_loop, name="samples-refresh", daemon=True).start()


# --- Příkazová řádka ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.samples_refresh",
        description="Přegenerování ukázkových ZIPů: bez voleb zadá ruční žádost (server ji zpracuje, až nebude počítat).",
    )
    parser.add_argument("--status", action="store_true", help="jen vypsat stav")
    args = parser.parse_args(argv)
    if not args.status:
        samples_dir().mkdir(parents=True, exist_ok=True)
        state = read_state()
        state.pop("round", None)  # nové kolo od začátku (i po vyčerpaných pokusech)
        write_state(state)
        (samples_dir() / REQUEST_NAME).write_text(_now_iso(), encoding="utf-8")
        print("Žádost zadána: server ukázky přegeneruje do několika minut, až nebude počítat žádný job.")
    state = read_state()
    print(json.dumps(state, ensure_ascii=False, indent=2) if state else "Ukázky se zatím na serveru negenerovaly.")
    if not settings.SAMPLES_REFRESH:
        print("Pozor: SAMPLES_REFRESH=0, plánovač neběží.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
