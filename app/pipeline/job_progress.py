"""Přehled kroků jobu v logu: aktuální krok + co následuje; mezera po hotovém."""

from __future__ import annotations

from typing import Callable


LogFn = Callable[[str], None]


class JobProgress:
    """Sekvenční kroky pipeline do job logu (bez časových odhadů)."""

    def __init__(self, log: LogFn | None, steps: list[str]) -> None:
        self._log = log
        self.steps = [str(s).strip() for s in steps if str(s).strip()]
        self._index = 0
        self._open_label: str | None = None

    def begin(self, label: str) -> None:
        """Zahájí krok ``label``; předchozí otevřený krok nejdřív ukončí mezerou."""
        text = str(label).strip()
        if not text:
            return
        if self._open_label is not None:
            self.done()
        # Posuň index na očekávaný label (příp. přeskočené mezi-kroky).
        if self._index < len(self.steps):
            try:
                found = self.steps.index(text, self._index)
            except ValueError:
                found = None
            if found is not None:
                self._index = found
        total = len(self.steps) or 1
        number = min(self._index + 1, total)
        self._emit(f"=== Krok {number}/{total}: {text} ===")
        remaining = self._remaining_after(text)
        if remaining:
            self._emit(f"  Následuje: {' · '.join(remaining)}")
        else:
            self._emit("  Následuje: (poslední krok)")
        self._open_label = text

    def done(self) -> None:
        """Ukončí aktuální krok a zapíše prázdný řádek pro přehlednost."""
        if self._open_label is None:
            return
        label = self._open_label
        self._open_label = None
        if self._index < len(self.steps) and self.steps[self._index] == label:
            self._index += 1
        elif label in self.steps:
            # label byl mimo pořadí – posuň za něj
            self._index = self.steps.index(label) + 1
        else:
            self._index += 1
        self._emit("")

    def skip(self, label: str, *, reason: str | None = None) -> None:
        """Označí krok jako přeskočený (cache / není potřeba) a vloží mezeru."""
        text = str(label).strip()
        if not text:
            return
        if self._open_label is not None:
            self.done()
        if self._index < len(self.steps):
            try:
                found = self.steps.index(text, self._index)
            except ValueError:
                found = None
            if found is not None:
                self._index = found
        note = f" ({reason})" if reason else ""
        total = len(self.steps) or 1
        number = min(self._index + 1, total)
        self._emit(f"=== Krok {number}/{total}: {text} – přeskočeno{note} ===")
        remaining = self._remaining_after(text)
        if remaining:
            self._emit(f"  Následuje: {' · '.join(remaining)}")
        if self._index < len(self.steps) and self.steps[self._index] == text:
            self._index += 1
        self._emit("")

    def finish_all(self) -> None:
        """Zavře otevřený krok (např. před finálním Hotovo)."""
        if self._open_label is not None:
            self.done()

    def _remaining_after(self, label: str) -> list[str]:
        if label in self.steps:
            idx = self.steps.index(label)
            return self.steps[idx + 1 :]
        if self._index < len(self.steps):
            return self.steps[self._index + 1 :]
        return []

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
        steps.append("georef mřížka")
    # Prepare LiDAR vždy v plánu – při reuse/cache se v běhu skipne.
    steps.append("prepare LiDAR")
    if bbox:
        steps.append("DEM/DSM/CHM")
        steps.append("prepare ZABAGED")
    steps.extend(
        [
            "vrstevnice",
            "vegetace",
            "srázy DEM",
            "knolly",
        ]
    )
    if bbox:
        steps.append("OSM pěšiny a objekty")
    steps.append("hillshade")
    if want_zip:
        if want_refs and bbox:
            steps.append("referenční podklady")
        if bbox:
            steps.append("RÚIAN a AOPK")
        steps.append("OOM / ZIP")
    else:
        steps.append("balení výstupu")
    return steps
