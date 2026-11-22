"""Upozornění na stáří a druh výškových dat země ještě před buildem (Petr 2026-11-16).

Důsledky dat jsou statické podle země (``elevation_years``, ``data_notes`` a ``green`` v ``configs/countries.yaml``),
proto je ukazuje GUI před tlačítkem Spustit (``/api/sheets`` → ``probe.data_notes``), log jobu, README ZIPu
a poznámka .omap (Mapa → Poznámky) z téhož zdroje. Šrafa v mapě ne: týká se celého výřezu, ne jeho části.
Upozornění jen tam, kde data opravdu mění kresbu; Česko žádné (český build beze změny).

Kresbu nemění.
"""

from __future__ import annotations

from typing import Any

from app import i18n
from app.countries import DEFAULT_COUNTRY, Country

# Klíče důsledků z countries.yaml (``data_notes``); ``photo`` se přidá samo u ``green: photo``.
NOTE_KEYS = ("old", "coarse", "noncommercial")  # noncommercial = licence výšek jen pro nekomerční užití (ES-CL)
# Důsledky jen pro les a MTBO (zeleň v lese z ``veg_texture``); sprint je nemá.
FOREST_ONLY = frozenset({"photo"})


def note_keys(country: Country | None) -> list[str]:
    """Klíče důsledků dat země v pořadí z configu (bez Česka a neznámých klíčů)."""
    if country is None or country.code == DEFAULT_COUNTRY:
        return []
    keys = [k for k in country.data_notes if k in NOTE_KEYS]
    if "old" in keys and not country.elevation_years:
        keys.remove("old")  # bez roků není co říct
    if country.green == "photo":
        keys.append("photo")
    return keys


def _msg(country: Country, key: str) -> i18n.Msg:
    return i18n.Msg(f"api.data_note.{key}", years=country.elevation_years)


def probe_notes(country: Country | None) -> dict[str, Any] | None:
    """Pro ``/api/sheets``: ``{summary, notes: [{text, forest_only}]}``; bez důsledků None (GUI nic neukáže)."""
    keys = note_keys(country)
    if not keys:
        return None
    from app.pipeline import elevation_raster

    src = country.elevation
    source = i18n.Msg(f"api.elevation_source.{src}") if src in elevation_raster.SOURCE_LABELS else src
    summary = (
        i18n.Msg("api.data_note.summary_years", source=source, years=country.elevation_years)
        if country.elevation_years
        else i18n.Msg("api.data_note.summary", source=source)
    )
    return {
        "summary": summary,
        "notes": [{"key": k, "text": _msg(country, k), "forest_only": k in FOREST_ONLY} for k in keys],
    }


def meta_lines(meta: dict | None, lang: str) -> list[str]:
    """Důsledky dat do README ZIPu z metadat jobu (Česko a starší metadata bez země nic)."""
    from app.countries import load_countries

    meta = meta or {}
    country = load_countries().get(str(meta.get("country") or DEFAULT_COUNTRY))
    return text_lines(country, lang, sprint=str(meta.get("preset_id") or "").startswith("sprint"))


def job_lines(job: dict | None, lang: str = "cs") -> list[str]:
    """Důsledky dat pro hotový job (detail jobu na webu, Petr 2026-11-22: upozornit i na konci generování).

    Vlastní výšky zadavatele nejsou národní zdroj země → nic; Česko nic. Lesní důsledky jen u jobu, který
    není čistě sprint (``preset_id`` sprint)."""
    from app.pipeline.country_profile import job_country

    job = job or {}
    opts = job.get("options") if isinstance(job.get("options"), dict) else {}
    if opts.get("user_sources"):
        return []
    return text_lines(job_country(opts), lang, sprint=str(job.get("preset_id") or "").startswith("sprint"))


def text_lines(country: Country | None, lang: str = "cs", *, sprint: bool = False) -> list[str]:
    """Důsledky dat jako věty v jazyce (log jobu česky, README, poznámka .omap); sprint bez lesních."""
    if country is None:
        return []
    return [_msg(country, k).render(lang) for k in note_keys(country) if not (sprint and k in FOREST_ONLY)]
