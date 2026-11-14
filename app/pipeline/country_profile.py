"""Profil zdrojů jobu podle země výřezu (zahraniční build).

Země jobu je ``options["country"]`` (nastaví ``app.main`` z ``app.countries.check_bbox``); starší joby bez pole
jsou Česko. Mimo Česko se české zdroje (ZABAGED, RÚIAN, DTM, katastr, AOPK, ČÚZK ortofoto a podklady)
nestahují a mapa se kreslí z OSM; výšková data dodá zdroj země (``elevation`` v ``configs/countries.yaml``).
"""

from __future__ import annotations

import os

from app.countries import DEFAULT_COUNTRY, Country, load_countries

ELEVATION_CZ = "cz_openzu"
ELEVATION_NONE = "none"


def job_country_code(options: dict | None) -> str:
    code = str((options or {}).get("country") or DEFAULT_COUNTRY).strip().upper()
    return code or DEFAULT_COUNTRY


def job_country(options: dict | None) -> Country | None:
    return load_countries().get(job_country_code(options))


def uses_cz_sources(options: dict | None) -> bool:
    """Stahovat české vektory a podklady (ZABAGED, RÚIAN, DTM, katastr, AOPK, ČÚZK)?"""
    country = job_country(options)
    if country is None:
        return job_country_code(options) == DEFAULT_COUNTRY
    return country.cz_sources


def elevation_source(options: dict | None) -> str:
    """Zdroj výšek jobu: ``cz_openzu`` (LiDAR ČÚZK, listy SM5), ``none`` (bez terénu) nebo zdroj země."""
    country = job_country(options)
    if country is None:
        return ELEVATION_CZ if job_country_code(options) == DEFAULT_COUNTRY else ELEVATION_NONE
    return country.elevation or ELEVATION_NONE


# Země běžícího jobu pro kód bez ``options`` (ortofoto ČÚZK, filtry kresby); proces jobu je vyhrazený a potomci
# (souběžné mapy) proměnnou dědí. Nastavuje ``run_job_pipeline``.
JOB_COUNTRY_ENV = "PODKLADARNA_JOB_COUNTRY"


def set_process_country(options: dict | None) -> str:
    code = job_country_code(options)
    os.environ[JOB_COUNTRY_ENV] = code
    return code


def process_uses_cz_sources() -> bool:
    return uses_cz_sources({"country": os.environ.get(JOB_COUNTRY_ENV) or DEFAULT_COUNTRY})


def citation(options: dict | None = None) -> str:
    """Krátká citace zdrojů pro šíření výstupu (licence místních dat), např. „Podklad: Podkladárna · ČÚZK · OSM, [rok]“."""
    country = job_country(options) if options is not None else process_country()
    return (country.citation if country else "") or "Podklad: Podkladárna · OSM, [rok]"


def attribution_lines(options: dict | None = None) -> tuple[str, ...]:
    """Zdroje dat s licencí, jak je vyžadují místní servery (prázdné = české znění)."""
    country = job_country(options) if options is not None else process_country()
    return country.attribution if country else ()


def process_country() -> Country | None:
    return job_country({"country": os.environ.get(JOB_COUNTRY_ENV) or DEFAULT_COUNTRY})
