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
ELEVATION_USER = "user_upload"  # = elevation_raster.USER_UPLOAD


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
    if (options or {}).get("user_sources") and job_country_code(options) != DEFAULT_COUNTRY:
        return ELEVATION_USER  # vlastní data zadavatele (app.user_sources), jen mimo Česko
    return country.elevation or ELEVATION_NONE


TECHNICAL_MAP_CZ = "cz_dtm"
TECHNICAL_MAP_NONE = "none"


def technical_map_source(options: dict | None) -> str:
    """Obdoba české DTM pro kresbu: ``cz_dtm`` (DMVS ČÚZK), ``nl_bgt`` (BGT, PDOK) nebo ``none`` (jen OSM)."""
    country = job_country(options)
    if country is None:
        return TECHNICAL_MAP_CZ if job_country_code(options) == DEFAULT_COUNTRY else TECHNICAL_MAP_NONE
    if not country.technical_map_covers((options or {}).get("bbox_wgs84")):
        # technická mapa jen pro část země (Plan de voirie = Paříž), výřez mimo: náhradní zdroj (BD TOPO) nebo OSM
        return country.technical_map_outside or TECHNICAL_MAP_NONE
    return country.technical_map or TECHNICAL_MAP_NONE


# Země běžícího jobu pro kód bez ``options`` (ortofoto ČÚZK, filtry kresby); proces jobu je vyhrazený a potomci
# (souběžné mapy) proměnnou dědí. Nastavuje ``run_job_pipeline``.
JOB_COUNTRY_ENV = "PODKLADARNA_JOB_COUNTRY"


# Jazyk textů výstupu běžícího jobu (.omap, ZIP): cs, nebo en u zadání z jiné než české stránky.
JOB_LANG_ENV = "PODKLADARNA_JOB_LANG"
# Adresa Omapicu u česky zadaného jobu z mezinárodní domény (české texty .omap pod značkou Omapic), jinak prázdné.
JOB_CS_INTL_ENV = "PODKLADARNA_JOB_CS_INTL_URL"


def set_process_country(options: dict | None) -> str:
    """Země (a jazyk výstupu) běžícího jobu do proměnných procesu."""
    from app.pipeline.zip_texts_en import cs_intl_site_url, zip_lang

    code = job_country_code(options)
    os.environ[JOB_COUNTRY_ENV] = code
    os.environ[JOB_LANG_ENV] = zip_lang(options)
    os.environ[JOB_CS_INTL_ENV] = cs_intl_site_url(options)
    return code


def process_cs_intl_url() -> str:
    """Adresa Omapicu, je-li běžící job česky z mezinárodní domény (jinak prázdné)."""
    return os.environ.get(JOB_CS_INTL_ENV, "") if process_lang() == "cs" else ""


def process_lang() -> str:
    """Jazyk výstupu běžícího jobu (bez nastavení česky)."""
    return "en" if os.environ.get(JOB_LANG_ENV) == "en" else "cs"


def process_uses_cz_sources() -> bool:
    return uses_cz_sources({"country": os.environ.get(JOB_COUNTRY_ENV) or DEFAULT_COUNTRY})


def citation(options: dict | None = None) -> str:
    """Krátká citace zdrojů pro šíření výstupu (licence místních dat), např. „Podklad: Podkladárna · ČÚZK · OSM, [rok]“."""
    country = job_country(options) if options is not None else process_country()
    return (country.citation if country else "") or "Base map: Omapic · OSM, [year]"


def attribution_lines(options: dict | None = None) -> tuple[str, ...]:
    """Zdroje dat s licencí, jak je vyžadují místní servery (prázdné = české znění).

    ``[data year]`` = rok stažení dat (Quellenvermerk s rokem dodání dat: M-V, LGLN);
    ``[data date]`` = den stažení dat (Etalab 2.0 chce zdroj a datum poslední aktualizace: IGN ve Francii).
    """
    from datetime import date

    country = job_country(options) if options is not None else process_country()
    if not country:
        return ()
    today = date.today()
    year, day = str(today.year), today.isoformat()
    return tuple(line.replace("[data year]", year).replace("[data date]", day) for line in country.attribution)


def process_country() -> Country | None:
    return job_country({"country": os.environ.get(JOB_COUNTRY_ENV) or DEFAULT_COUNTRY})
