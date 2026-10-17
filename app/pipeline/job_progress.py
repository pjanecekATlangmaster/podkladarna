"""Přehled kroků jobu v logu: ``[N/M] Krok · čas od startu``, detaily odsazené."""

from __future__ import annotations

import time
from typing import Callable


LogFn = Callable[[str], None]

# Odsazení detailních řádků pod hlavičkou kroku (hlavní vlákno).
DETAIL_INDENT = "  "


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

    def elapsed(self) -> str:
        return format_elapsed(self._clock() - self._start)

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

    def done(self) -> None:
        """Ukončí aktuální krok (posune pořadí)."""
        if self._open_label is None:
            return
        label = self._open_label
        self._open_label = None
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
        steps.append("sestavení mapy a ZIP")
    else:
        steps.append("balení výstupu")
    return steps
