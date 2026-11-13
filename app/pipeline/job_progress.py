"""Přehled kroků jobu v logu: ``[N/M] Krok · čas od startu``, detaily odsazené."""

from __future__ import annotations

import threading
import time
from typing import Callable


LogFn = Callable[[str], None]

# Odsazení detailních řádků pod hlavičkou kroku (hlavní vlákno).
DETAIL_INDENT = "  "

# Značka neopravené chyby (krok/podklad chybí ve výstupu). Fallback, který
# uspěl, se takto neoznačuje.
ISSUE_LOG_PREFIX = "⚠ "


def log_issue(log: LogFn | None, message: str) -> None:
    """Zaloguje neopravenou chybu; na konci jobu se zopakuje v souhrnu."""
    if log is None or not message:
        return
    log(ISSUE_LOG_PREFIX + str(message).strip())


class IssueCollector:
    """Obal job logu: řádky se značkou ``⚠`` si zapamatuje (i z vláken na pozadí)."""

    def __init__(self, log: LogFn) -> None:
        self._log = log
        self._lock = threading.Lock()
        self.items: list[str] = []

    def __call__(self, msg: str) -> None:
        if msg and ISSUE_LOG_PREFIX in msg:
            text = " ".join(
                str(msg).replace(ISSUE_LOG_PREFIX, "", 1).split()
            )
            with self._lock:
                if text not in self.items:
                    self.items.append(text)
        self._log(msg)

    def report(self) -> None:
        """Zopakuje chyby blokem na konci logu (mimo sběr – nezdvojí se)."""
        if not self.items:
            return
        self._log("")
        self._log(f"{ISSUE_LOG_PREFIX}Dokončeno s chybami ({len(self.items)}):")
        for item in self.items:
            self._log(f"{DETAIL_INDENT}- {item}")


def format_elapsed(seconds: float) -> str:
    """``m:ss`` (nebo ``h:mm:ss`` od hodiny)."""
    total = max(0, int(round(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def indented_log(log: LogFn) -> LogFn:
    """Detailní řádky kroku odsadí pod hlavičku (i víceřádkové zprávy)."""

    def _log(msg: str) -> None:
        if not msg:
            log(msg)
            return
        log("\n".join(DETAIL_INDENT + ln for ln in str(msg).splitlines()))

    return _log


# Kroky sestavení mapy (Petr 2026-11-13: dřív jeden krok „sestavení mapy a ZIP“). Fáze mapy hlásí
# ``map_phase`` z ``package_oom`` / ``discipline_maps``; při souběhu les ‖ MTBO se krok posune, až fázi
# dosáhnou všechny mapy (``MapPhaseTracker``).
DTM_STEP = "DTM kraje"
MAP_PHASES: tuple[str, ...] = (
    "polohopis a vegetace",
    "silnice a cesty",
    "ploty, budovy a oliva",
    "slučování zdrojů a odsun od cest",
    "zjednodušení a zápis mapy",
    "převod do OCAD",
)
PREVIEW_STEP = "náhled mapy"
ZIP_STEP = "balení ZIP"
LEGACY_MAPS_STEP = "sestavení mapy a ZIP"
# Součet těchto kroků = skládání map pro kalibraci odhadu (``db.record_job_usage``); starší joby měly jeden krok.
MAP_ASSEMBLY_STEPS: frozenset[str] = frozenset(
    (DTM_STEP, *MAP_PHASES, PREVIEW_STEP, ZIP_STEP, LEGACY_MAPS_STEP)
)

_map_phase_hook: Callable[[str], None] | None = None

# Úlohy na pozadí (stahování podkladů, DTM, OSM) mimo pořadí kroků: řádek ``Na pozadí: <název> · <stav>``
# (stav ``běží`` / ``hotovo za m:ss`` / ``z mezipaměti`` / ``nedostupné`` / ``selhalo``), web je ukáže zvlášť.
BACKGROUND_PREFIX = "Na pozadí: "


class background_task:
    """``with background_task(log, "Ortofoto") as t:`` – zapíše začátek a konec úlohy; ``t.state`` přepíše výsledek."""

    def __init__(self, log: LogFn | None, name: str) -> None:
        self._log = log
        self.name = " ".join(str(name).replace("·", "-").split())
        self.state: str | None = None
        self._t0 = time.monotonic()

    def _emit(self, state: str) -> None:
        if self._log is not None and self.name:
            try:
                self._log(f"{BACKGROUND_PREFIX}{self.name} · {state}")
            except Exception:
                pass

    def __enter__(self) -> "background_task":
        self._t0 = time.monotonic()
        self._emit("běží")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self._emit("selhalo")
        else:
            self._emit(self.state or f"hotovo za {format_elapsed(time.monotonic() - self._t0)}")
        return False


def set_map_phase_hook(hook: Callable[[str], None] | None) -> Callable[[str], None] | None:
    """Nastaví příjemce fází mapy (v procesu jobu tracker, v potomkovi souběhu fronta logu); vrátí předchozí."""
    global _map_phase_hook
    prev = _map_phase_hook
    _map_phase_hook = hook
    return prev


def map_phase(label: str) -> None:
    """Fáze sestavení mapy (jeden z ``MAP_PHASES``); bez příjemce nic. Chyba hlášení nesmí shodit mapu."""
    hook = _map_phase_hook
    if hook is None:
        return
    try:
        hook(label)
    except Exception:
        pass


class MapPhaseTracker:
    """Fáze map disciplín → kroky ``JobProgress``: krok se otevře, až ho dosáhnou všechny mapy (minimum)."""

    def __init__(self, progress: "JobProgress | None", tags: list[str]) -> None:
        self._progress = progress
        self._reached = {str(t): 0 for t in tags}
        self._current = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        """Otevře první fázi (volá proces jobu těsně před sestavením map)."""
        if self._progress is not None and MAP_PHASES:
            self._progress.begin(MAP_PHASES[0])

    def report(self, tag: str, label: str) -> None:
        if label not in MAP_PHASES:
            return
        idx = MAP_PHASES.index(label)
        tag = str(tag)
        with self._lock:
            if tag not in self._reached or idx <= self._reached[tag]:
                return
            self._reached[tag] = idx
            low = min(self._reached.values())
            if low <= self._current:
                return
            self._current = low
            if self._progress is not None:
                self._progress.begin(MAP_PHASES[low])


def _label(text: str) -> str:
    return text[:1].upper() + text[1:]


class JobProgress:
    """Sekvenční kroky pipeline do job logu (bez časových odhadů)."""

    def __init__(
        self,
        log: LogFn | None,
        steps: list[str],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._log = log
        self.steps = [str(s).strip() for s in steps if str(s).strip()]
        self._index = 0
        self._open_label: str | None = None
        self._clock = clock
        self._start = clock()
        self._emitted_header = False
        self._open_at: float | None = None
        # Časy kroků pro statistiky buildu (GUI): label, sekundy, přeskočeno + důvod.
        self._timings: list[dict] = []

    def elapsed(self) -> str:
        return format_elapsed(self.elapsed_seconds())

    def elapsed_seconds(self) -> float:
        return max(0.0, self._clock() - self._start)

    def timings(self) -> list[dict]:
        """Hotové kroky (+ právě běžící s dosavadním časem) v pořadí běhu."""
        out = [dict(t) for t in self._timings]
        if self._open_label is not None and self._open_at is not None:
            out.append(
                {
                    "label": self._open_label,
                    "seconds": round(max(0.0, self._clock() - self._open_at), 1),
                    "open": True,
                }
            )
        return out

    def announce_plan(self) -> None:
        """Jednou na začátku: co všechno job udělá."""
        if self.steps:
            self._emit(f"Plán ({len(self.steps)} kroků): {' · '.join(self.steps)}")

    def begin(self, label: str) -> None:
        """Zahájí krok ``label``; předchozí otevřený krok nejdřív ukončí."""
        text = str(label).strip()
        if not text:
            return
        if self._open_label is not None:
            self.done()
        self._seek(text)
        self._header(f"{_label(text)} · {self.elapsed()}")
        self._open_label = text
        self._open_at = self._clock()

    def done(self) -> None:
        """Ukončí aktuální krok (posune pořadí)."""
        if self._open_label is None:
            return
        label = self._open_label
        self._open_label = None
        if self._open_at is not None:
            self._timings.append(
                {"label": label, "seconds": round(max(0.0, self._clock() - self._open_at), 1)}
            )
        self._open_at = None
        self._advance_past(label)

    def skip(self, label: str, *, reason: str | None = None) -> None:
        """Označí krok jako přeskočený (cache / není potřeba)."""
        text = str(label).strip()
        if not text:
            return
        if self._open_label is not None:
            self.done()
        self._seek(text)
        note = f" ({reason})" if reason else ""
        self._header(f"{_label(text)} – přeskočeno{note}")
        self._timings.append(
            {"label": text, "seconds": 0.0, "skipped": True, **({"reason": reason} if reason else {})}
        )
        self._advance_past(text)

    def finish_all(self) -> None:
        """Zavře otevřený krok (např. před finálním Hotovo)."""
        if self._open_label is not None:
            self.done()

    def _seek(self, text: str) -> None:
        # Posuň index na očekávaný label (příp. přeskočené mezi-kroky).
        if self._index < len(self.steps):
            try:
                self._index = self.steps.index(text, self._index)
            except ValueError:
                pass

    def _advance_past(self, label: str) -> None:
        if self._index < len(self.steps) and self.steps[self._index] == label:
            self._index += 1
        elif label in self.steps:
            # label byl mimo pořadí – posuň za něj
            self._index = self.steps.index(label) + 1
        else:
            self._index += 1

    def _header(self, body: str) -> None:
        total = len(self.steps) or 1
        number = min(self._index + 1, total)
        # Prázdný řádek odděluje kroky (ne před úplně prvním).
        if self._emitted_header:
            self._emit("")
        self._emitted_header = True
        self._emit(f"[{number}/{total}] {body}")

    def _emit(self, message: str) -> None:
        if self._log is None:
            return
        self._log(message)


def plan_pipeline_steps(
    *,
    bbox: object | None,
    reused_from: object | None,
    want_zip: bool,
    want_refs: bool,
) -> list[str]:
    """Sestaví očekávané kroky běhu (přehled „co ještě následuje“)."""
    steps: list[str] = []
    if reused_from:
        steps.append("kopie LAZ z předchozího jobu")
    if bbox:
        steps.append("stažení LiDAR")
        steps.append("stažení ZABAGED")
        steps.append("mřížka mapy")
    # Příprava LiDAR vždy v plánu – při reuse/cache se v běhu skipne.
    steps.append("příprava LiDAR")
    if bbox:
        steps.append("výškové modely")
        steps.append("příprava ZABAGED")
    steps.extend(
        [
            "vrstevnice",
            "vegetace",
            "srázy",
            "kupky a ďolíky",
        ]
    )
    if bbox:
        steps.append("OSM pěšiny a objekty")
    steps.append("stínování reliéfu")
    if want_zip:
        if want_refs and bbox:
            steps.append("referenční podklady")
        if bbox:
            steps.append("RÚIAN a AOPK")
            steps.append(DTM_STEP)
            steps.extend(MAP_PHASES)
            steps.append(PREVIEW_STEP)
        steps.append(ZIP_STEP)
    else:
        steps.append("balení výstupu")
    return steps
