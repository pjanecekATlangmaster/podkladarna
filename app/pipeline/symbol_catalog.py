"""Katalog symbolů čistých OCAD sad (``configs/oom/symbol_catalog.yaml``).

Jediný zdroj překladu kódů generátoru na kódy sady (``generator_codes`` →
``ocad_code``). Žádné implicitní „+.0“ a žádný fallback na syrový kód:
kód, který katalog nezná, se neřeší (``None``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from app.settings import CONFIG_DIR

CATALOG_PATH = CONFIG_DIR / "oom" / "symbol_catalog.yaml"


def normalize_code(code: str) -> str:
    """``401.0`` → ``401`` (OCAD export dává hlavním symbolům ``.0``).

    Jen pro kontroly a tvorbu katalogu; za běhu se jím kódy nepřekládají.
    """
    return code[:-2] if code.endswith(".0") else code


def uses_catalog(path: Path | str) -> bool:
    """True = nová čistá česká sada (``*_cz.omap``) řízená katalogem."""
    return Path(path).name.endswith("_cz.omap")


def catalog_key(path: Path | str) -> str | None:
    """Klíč sady v katalogu: ``isom`` / ``sprint`` / ``mtbo`` (jinak ``None``)."""
    name = Path(path).name
    if name.startswith("ISSprOM"):
        return "sprint"
    if name.startswith("ISMTBOM"):
        return "mtbo"
    if name.startswith("ISOM"):
        return "isom"
    return None


@lru_cache(maxsize=4)
def _load_catalog(path_str: str) -> dict[str, list[dict]]:
    raw = yaml.safe_load(Path(path_str).read_text(encoding="utf-8")) or {}
    return {str(k): list(v or []) for k, v in raw.items()}


def load_catalog() -> dict[str, list[dict]]:
    return _load_catalog(str(CATALOG_PATH))


@lru_cache(maxsize=8)
def _generator_map(path_str: str, key: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in _load_catalog(path_str).get(key, []):
        ocad = str(entry["ocad_code"])
        for gen in entry.get("generator_codes") or []:
            gen = str(gen)
            if gen in out and out[gen] != ocad:
                raise ValueError(
                    f"Katalog {key}: kód generátoru {gen!r} míří na {out[gen]!r} i {ocad!r}"
                )
            out[gen] = ocad
    return out


def generator_map(key: str) -> dict[str, str]:
    """{kód generátoru: kód sady} pro klíč ``isom`` / ``sprint`` / ``mtbo``."""
    return _generator_map(str(CATALOG_PATH), key)


def translate(path: Path | str, gen_code: str) -> str | None:
    """Kód generátoru → kód sady v ``path``; ``None`` když ho katalog nezná."""
    key = catalog_key(path)
    if key is None:
        return None
    return generator_map(key).get(gen_code)


def catalog_entry(path: Path | str, ocad_code: str) -> dict | None:
    key = catalog_key(path)
    if key is None:
        return None
    for entry in load_catalog().get(key, []):
        if str(entry["ocad_code"]) == ocad_code:
            return entry
    return None


def all_set_codes(gen_code: str) -> set[str]:
    """Kódy sady, na které se ``gen_code`` překládá ve všech sadách katalogu."""
    out: set[str] = set()
    for key in load_catalog():
        code = generator_map(key).get(gen_code)
        if code:
            out.add(code)
    return out
