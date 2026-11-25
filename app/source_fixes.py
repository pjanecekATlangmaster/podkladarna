"""Hlídač schválených oprav zdrojů: server si je sám stáhne z ``master`` (Petr 2026-11-25, pull model).

Na ostrou A se nikdo nepřipojuje. Webový proces každých ``SOURCE_FIXES_INTERVAL_MIN`` minut přečte
z GitHubu (jen čtení, veřejné repo) soubor ``configs/source_fixes.yaml`` z větve ``master``::

    tuning:                 # totéž co ruční tuning.yaml (workers 1–8, disabled, note)
      si_clss:
        workers: 1
        note: "429 od 24. 11."
    patches:                # oprava kódu zdroje = commit, který už je v master
      - sources: [si_clss]
        commit: 0123456789abcdef0123456789abcdef01234567

Schválení opravy = merge PR, který soubor mění. Takový PR merguje jen Petr (CLAUDE.md), review agent
ho nemerguje. Hlídač pak:

- ``tuning`` zapíše do ``source_overrides/tuning_auto.yaml`` (ruční ``tuning.yaml`` má u stejného zdroje
  přednost); platí od dalšího jobu, bez restartu;
- ``patches`` nainstaluje stejnými kontrolami jako ruční ``install`` (commit je předek ``master``, otisk
  git blobu, verze vyšší než image, modul se s image načte, Česko a vlastní data nikdy), jen když neběží
  žádný job; opravu, kterou image už obsahuje (verze ≤ image, třeba na testu B), tiše přeskočí;
- opravu, která ze souboru zmizí, zruší (jen opravy, které sám nainstaloval; ruční zůstávají);
- **vrátí** opravu, když job, který ji použil, selže (kromě selhání na potvrzeném výpadku zdroje,
  ``options.source_outage``) nebo když se modul opravy v jobu nenačte; týž commit už znovu nezkouší;
- o každé změně pošle e-mail na ``FEEDBACK_TO``; stav ukazuje ``GET /api/health`` (pole ``source_fixes``),
  takže denní kontrola zdrojů vidí, co na A opravdu běží.

Vypnutí: smazat řádky ze souboru v ``master`` (hlídač vrátí vše, co nasadil), nebo na serveru
``SOURCE_FIXES_AUTO=0`` v ``data/.env``. Ruční příkaz ``python -m app.source_overrides`` funguje dál;
``python -m app.source_fixes sync`` provede jedno kolo hned, ``status`` vypíše stav.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app import settings, source_overrides as ov

logger = logging.getLogger(__name__)

MANIFEST_PATH = "configs/source_fixes.yaml"
MANIFEST_URL = f"https://raw.githubusercontent.com/{ov.PATCH_REPO}/{ov.PATCH_BRANCH}/{MANIFEST_PATH}"
MANIFEST_MAX_BYTES = 64_000
# Síťová chyba se hlásí e-mailem až po této době bez úspěšného čtení (GitHub občas zakašle).
FETCH_ALERT_AFTER_H = 24
_SHA40 = re.compile(r"^[0-9a-f]{40}$")

_started = False
_lock = threading.Lock()


# --- nastavení a soubory ---------------------------------------------------------------------------------


def enabled() -> bool:
    return os.environ.get("SOURCE_FIXES_AUTO", "1").strip().lower() not in {"0", "false", "off", "no"}


def interval_s() -> float:
    try:
        return max(60.0, float(os.environ.get("SOURCE_FIXES_INTERVAL_MIN", "10")) * 60)
    except ValueError:
        return 600.0


def state_path() -> Path:
    return ov.base_dir() / "auto_state.json"


def read_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- manifest ---------------------------------------------------------------------------------------------


def fetch_manifest() -> str:
    req = urllib.request.Request(MANIFEST_URL, headers={"User-Agent": "podkladarna-source-fixes"})
    with urllib.request.urlopen(req, timeout=ov.HTTP_TIMEOUT_S) as resp:  # noqa: S310 – pevná adresa GitHubu
        data = resp.read(MANIFEST_MAX_BYTES + 1)
    if len(data) > MANIFEST_MAX_BYTES:
        raise ValueError("soubor oprav je příliš velký")
    return data.decode("utf-8")


def parse_manifest(text: str) -> tuple[dict, list[str]]:
    """Text ``source_fixes.yaml`` → ({tuning, patches}, chyby). Při jakékoli chybě se nepoužije nic."""
    import yaml

    try:
        data = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001
        return {}, [f"YAML nejde přečíst: {exc}"]
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return {}, ["soubor musí být slovník s klíči tuning a patches"]
    errors = [f"{k}: neznámý klíč (povolené: tuning, patches)" for k in data if k not in ("tuning", "patches")]
    tuning, tuning_errors = ov.validate_tuning(data.get("tuning"))
    errors += [f"tuning.{e}" for e in tuning_errors]
    patches: dict[str, str] = {}  # zdroj → plné SHA
    raw = data.get("patches") or []
    if not isinstance(raw, list):
        errors.append("patches musí být seznam")
        raw = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) - {"sources", "commit", "note"}:
            errors.append(f"patches[{i}]: povolená pole sources, commit, note")
            continue
        commit = str(item.get("commit") or "").strip().lower()
        sources = item.get("sources")
        if not _SHA40.match(commit):
            errors.append(f"patches[{i}].commit: celé SHA commitu (40 hex znaků)")
        if not isinstance(sources, list) or not sources:
            errors.append(f"patches[{i}].sources: seznam klíčů zdrojů")
            continue
        for s in sources:
            s = str(s)
            if not ov._KEY_RE.match(s) or ov.protected(s):
                errors.append(f"patches[{i}]: {s} – neplatný zdroj (Česko a vlastní data se neopravují)")
            elif s in patches:
                errors.append(f"patches[{i}]: {s} je v souboru dvakrát")
            else:
                patches[s] = commit
    if errors:
        return {}, errors
    return {"tuning": tuning, "patches": patches}, []


# --- vrácení vadné opravy --------------------------------------------------------------------------------


def failed_jobs_with_patch(source: str, commit: str, since: str) -> list[str]:
    """Joby od ``since``, kterým oprava zdroje selhala nebo které s ní skončily chybou (mimo výpadek zdroje)."""
    from app import db

    c10 = commit[:10]
    used = f"%Oprava zdroje {source}: kód z commitu {c10}%"
    broken = f"%Oprava zdroje {source} z commitu {c10} selhala%"
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT j.id, j.status, j.options_json, l.line LIKE ? AS broken
            FROM jobs j JOIN job_logs l ON l.job_id = j.id
            WHERE l.created_at >= ? AND (l.line LIKE ? OR l.line LIKE ?)
            """,
            (broken, since, used, broken),
        ).fetchall()
    out = []
    for job_id, status, options_json, is_broken in rows:
        if is_broken:
            out.append(str(job_id))
            continue
        if status != "failed":
            continue
        try:
            options = json.loads(options_json or "{}")
        except ValueError:
            options = {}
        if not options.get("source_outage"):
            out.append(str(job_id))
    return sorted(set(out))


# --- jedno kolo ------------------------------------------------------------------------------------------


def _auto_entries() -> dict[str, dict]:
    return {str(e.get("source")): e for e in ov.read_manifest()["patches"] if e.get("auto")}


def _mark_auto(sources: list[str]) -> None:
    manifest = ov.read_manifest()
    for e in manifest["patches"]:
        if e.get("source") in sources:
            e["auto"] = True
    ov._write_manifest(manifest)


def _remove_auto(sources: list[str]) -> None:
    manifest = ov.read_manifest()
    manifest["patches"] = [e for e in manifest["patches"] if not (e.get("auto") and e.get("source") in sources)]
    ov._write_manifest(manifest)


def _apply_tuning(tuning: dict, changes: list[str]) -> None:
    path = ov.tuning_auto_path()
    current: dict = {}
    if path.is_file():
        try:
            import yaml

            current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            current = {"_rozbite": True}
    if current == tuning:
        return
    if tuning:
        import yaml

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            "# Zapisuje hlídač app/source_fixes.py z configs/source_fixes.yaml v master – ručně neupravovat,\n"
            "# ruční ladění patří do tuning.yaml.\n" + yaml.safe_dump(tuning, allow_unicode=True, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    else:
        path.unlink(missing_ok=True)
    for key in sorted(set(current) | set(tuning)):
        if current.get(key) != tuning.get(key):
            changes.append(f"Ladění {key}: {json.dumps(tuning.get(key), ensure_ascii=False) if key in tuning else 'zrušeno'}")


def sync(
    *,
    fetch: Callable[[], str] = fetch_manifest,
    gh_fetch=None,
    idle: Callable[[], bool] | None = None,
    notify: Callable[[str, str], None] | None = None,
) -> dict:
    """Jedno kolo hlídače. Vrací nový stav (zapíše ho i do ``auto_state.json``)."""
    notify = notify or _notify
    idle = idle or (lambda: ov.running_jobs() == 0)
    state = read_state()
    state["last_sync"] = _now()
    changes: list[str] = []
    problems: list[str] = []
    try:
        text = fetch()
    except Exception as exc:  # noqa: BLE001 – síť / GitHub; zkusí se v dalším kole
        state["last_error"] = f"soubor oprav nejde stáhnout: {exc}"
        last_ok = state.get("last_fetch_ok")
        stale = not last_ok or (
            datetime.now(timezone.utc) - datetime.fromisoformat(last_ok)
        ).total_seconds() > FETCH_ALERT_AFTER_H * 3600
        if stale and not state.get("fetch_alert_sent"):
            state["fetch_alert_sent"] = True
            notify("Hlídač oprav zdrojů: GitHub nedostupný", f"{state['last_error']}\nPoslední úspěšné čtení: {last_ok or 'nikdy'}.")
        _write_json(state_path(), state)
        return state
    state["last_fetch_ok"] = state["last_sync"]
    state.pop("fetch_alert_sent", None)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    manifest, errors = parse_manifest(text)
    if errors:
        state["last_error"] = "soubor oprav je neplatný: " + "; ".join(errors[:5])
        if state.get("invalid_digest") != digest:
            state["invalid_digest"] = digest
            notify("Hlídač oprav zdrojů: neplatný soubor", state["last_error"] + "\nNic se nezměnilo, platí poslední stav.")
        _write_json(state_path(), state)
        return state
    state.pop("invalid_digest", None)
    state["manifest_sha256"] = digest
    rejected: dict[str, str] = state.setdefault("rejected", {})

    _apply_tuning(manifest["tuning"], changes)

    wanted: dict[str, str] = manifest["patches"]
    auto = _auto_entries()
    # 1) vadné opravy vrátit
    for source, entry in auto.items():
        commit = str(entry.get("commit") or "")
        bad = failed_jobs_with_patch(source, commit, str(entry.get("installed_at") or ""))
        if bad:
            _remove_auto([source])
            rejected[source] = commit
            changes.append(f"VRÁCENO: oprava {source} z commitu {commit[:10]} – selhal job {', '.join(bad[:3])}; jede kód image")
    auto = _auto_entries()
    # 2) zmizelé ze souboru zrušit, nové nainstalovat (jen bez běžícího jobu)
    to_remove = [
        s for s, e in auto.items()
        if s not in wanted or (wanted[s] != e.get("commit") and rejected.get(s) == wanted[s])
    ]
    to_install: dict[str, list[str]] = {}
    for source, commit in wanted.items():
        local = ov.patch_entry(source)
        if local and local.get("commit") == commit:
            continue
        if rejected.get(source) == commit:
            continue
        to_install.setdefault(commit, []).append(source)
    for source in list(rejected):
        if source not in wanted:
            rejected.pop(source)  # řádek zmizel: příští (jiný) záznam se zase smí zkusit
    if to_remove or to_install:
        if not idle():
            state["pending"] = "čeká na doběhnutí jobu"
        else:
            state.pop("pending", None)
            if to_remove:
                _remove_auto(to_remove)
                changes += [f"Zrušena oprava {s} (v souboru oprav už není platná), jede kód image" for s in to_remove]
            for commit, sources in to_install.items():
                try:
                    kwargs = {"force": True}
                    if gh_fetch is not None:
                        kwargs["fetch"] = gh_fetch
                    res = ov.install(sources, commit, **kwargs)
                except SystemExit as exc:
                    msg = str(exc.code if exc.code is not None else exc)
                    if "není potřeba" in msg:  # image už opravu obsahuje (test B, po Promote)
                        state.setdefault("in_image", {}).update({s: commit for s in sources})
                        continue
                    for s in sources:
                        rejected[s] = commit
                    problems.append(f"Oprava {', '.join(sources)} z commitu {commit[:10]} nenainstalována: {msg}")
                    continue
                except Exception as exc:  # noqa: BLE001 – síť: zkusí se v dalším kole
                    problems.append(f"Oprava {', '.join(sources)}: GitHub API nedostupné ({exc}), další pokus za chvíli")
                    continue
                _mark_auto(sources)
                changes.append(
                    f"Nasazena oprava {', '.join(sources)} z commitu {commit[:10]} (verze {res['version']}), platí od dalšího jobu"
                )
    else:
        state.pop("pending", None)

    state["applied"] = {s: str(e.get("commit"))[:10] for s, e in _auto_entries().items()}
    state["tuning"] = sorted(manifest["tuning"])
    state["last_error"] = "; ".join(problems) or None
    reported = [p for p in problems if "GitHub API nedostupné" not in p]
    if changes or reported:
        notify(
            "Hlídač oprav zdrojů: " + ("změna na serveru" if changes else "oprava nenasazena"),
            "\n".join(changes + reported) + f"\n\nImage {settings.APP_VERSION}, soubor {MANIFEST_PATH} v {ov.PATCH_BRANCH}.",
        )
    _write_json(state_path(), state)
    return state


def _notify(subject: str, body: str) -> None:
    instance = settings.INSTANCE_NAME or settings.INSTANCE_CHANNEL
    logger.info("%s: %s", subject, body.replace("\n", " | "))
    to = (settings.FEEDBACK_TO or "").strip()
    if not to:
        return
    try:
        from app.mail import send_mail

        send_mail(to, f"[{instance}] {subject}", body)
    except Exception:  # noqa: BLE001 – bez mailu zůstane záznam v logu a v /api/health
        logger.exception("Hlídač oprav zdrojů: e-mail se nepodařilo poslat")


def run_once() -> dict | None:
    """Kolo pod zámkem napříč procesy (víc workerů uvicornu nesmí instalovat naráz)."""
    lock = ov.base_dir() / ".auto.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
        return sync()


def health() -> dict:
    """Krátký stav pro ``/api/health`` (bez tajemství: zdroj, zkrácený commit, čas)."""
    state = read_state()
    return {
        "auto": enabled(),
        "last_sync": state.get("last_sync"),
        "applied": state.get("applied") or {},
        "tuning": state.get("tuning") or [],
        "pending": state.get("pending"),
        "error": state.get("last_error"),
    }


def start_scheduler() -> None:
    global _started
    with _lock:
        if _started or not enabled():
            return
        _started = True

    def _loop() -> None:
        time.sleep(min(120.0, interval_s()))
        while True:
            try:
                run_once()
            except Exception:  # noqa: BLE001 – hlídač nesmí spadnout
                logger.exception("Hlídač oprav zdrojů: kolo selhalo")
            time.sleep(interval_s())

    threading.Thread(target=_loop, name="source-fixes", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.source_fixes", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="stav hlídače")
    sub.add_parser("sync", help="jedno kolo hned")
    sub.add_parser("check", help="ověří soubor configs/source_fixes.yaml v pracovní kopii (exit 1 při chybě)")
    args = parser.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps({**health(), "state": read_state()}, ensure_ascii=False, indent=2))
    elif args.cmd == "sync":
        res = run_once()
        print(json.dumps(res, ensure_ascii=False, indent=2) if res is not None else "Kolo už běží v jiném procesu.")
    elif args.cmd == "check":
        path = Path(__file__).resolve().parent.parent / MANIFEST_PATH
        _, errors = parse_manifest(path.read_text(encoding="utf-8"))
        print("\n".join(errors) if errors else f"{MANIFEST_PATH} je v pořádku")
        return 1 if errors else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
