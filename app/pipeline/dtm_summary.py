"""Souhrn práce s DTM (Digitální technická mapa ČÚZK) pro uživatele: log, README i detail jobu.

Čisté funkce bez sítě. Texty vět se skládají tady, log, README a web je jen vypisují.
Soubor ``work/dtm_summary.json``: ``state`` (off | pending | used | weak | unavailable | failed),
``level`` (ok | warn | info), ``lines`` (hotové české věty), ``warn_lines`` (jen věty o skutečném problému –
log, README i web zvýrazní varováním jen je, ostatní věty jsou neutrální informace), ``notice_lines`` (upozornění
pro příjemce mapy: DTM se pro mapu používá, ale při zpracování bylo nedostupné – log a README s prefixem
„UPOZORNĚNÍ: “, web nenápadnou poznámkou, privátní job v e-mailu; stav jobu ani statistiku chyb nemění) a podpůrné údaje.

Tři stavy (Petr 2026-11-13): DTM se použilo (``used`` / ``weak``), území bez dat DTM nebo DTM vypnuté
(``unavailable`` / ``off`` – jen neutrální věta, ne výpadek) a DTM se mělo použít, ale příprava skončila výjimkou
(výpadek DMVS nebo serveru kraje, plánovaná odstávka – ``failed``, jediný stav s upozorněním příjemci).

Chybějící nebo nedostupné DTM je problém jen u sprintu kresleného z DTM (``dtm_required``); les a MTBO se bez DTM
kreslí plnohodnotně z OSM a ZABAGED, proto je tam věta jen neutrální poznámka (Petr 2026-11-13).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.pipeline.dtm_import import (
    BUILDINGS_NAME,
    FENCES_NAME,
    PARKING_NAME,
    ROADS_NAME,
    SIDEWALKS_NAME,
)

SUMMARY_NAME = "dtm_summary.json"
NOTICE_PREFIX = "UPOZORNĚNÍ: "
OUTAGE_NOTICE = (
    "Digitální technická mapa (DTM) ČÚZK byla při zpracování nedostupná (výpadek nebo odstávka zdroje). "
    "Mapa je kreslená z OSM a ZABAGED, jen budovy, ploty, chodníky a plochy silnic nemají přesnější tvar z DTM; "
    "zadání můžete později spustit znovu."
)
README_TITLE = "Digitální technická mapa (DTM)"

_MAP_LABELS = {"sprint": "sprint", "les": "les", "mtbo": "MTBO"}
_AREA_FILES = (
    ("budovy", BUILDINGS_NAME),
    ("chodníky", SIDEWALKS_NAME),
    ("silnice", ROADS_NAME),
    ("parkoviště", PARKING_NAME),
)


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " a " + items[-1]


def _labels(tags: list[str]) -> list[str]:
    return [_MAP_LABELS.get(t, t) for t in tags]


def _obci(n: int) -> str:
    """„1 obec“ / „2 obce“ / „5 obcí“."""
    return f"{n} obec" if n == 1 else (f"{n} obce" if 2 <= n <= 4 else f"{n} obcí")


def _selection_lines(maps: dict[str, bool], dtm_on: bool, auto: bool = True) -> list[str]:
    used = [t for t, on in maps.items() if on and dtm_on]
    rest = [t for t in maps if t not in used]
    if not used:
        if "sprint" in maps or not auto:
            return ["Kresba z DTM je vypnutá, mapa se kreslí z OSM a ZABAGED."]
        return [
            "DTM se pro tuto mapu nepoužívá (volba DTM: automaticky – DTM jen pro sprint); "
            "mapa se kreslí z OSM a ZABAGED."
        ]
    if not rest:
        return []
    if "sprint" in used and set(rest) <= {"les", "mtbo"}:
        text = _join(_labels(rest))
        return [
            f"{text[0].upper()}{text[1:]} se z DTM nekreslí "
            "(volba DTM: automaticky – DTM jen pro sprint); kreslí se z OSM a ZABAGED."
        ]
    return [f"Z DTM se nekreslí: {_join(_labels(rest))}; kreslí se z OSM a ZABAGED."]


def initial_summary(maps: dict[str, bool], dtm_on: bool, *, auto: bool = True) -> dict[str, Any]:
    """Souhrn v době rozhodnutí: ``off`` (DTM se nepoužije) nebo ``pending`` (data se připravují).

    ``auto`` = volba DTM „automaticky“ (les a MTBO bez DTM podle presetu); False = volba DTM vypnutá.
    """
    used = [t for t, on in maps.items() if on and dtm_on]
    return {
        "state": "pending" if used else "off",
        "level": "info",
        "auto": bool(auto),
        "maps": {t: bool(on and dtm_on) for t, on in maps.items()},
        "lines": _selection_lines(maps, dtm_on, auto),
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def dtm_required(maps: dict[str, bool]) -> bool:
    """DTM je pro kresbu podstatné jen u sprintu kresleného z DTM; les a MTBO se bez něj kreslí z OSM a ZABAGED."""
    return bool((maps or {}).get("sprint"))


def finalize_summary(summary: dict[str, Any], work_dir: Path, *, failed: bool) -> dict[str, Any]:
    """Doplní stav a věty podle toho, co příprava DTM skutečně zapsala do ``work/dtm``."""
    out = dict(summary)
    maps = out.get("maps") or {}
    used = [t for t, on in maps.items() if on]
    selection = _selection_lines(maps, True, bool(out.get("auto", True))) if used else list(out.get("lines") or [])
    if not used:
        return out
    required = dtm_required(maps)
    if failed:
        if required:
            line = "Příprava DTM selhala, mapa se kreslila bez DTM."
            out.update(state="failed", level="warn", lines=[line], warn_lines=[line])
        else:
            out.update(state="failed", level="info", lines=[OUTAGE_NOTICE], warn_lines=[], notice_lines=[OUTAGE_NOTICE])
        return out
    dtm = work_dir / "dtm"
    fences = _read_json(dtm / FENCES_NAME)
    if not isinstance(fences, dict):
        line = "DTM pro toto území není k dispozici, mapa se kreslí z OSM a ZABAGED."
        out.update(
            state="unavailable",
            level="warn" if required else "info",
            lines=[line],
            warn_lines=[line] if required else [],
        )
        return out

    units = [u for u in fences.get("units") or [] if isinstance(u, dict)]
    missing = [str(u.get("name") or u.get("code")) for u in units if not u.get("has_data")]
    names = {f"{u.get('kind')}_{u.get('code')}": str(u.get("name") or u.get("code")) for u in units}
    incomplete = [names.get(str(k), str(k).split("_", 1)[-1]) for k in fences.get("incomplete_opl") or []]
    counts = {"ploty": len(fences.get("fences") or [])}
    for label, name in _AREA_FILES:
        data = _read_json(dtm / name)
        if isinstance(data, dict):
            counts[label] = len(data.get("polygons") or [])
    empty = not any(counts.values())
    packages = len(fences.get("packages") or [])

    if units:
        covered = f"{_obci(len(units) - len(missing))}"
    else:
        covered = f"{packages} balíků dat"
    detail = ", ".join(f"{label} {n}" for label, n in counts.items() if n)
    head = (
        f"Digitální technická mapa (DTM) ČÚZK: použita pro {_join(_labels(used))} "
        f"({covered}{', ' + detail if detail else ''})."
    )
    problems: list[str] = []
    if missing:
        problems.append(
            f"DTM pokrývá jen {len(units) - len(missing)} z {len(units)} "
            f"{'obce' if len(units) == 1 else 'obcí'} výřezu (chybí: {', '.join(missing)}); "
            "jinde se kreslí z OSM a ZABAGED."
        )
    if incomplete:
        problems.append(
            (f"V obci {incomplete[0]} jsou" if len(incomplete) == 1 else f"V obcích {', '.join(incomplete)} jsou")
            + " plochy DTM neúplné, budovy a chodníky tam kreslí OSM."
        )
    if empty:
        problems.append("DTM ve výřezu neobsahuje ploty ani plochy.")
    weak = bool(problems)
    out.update(
        state="weak" if weak else "used",
        level=("warn" if required else "info") if weak else "ok",
        lines=[head, *problems, *selection],
        warn_lines=problems if required else [],
        packages=packages,
        units_total=len(units),
        units_with_data=len(units) - len(missing),
        missing=missing,
        incomplete=incomplete,
        counts=counts,
    )
    return out


def _work(path: Path) -> Path:
    return path / "work" if (path / "work").is_dir() else path


def write_summary(work_dir: Path, summary: dict[str, Any]) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / SUMMARY_NAME).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_summary(job_dir_or_work: Path) -> dict[str, Any] | None:
    data = _read_json(_work(Path(job_dir_or_work)) / SUMMARY_NAME)
    return data if isinstance(data, dict) and data.get("lines") else None


def warning_lines(summary: dict[str, Any]) -> set[str]:
    """Věty o skutečném problému. Starší souhrny bez ``warn_lines``: u úrovně warn vše kromě úvodní
    věty o použití DTM a vět o volbě mapy (ty jsou jen informace)."""
    if "warn_lines" in summary:
        return set(summary.get("warn_lines") or [])
    if summary.get("level") != "warn":
        return set()
    info = set(_selection_lines(summary.get("maps") or {}, True, bool(summary.get("auto", True))))
    return {
        line
        for line in summary.get("lines") or []
        if not line.startswith("Digitální technická mapa") and line not in info
    }


def notice_lines(summary: dict[str, Any] | None) -> list[str]:
    """Upozornění pro příjemce mapy (DTM se používá, ale bylo nedostupné); prázdné u jiných stavů a starších souhrnů."""
    if not summary:
        return []
    return [str(line) for line in summary.get("notice_lines") or [] if line]


def log_lines(summary: dict[str, Any]) -> list[str]:
    """Věty pro log a README; prefix „VAROVÁNÍ: “ jen u vět o skutečném problému, „UPOZORNĚNÍ: “ u upozornění
    pro příjemce mapy."""
    warn = warning_lines(summary)
    notice = set(notice_lines(summary))
    out = []
    for line in summary.get("lines") or []:
        if line in warn:
            out.append(f"VAROVÁNÍ: {line}")
        elif line in notice:
            out.append(f"{NOTICE_PREFIX}{line}")
        else:
            out.append(line)
    return out


def readme_block(summary: dict[str, Any] | None) -> str:
    """Blok do README_OOM.txt; prázdný řetězec bez souhrnu."""
    if not summary or not summary.get("lines"):
        return ""
    return "\n".join([README_TITLE, "-" * len(README_TITLE), *log_lines(summary)])
