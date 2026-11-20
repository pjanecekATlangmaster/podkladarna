"""Překlad chyb jobu do řeči pro uživatele bez technických znalostí.

Technické hlášky (``⚠ …`` v logu, výjimky) zůstávají v logu jobu; na kartu
jobu (``jobs.error``) jde lidská verze: co chybí, jaký to má dopad na mapu
a co dělat (zkusit později, znovu stáhnout podklady, nebo napsat Petrovi).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.i18n import SOURCE_LANG, t

# Kategorie (od nejzávažnější).
SEVERE = "severe"  # mapa je neúplná nebo chybná
MISSING = "missing"  # chybí podkladová vrstva / doplněk, mapa je v pořádku
OTHER = "other"  # ostatní / informační

# Doporučený postup (pořadí = síla doporučení, vybírá se nejsilnější).
CONTACT = "contact"
REFRESH = "refresh"
RETRY = "retry"
REPORT = "report"
NONE = "none"

_ADVICE_ORDER = (CONTACT, REFRESH, RETRY, REPORT, NONE)
_CATEGORY_ORDER = (SEVERE, MISSING, OTHER)

# Texty pro uživatele jsou v katalogu ``configs/i18n`` (``issue.*``); souhrn se píše v jazyce zadání jobu.
_ADVICE_KEYS = {CONTACT: "issue.advice.contact", REFRESH: "issue.advice.refresh", RETRY: "issue.advice.retry",
                REPORT: "issue.advice.report", NONE: "issue.advice.none"}


def advice_text(advice: str, lang: str = "cs") -> str:
    return t(_ADVICE_KEYS[advice], lang)


# Česká znění (zpětná kompatibilita, testy).
ADVICE_TEXT = {k: t(v, "cs") for k, v in _ADVICE_KEYS.items()}
DETAILS_HINT = t("issue.details", "cs")

# Délka lidského souhrnu ve sloupci ``jobs.error``.
SUMMARY_MAX_CHARS = 700


@dataclass(frozen=True)
class Explained:
    category: str
    what: str
    advice: str
    technical: str


# (vzor na začátku hlášky, kategorie, co chybí – lidsky, postup).
# Pořadí rozhoduje: první shoda vyhrává.
_RULES: tuple[tuple[str, str, str, str], ...] = (
    (r"OSM 501\b", SEVERE, "osm_paved", RETRY),
    (r"OSM:.*dlaždic", SEVERE, "osm_tiles", RETRY),
    (r"OSM:", SEVERE, "osm", RETRY),
    (r"DTM: ", SEVERE, "dtm", RETRY),
    (r"Technická mapa (?P<label>[^:]+):", SEVERE, "technical_map", RETRY),
    (r"Vektory:", SEVERE, "vectors", RETRY),
    (r"RÚIAN budovy", SEVERE, "ruian", RETRY),
    (r"AOPK stromy", SEVERE, "aopk", RETRY),
    (r"[^:]*: chybí DEM", SEVERE, "no_dem", REFRESH),
    (r"Výškové modely", SEVERE, "elevation", REFRESH),
    (r"DSM/CHM", SEVERE, "chm", REFRESH),
    (r"Vegetace", SEVERE, "vegetation", REFRESH),
    (r"Srázy", SEVERE, "cliffs", CONTACT),
    (r"Kupky a ďolíky", SEVERE, "knolls", CONTACT),
    (r"Balvany", SEVERE, "boulders", CONTACT),
    (r"Výstup:", SEVERE, "output", CONTACT),
    (r"OOM: proces mapy spadl", OTHER, "map_process", NONE),
    (r"OOM: podklady pro šablony", MISSING, "templates", CONTACT),
    (r"OOM: (?P<variant>.+?) nevytvořeno", SEVERE, "map_variant", CONTACT),
    (r"OOM OCD", MISSING, "ocd", REPORT),
    (r"OOM georef", MISSING, "georef", REPORT),
    (r"OOM náhled|Náhled compose", MISSING, "preview", REPORT),
    (r"uzitecne/", MISSING, "uzitecne", REPORT),
    (r"Ortofoto", MISSING, "orthophoto", RETRY),
    (r"Katastr", MISSING, "cadastre", RETRY),
    (r"OpenStreetMap:", MISSING, "osm_layer", RETRY),
    (r"Mapa ZTM", MISSING, "ztm", RETRY),
    (r"(?P<label>DTM kraje[^:]*)", MISSING, "dtm_region", RETRY),
    (r"Náhled DMP OK", MISSING, "dmpok", RETRY),
    (r"Stínování reliéfu|Stínování: žádný zdroj", MISSING, "hillshade", RETRY),
    (r"Stínování|Hillshade", MISSING, "hillshade_cuzk", RETRY),
    (r"Referenční podklady", MISSING, "references", RETRY),
)

_COMPILED = tuple((re.compile(p), c, w, a) for p, c, w, a in _RULES)
# Části české technické hlášky dosazované do textu (``{label}``) → klíč katalogu; jen mimo češtinu,
# česky se dosazuje text hlášky beze změny.
_LABEL_KEYS = {
    "Srázy": "issue.what.cliffs",
    "Kupky a ďolíky": "issue.what.knolls",
    "Balvany": "issue.what.boulders",
    "DTM kraje – plochy (ZPS)": "issue.what.dtm_region_areas",
    "DTM kraje – linie a body (ZPS)": "issue.what.dtm_region_lines",
}
_CONTEXT_PREFIX = re.compile(r"^\[[^\]]*\]\s*")


def explain_issue(text: str, lang: str = "cs") -> Explained:
    """Zařadí jednu technickou hlášku (bez značky ``⚠``); ``what`` v jazyce ``lang``."""
    technical = " ".join(str(text or "").split())
    core = _CONTEXT_PREFIX.sub("", technical)
    for rx, category, what, advice in _COMPILED:
        m = rx.match(core)
        if not m:
            continue
        label = core.split(":", 1)[0].strip()
        groups = {k: (v or "").strip() for k, v in m.groupdict().items()}
        groups.setdefault("label", label)
        if lang != SOURCE_LANG and groups["label"] in _LABEL_KEYS:
            groups["label"] = t(_LABEL_KEYS[groups["label"]], lang)
        groups["label_lc"] = groups["label"][:1].lower() + groups["label"][1:]
        return Explained(category, t(f"issue.what.{what}", lang, **groups), advice, technical)
    return Explained(OTHER, "", REPORT, technical)


def _unique(seq):
    out: list[str] = []
    for item in seq:
        if item and item not in out:
            out.append(item)
    return out


def user_summary(issues: list[str], lang: str = "cs") -> str:
    """Lidský souhrn chyb jobu pro kartu jobu (``jobs.error``) v jazyce zadání."""
    items = [explain_issue(text, lang) for text in issues if str(text or "").strip()]
    if not items:
        return ""
    severe = _unique(e.what for e in items if e.category == SEVERE)
    missing = _unique(e.what for e in items if e.category == MISSING)
    parts: list[str] = []
    if severe:
        parts.append(t("issue.summary.severe", lang, items=", ".join(severe)))
        if missing:
            parts.append(t("issue.summary.severe_missing", lang, items=", ".join(missing)))
    elif missing:
        parts.append(t("issue.summary.missing", lang, items=", ".join(missing)))
    else:
        parts.append(t("issue.summary.minor", lang))
    advice = min((e.advice for e in items), key=_ADVICE_ORDER.index)
    if advice == REPORT and severe:
        advice = CONTACT
    if advice != NONE:
        parts.append(advice_text(advice, lang))
    parts.append(t("issue.details", lang))
    return _clip(" ".join(parts))


# --- job úplně selhal ---------------------------------------------------

_FAILURE_RULES: tuple[tuple[str, str], ...] = (
    (r"došlo místo na disku|No space left on device|Errno 28|disk is full|disk full", "disk"),
    (r"časový limit", "timeout"),
    (r"restartem serveru", "restart"),
    (r"Server byl obsazený", "busy"),
    (r"nezůstaly žádné DMR|nezůstaly žádné DMP|žádné LiDAR|mimo listy", "lidar"),
    (
        r"ZABAGED|HTTP ?5\d\d|\b50[234]\b|timed? ?out|Timeout|Connection|"
        r"Max retries|Temporary failure|Service Unavailable|Bad Gateway",
        "download",
    ),
    (r"kódem -9|kódem 137|MemoryError|Cannot allocate", "memory"),
    # Mimo Česko (rastry výšek): ne výpadek (ten chytá „download“), ale výřez bez dat – moře, mimo pokrytí zdroje.
    (r"Výšková data \(.+\) nejsou k dispozici", "elevation"),
)
_FAILURE_COMPILED = tuple((re.compile(p), key) for p, key in _FAILURE_RULES)

FAILURE_DEFAULT = t("issue.failure.default", "cs")


def failure_key(technical: str) -> str:
    """Druh selhání jobu (``disk``, ``download``, ``elevation`` … nebo ``default``) podle technické hlášky."""
    text = " ".join(str(technical or "").split())
    for rx, name in _FAILURE_COMPILED:
        if rx.search(text):
            return name
    return "default"


def failure_summary(technical: str, lang: str = "cs") -> str:
    """Lidská zpráva pro job, který skončil ``failed`` (technika zůstává v logu), v jazyce zadání."""
    key = failure_key(technical)
    return _clip(f"{t('issue.failure.prefix', lang)} {t(f'issue.failure.{key}', lang)} {t('issue.details', lang)}")


def source_outage_summary(source: str, lang: str = "cs") -> str:
    """Zpráva pro job, který selhal na výpadku zdroje výšek země (``app.source_gate.failed_job_outage``)."""
    return _clip(f"{t('issue.failure.prefix', lang)} {t('issue.failure.source_down', lang, source=source)} {t('issue.details', lang)}")


def _clip(text: str) -> str:
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return text
