"""Rychlá oprava zdrojů výšek na serveru bez nového image (Petr 2026-11-25, varianta B).

Dvě věci v ``DATA_ROOT/source_overrides/`` (na ostré A ``/opt/podkladarna/data/source_overrides/``):

1. **Ladicí soubor** ``tuning.yaml`` – jen povolené klíče pro zdroj výšek (klíč ``elevation`` z ``countries.yaml``)::

       si_clss:
         workers: 1          # souběžná stahování (1–8), jinak hodnota z kódu
         disabled: false     # true = zdroj dočasně vypnutý: POST /api/jobs job odmítne, job ho nespustí
         note: "429 od 24. 11."

   Neplatný soubor se ignoruje celý (varování v logu jobu, ``check`` vrátí chybu). Čte se při každém použití
   podle mtime, takže změna platí bez restartu pro další job.

2. **Oprava kódu zdroje** (``patches/manifest.json`` + ``patches/<sha256>.py``): přesná kopie
   ``app/pipeline/elevation_raster.py`` z commitu, který už je v ``master`` (prošel review a mergem).
   Instaluje ji jen ``python -m app.source_overrides install --source <klíč> --commit <sha>``: ověří přes
   GitHub API, že commit je předkem ``master``, otisk souboru proti git blobu, verzi (oprava musí být novější
   než image) a že se modul s image načte. Job pak pro uvedené zdroje vezme provider z opravy; ostatní země
   i Česko jedou dál kódem image (modul opravy se v jejich jobu ani nenačte). Kontrola otisku proběhne
   i při každém načtení, při neshodě nebo chybě importu job jede kódem image a zapíše varování.
   Jakmile image dosáhne verze opravy (Promote), oprava se sama přestane používat.

Každý job běží ve vlastním procesu (``worker._popen_job``), takže změna se projeví až dalšímu jobu a restart
není potřeba. ``install`` / ``remove`` při běžícím jobu odmítnou (``--force`` přeskočí). Česko (``cz_*``,
země s ``cz_sources``) ani vlastní data (``local:``, nahrané výšky) se ladit ani opravovat nedají.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app import settings

PATCH_REPO = "pjanecekATlangmaster/podkladarna"
PATCH_BRANCH = "master"
# Moduly, které jde opravit, a jejich registr providerů (``PROVIDERS``).
PATCH_MODULE = "app/pipeline/elevation_raster.py"
# Jména, která modul opravy přebírá z image, aby výjimky a výsledek byly tytéž třídy jako u volajícího.
SHARED_NAMES = ("ElevationUnavailable", "SourceRasters")
WORKERS_MAX = 8
TUNING_FIELDS: dict[str, type] = {"workers": int, "disabled": bool, "note": str}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_VERSION_RE = re.compile(r'^APP_VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"', re.M)
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
HTTP_TIMEOUT_S = 30.0
PATCH_MAX_BYTES = 2_000_000

_lock = threading.Lock()
_tuning_cache: dict[str, Any] = {}


# --- cesty a ochrana Česka ------------------------------------------------------------------------------


def base_dir() -> Path:
    return Path(settings.DATA_ROOT) / "source_overrides"


def tuning_path() -> Path:
    return base_dir() / "tuning.yaml"


def patches_dir() -> Path:
    return base_dir() / "patches"


def manifest_path() -> Path:
    return patches_dir() / "manifest.json"


def protected(source: str) -> bool:
    """Česko a vlastní data: ladění ani oprava se jich netýká (český build se nemění)."""
    if not source or source.startswith(("cz_", "local:")) or source == "user_upload":
        return True
    try:
        from app import countries

        return any(c.cz_sources and c.elevation == source for c in countries.load_countries().values())
    except Exception:  # noqa: BLE001 – bez seznamu zemí raději chránit jen podle předpony
        return False


def version_tuple(text: str) -> tuple[int, ...]:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", (text or "").strip())
    return tuple(int(v) for v in m.groups()) if m else (0, 0, 0)


# --- ladicí soubor ---------------------------------------------------------------------------------------


def validate_tuning(data: Any) -> tuple[dict[str, dict], list[str]]:
    """Obsah ``tuning.yaml`` → (platné položky, chyby). Při jakékoli chybě se nepoužije nic."""
    if data is None:
        return {}, []
    if not isinstance(data, dict):
        return {}, ["soubor musí být slovník zdroj → nastavení"]
    out: dict[str, dict] = {}
    errors: list[str] = []
    for key, entry in data.items():
        key = str(key)
        if not _KEY_RE.match(key):
            errors.append(f"{key}: neplatný klíč zdroje")
            continue
        if protected(key):
            errors.append(f"{key}: Česko a vlastní data se neladí")
            continue
        if not isinstance(entry, dict):
            errors.append(f"{key}: nastavení musí být slovník")
            continue
        clean: dict[str, Any] = {}
        for field, value in entry.items():
            kind = TUNING_FIELDS.get(str(field))
            if kind is None:
                errors.append(f"{key}.{field}: neznámé pole (povolená: {', '.join(TUNING_FIELDS)})")
            elif kind is int and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= WORKERS_MAX):
                errors.append(f"{key}.{field}: celé číslo 1–{WORKERS_MAX}")
            elif kind is str and isinstance(value, (int, float)) and not isinstance(value, bool):
                clean[str(field)] = str(value)  # note: 429 bez uvozovek
            elif kind is not int and not isinstance(value, kind):
                errors.append(f"{key}.{field}: musí být {kind.__name__}")
            else:
                clean[str(field)] = value
        out[key] = clean
    return ({} if errors else out), errors


def load_tuning() -> tuple[dict[str, dict], list[str]]:
    """Platné položky ladicího souboru (cache podle mtime) a chyby."""
    path = tuning_path()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}, []
    with _lock:
        if _tuning_cache.get("stamp") == (str(path), stamp):
            return _tuning_cache["entries"], _tuning_cache["errors"]
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        entries, errors = validate_tuning(data)
    except Exception as exc:  # noqa: BLE001 – rozbitý YAML = nic nepoužít
        entries, errors = {}, [f"soubor nejde přečíst: {exc}"]
    with _lock:
        _tuning_cache.update(stamp=(str(path), stamp), entries=entries, errors=errors)
    return entries, errors


def tuning_for(source: str) -> dict:
    if protected(source):
        return {}
    return load_tuning()[0].get(source, {})


def workers(source: str, default: int) -> int:
    """Souběžnost stahování zdroje: z ladicího souboru, jinak ``default`` z kódu."""
    value = tuning_for(source).get("workers")
    return int(value) if value else default


def off_note(source: str) -> str | None:
    """Zdroj vypnutý ladicím souborem → poznámka (prázdný text, když chybí), jinak None."""
    entry = tuning_for(source)
    return str(entry.get("note") or "") if entry.get("disabled") else None


def tuning_log_lines(source: str) -> list[str]:
    entries, errors = load_tuning()
    if errors:
        return [f"Ladění zdrojů: soubor tuning.yaml je neplatný, nepoužit ({'; '.join(errors[:3])})"]
    entry = entries.get(source) if not protected(source) else None
    if entry and entry.get("workers"):
        return [f"Ladění zdrojů: {source} – souběžnost {entry['workers']} (tuning.yaml)"]
    return []


# --- opravy kódu -----------------------------------------------------------------------------------------


def read_manifest() -> dict:
    try:
        data = json.loads(manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"patches": []}
    if not isinstance(data, dict) or not isinstance(data.get("patches"), list):
        return {"patches": []}
    return data


def patch_entry(source: str) -> dict | None:
    for entry in read_manifest()["patches"]:
        if isinstance(entry, dict) and entry.get("source") == source:
            return entry
    return None


def patch_state(entry: dict) -> tuple[bool, str]:
    """(použít, důvod) – bez načtení modulu."""
    source = str(entry.get("source") or "")
    if protected(source):
        return False, "Česko a vlastní data se neopravují"
    if entry.get("module") != PATCH_MODULE:
        return False, f"nepodporovaný modul {entry.get('module')}"
    if version_tuple(str(entry.get("version"))) <= version_tuple(settings.APP_VERSION):
        return False, f"image {settings.APP_VERSION} už opravu obsahuje (oprava {entry.get('version')})"
    path = patches_dir() / f"{entry.get('sha256')}.py"
    if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256") or "")) or not path.is_file():
        return False, "soubor opravy chybí"
    return True, "aktivní"


def _check_compatible(mod, base_mod) -> None:
    for name in SHARED_NAMES:
        new, old = getattr(mod, name, None), getattr(base_mod, name, None)
        if new is None or old is None:
            raise RuntimeError(f"chybí {name}")
        if is_dataclass(old) and [f.name for f in fields(new)] != [f.name for f in fields(old)]:
            raise RuntimeError(f"{name} má v opravě jiná pole než v image")


def load_patch_module(entry: dict, base_mod):
    """Načte modul opravy (otisk se ověří vždy) a převezme do něj sdílené třídy z image."""
    digest = str(entry["sha256"])
    path = patches_dir() / f"{digest}.py"
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError("otisk souboru opravy nesedí s manifestem")
    name = f"_source_patch_{digest[:16]}"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError("modul opravy nejde načíst")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod  # dataclass / from __future__ potřebují modul v sys.modules
        try:
            spec.loader.exec_module(mod)
            _check_compatible(mod, base_mod)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        for shared in SHARED_NAMES:
            setattr(mod, shared, getattr(base_mod, shared))
    return mod


def patched_provider(source: str, base_mod, log: Callable | None = None):
    """Provider z opravy pro ``source``, nebo None (žádná, neaktivní nebo vadná oprava → kód image)."""
    entry = patch_entry(source)
    if entry is None:
        return None
    use, reason = patch_state(entry)
    commit = str(entry.get("commit") or "")[:10]
    if not use:
        if log:
            log(f"Oprava zdroje {source} z commitu {commit} nepoužita: {reason}")
        return None
    try:
        mod = load_patch_module(entry, base_mod)
        provider = getattr(mod, "PROVIDERS", {}).get(source)
        if provider is None:
            raise RuntimeError(f"oprava neobsahuje zdroj {source}")
    except Exception as exc:  # noqa: BLE001 – vadná oprava nesmí shodit job, jede se kódem image
        if log:
            log(f"⚠ Oprava zdroje {source} z commitu {commit} selhala ({exc}), jede kód image {settings.APP_VERSION}")
        return None
    if log:
        log(
            f"Oprava zdroje {source}: kód z commitu {commit} (verze {entry.get('version')}, "
            f"nainstalováno {str(entry.get('installed_at') or '')[:10]}) místo image {settings.APP_VERSION}"
        )
    return provider


# --- instalace (CLI na serveru) --------------------------------------------------------------------------


def _github_json(path: str) -> Any:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{PATCH_REPO}/{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "podkladarna-source-overrides"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310 – pevná adresa GitHub API
        return json.loads(resp.read(PATCH_MAX_BYTES * 2))


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 – identita git blobu


def _file_at(commit: str, path: str, fetch) -> bytes:
    meta = fetch(f"contents/{path}?ref={commit}")
    if meta.get("encoding") != "base64" or int(meta.get("size") or 0) > PATCH_MAX_BYTES:
        raise SystemExit(f"{path}: GitHub nevrátil obsah souboru (příliš velký?)")
    data = base64.b64decode(meta.get("content") or "")
    if git_blob_sha(data) != meta.get("sha"):
        raise SystemExit(f"{path}: otisk staženého souboru nesedí s gitem")
    return data


def running_jobs() -> int:
    from app import db

    try:
        with db.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'running'").fetchone()[0])
    except Exception:  # noqa: BLE001 – bez DB raději předpokládat, že něco běží
        return 1


def _ensure_idle(force: bool) -> None:
    n = running_jobs()
    if n and not force:
        raise SystemExit(f"Běží {n} job(y) – počkejte na busy:false, nebo spusťte s --force.")


def _write_manifest(data: dict) -> None:
    patches_dir().mkdir(parents=True, exist_ok=True)
    path = manifest_path()
    if path.is_file():
        (patches_dir() / "manifest.prev.json").write_bytes(path.read_bytes())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def install(sources: list[str], commit: str, *, force: bool = False, fetch=_github_json, base_mod=None) -> dict:
    commit = commit.strip().lower()
    if not _SHA_RE.match(commit):
        raise SystemExit("--commit: zadejte SHA commitu (7–40 hex znaků)")
    if base_mod is None:
        from app.pipeline import elevation_raster as base_mod
    for source in sources:
        if protected(source):
            raise SystemExit(f"{source}: Česko a vlastní data se neopravují")
        if source not in base_mod.PROVIDERS:
            raise SystemExit(f"{source}: neznámý zdroj výšek (v image není)")
    _ensure_idle(force)
    full = str(fetch(f"commits/{commit}").get("sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", full):
        raise SystemExit(f"commit {commit} na GitHubu není")
    status = fetch(f"compare/{full}...{PATCH_BRANCH}").get("status")
    if status not in ("ahead", "identical"):
        raise SystemExit(f"commit {full[:10]} není v {PATCH_BRANCH} (stav {status}) – opravu nejdřív zrevidovat a mergnout")
    m = _VERSION_RE.search(_file_at(full, "app/settings.py", fetch).decode("utf-8"))
    if not m:
        raise SystemExit("v commitu chybí APP_VERSION")
    version = ".".join(m.groups())
    if version_tuple(version) <= version_tuple(settings.APP_VERSION):
        raise SystemExit(f"commit má verzi {version}, image {settings.APP_VERSION} je stejná nebo novější – oprava není potřeba")
    data = _file_at(full, PATCH_MODULE, fetch)
    digest = hashlib.sha256(data).hexdigest()
    patches_dir().mkdir(parents=True, exist_ok=True)
    target = patches_dir() / f"{digest}.py"
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = [
        {"source": s, "module": PATCH_MODULE, "commit": full, "version": version, "sha256": digest, "installed_at": now}
        for s in sources
    ]
    try:
        mod = load_patch_module(new[0], base_mod)
        missing = [s for s in sources if s not in getattr(mod, "PROVIDERS", {})]
        if missing:
            raise RuntimeError(f"oprava neobsahuje zdroj {', '.join(missing)}")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"oprava se s image {settings.APP_VERSION} nenačte: {exc}") from exc
    manifest = read_manifest()
    manifest["patches"] = [e for e in manifest["patches"] if e.get("source") not in sources] + new
    _write_manifest(manifest)
    return {"sources": sources, "commit": full, "version": version, "sha256": digest}


def remove(sources: list[str] | None, *, force: bool = False) -> list[str]:
    """Zruší opravy uvedených zdrojů (None = všechny) → job zase jede kódem image."""
    _ensure_idle(force)
    manifest = read_manifest()
    gone = [e.get("source") for e in manifest["patches"] if sources is None or e.get("source") in sources]
    manifest["patches"] = [e for e in manifest["patches"] if e.get("source") not in gone]
    _write_manifest(manifest)
    return [str(s) for s in gone]


def status_lines() -> list[str]:
    lines = [f"Image: {settings.APP_VERSION}", f"Složka: {base_dir()}"]
    entries, errors = load_tuning()
    if not tuning_path().is_file():
        lines.append("Ladění (tuning.yaml): není")
    elif errors:
        lines.append("Ladění (tuning.yaml): NEPLATNÉ, nepoužívá se")
        lines += [f"  - {e}" for e in errors]
    else:
        lines.append("Ladění (tuning.yaml): platné")
        lines += [f"  {k}: {json.dumps(v, ensure_ascii=False)}" for k, v in entries.items()]
    patches = read_manifest()["patches"]
    lines.append(f"Opravy kódu: {len(patches) or 'žádné'}")
    for e in patches:
        use, reason = patch_state(e)
        lines.append(f"  {e.get('source')}: commit {str(e.get('commit'))[:10]}, verze {e.get('version')} – {reason}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.source_overrides", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="stav ladění a oprav")
    sub.add_parser("check", help="ověří tuning.yaml (exit 1 při chybě)")
    p_in = sub.add_parser("install", help="nainstaluje opravu zdroje z commitu v master")
    p_in.add_argument("--source", required=True, help="klíč zdroje výšek, víc oddělit čárkou (si_clss,de_he)")
    p_in.add_argument("--commit", required=True)
    p_in.add_argument("--force", action="store_true", help="i když běží job")
    p_rm = sub.add_parser("remove", help="zruší opravu (návrat ke kódu image)")
    g = p_rm.add_mutually_exclusive_group(required=True)
    g.add_argument("--source")
    g.add_argument("--all", action="store_true")
    p_rm.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "status":
        print("\n".join(status_lines()))
    elif args.cmd == "check":
        _, errors = load_tuning()
        print("\n".join(errors) if errors else "tuning.yaml je v pořádku (nebo chybí)")
        return 1 if errors else 0
    elif args.cmd == "install":
        sources = [s.strip() for s in args.source.split(",") if s.strip()]
        try:
            res = install(sources, args.commit, force=args.force)
        except urllib.error.URLError as exc:
            raise SystemExit(f"GitHub API nedostupné: {exc}") from exc
        print(f"Nainstalováno: {', '.join(res['sources'])} z commitu {res['commit'][:10]} (verze {res['version']}). "
              "Platí od dalšího jobu.")
    elif args.cmd == "remove":
        gone = remove(None if args.all else [s.strip() for s in args.source.split(",")], force=args.force)
        print(f"Zrušeno: {', '.join(gone) or 'nic'}. Další job jede kódem image {settings.APP_VERSION}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
