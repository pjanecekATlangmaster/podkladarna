"""Překlad chyb jobu do řeči pro uživatele bez technických znalostí.

Technické hlášky (``⚠ …`` v logu, výjimky) zůstávají v logu jobu; na kartu
jobu (``jobs.error``) jde lidská verze: co chybí, jaký to má dopad na mapu
a co dělat (zkusit později, znovu stáhnout podklady, nebo napsat Petrovi).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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

FEEDBACK_HINT = "přes „Napsat zpětnou vazbu“ u tohoto jobu"

ADVICE_TEXT = {
    CONTACT: (
        "Nejspíš jde o chybu Podkladárny, nový build sám nepomůže. "
        f"Napište prosím Petrovi {FEEDBACK_HINT}."
    ),
    REFRESH: (
        "Spusťte job znovu se zaškrtnutou volbou „Znovu stáhnout a spočítat "
        f"podklady“. Pokud se chyba opakuje, napište Petrovi {FEEDBACK_HINT}."
    ),
    RETRY: (
        "Nejspíš jde o dočasný výpadek služby, ze které se data stahují. "
        "Spusťte stejný job znovu za pár hodin."
    ),
    REPORT: (
        "Mapu můžete normálně použít. Pokud se to opakuje, dejte Petrovi "
        f"vědět {FEEDBACK_HINT}."
    ),
    NONE: "Není potřeba nic dělat.",
}

DETAILS_HINT = "Technické podrobnosti jsou v logu jobu."

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
    # --- mapa je neúplná ---
    (r"OSM 501\b", SEVERE, "zpevněné plochy z OpenStreetMap", RETRY),
    (r"OSM:.*dlaždic", SEVERE, "část cest a objektů z OpenStreetMap", RETRY),
    (r"OSM:", SEVERE, "cesty a objekty z OpenStreetMap", RETRY),
    (r"DTM: ", SEVERE, "kresba z Digitální technické mapy (budovy, ploty, šířky cest)", RETRY),
    (r"Vektory:", SEVERE, "data stahovaná předem (ZABAGED, RÚIAN, AOPK)", RETRY),
    (r"RÚIAN budovy", SEVERE, "budovy z RÚIAN (část budov může chybět)", RETRY),
    (r"AOPK stromy", SEVERE, "významné stromy z AOPK", RETRY),
    (r"[^:]*: chybí DEM", SEVERE, "{label_lc} (chybí výškový model)", REFRESH),
    (r"Výškové modely", SEVERE, "výškové modely (vrstevnice, srázy a porosty mohou chybět)", REFRESH),
    (r"DSM/CHM", SEVERE, "model výšky porostu (porosty jsou méně přesné)", REFRESH),
    (r"Vegetace", SEVERE, "porosty (vegetace)", REFRESH),
    (r"Srázy", SEVERE, "srázy", CONTACT),
    (r"Kupky a ďolíky", SEVERE, "kupky a ďolíky", CONTACT),
    (r"Balvany", SEVERE, "balvany", CONTACT),
    (r"Výstup:", SEVERE, "výsledný soubor s mapou", CONTACT),
    (r"OOM: proces mapy spadl", OTHER, "souběžný výpočet map (mapy se dopočítaly postupně)", NONE),
    (r"OOM: podklady pro šablony", MISSING, "podkladové vrstvy (šablony) v souboru mapy", CONTACT),
    (r"OOM: (?P<variant>.+?) nevytvořeno", SEVERE, "mapa {variant}", CONTACT),
    # --- chybí jen podklad / doplněk ---
    (r"OOM OCD", MISSING, "kopie mapy ve formátu OCAD (.ocd)", REPORT),
    (r"OOM georef", MISSING, "georeferencované náhledy mapy", REPORT),
    (r"OOM náhled|Náhled compose", MISSING, "náhledový obrázek mapy", REPORT),
    (r"uzitecne/", MISSING, "složka „uzitecne“ s doplňkovými soubory", REPORT),
    (r"Ortofoto", MISSING, "ortofoto", RETRY),
    (r"Katastr", MISSING, "katastrální mapa", RETRY),
    (r"OpenStreetMap:", MISSING, "podkladová vrstva OpenStreetMap", RETRY),
    (r"Mapa ZTM", MISSING, "Základní topografická mapa (ZTM)", RETRY),
    (r"(?P<label>DTM kraje[^:]*)", MISSING, "{label}", RETRY),
    (r"Náhled DMP OK", MISSING, "náhled povrchu (DMP OK)", RETRY),
    (r"Stínování reliéfu|Stínování: žádný zdroj", MISSING, "stínování reliéfu", RETRY),
    (r"Stínování|Hillshade", MISSING, "stínování reliéfu z ČÚZK", RETRY),
    (r"Referenční podklady", MISSING, "podkladové vrstvy (ortofoto, katastr, stínování…)", RETRY),
)

_COMPILED = tuple((re.compile(p), c, w, a) for p, c, w, a in _RULES)
_CONTEXT_PREFIX = re.compile(r"^\[[^\]]*\]\s*")


def explain_issue(text: str) -> Explained:
    """Zařadí jednu technickou hlášku (bez značky ``⚠``)."""
    technical = " ".join(str(text or "").split())
    core = _CONTEXT_PREFIX.sub("", technical)
    for rx, category, what, advice in _COMPILED:
        m = rx.match(core)
        if not m:
            continue
        label = core.split(":", 1)[0].strip()
        groups = {k: (v or "").strip() for k, v in m.groupdict().items()}
        groups.setdefault("label", label)
        groups["label_lc"] = groups["label"][:1].lower() + groups["label"][1:]
        return Explained(category, what.format(**groups), advice, technical)
    return Explained(OTHER, "", REPORT, technical)


def _unique(seq):
    out: list[str] = []
    for item in seq:
        if item and item not in out:
            out.append(item)
    return out


def user_summary(issues: list[str]) -> str:
    """Lidský souhrn chyb jobu pro kartu jobu (``jobs.error``)."""
    items = [explain_issue(t) for t in issues if str(t or "").strip()]
    if not items:
        return ""
    severe = _unique(e.what for e in items if e.category == SEVERE)
    missing = _unique(e.what for e in items if e.category == MISSING)
    parts: list[str] = []
    if severe:
        parts.append("Mapa je hotová, ale je neúplná – chybí: " + ", ".join(severe) + ".")
        if missing:
            parts.append("Chybí také podkladové vrstvy: " + ", ".join(missing) + ".")
    elif missing:
        parts.append(
            "Mapa je v pořádku, chybí jen některé podkladové vrstvy: "
            + ", ".join(missing)
            + "."
        )
    else:
        parts.append("Mapa je hotová, při zpracování nastaly drobné potíže bez vlivu na mapu.")
    advice = min((e.advice for e in items), key=_ADVICE_ORDER.index)
    if advice == REPORT and severe:
        advice = CONTACT
    if advice != NONE:
        parts.append(ADVICE_TEXT[advice])
    parts.append(DETAILS_HINT)
    return _clip(" ".join(parts))


# --- job úplně selhal ---------------------------------------------------

_FAILURE_RULES: tuple[tuple[str, str], ...] = (
    (
        r"časový limit",
        "Výpočet trval příliš dlouho a byl ukončen. Zkuste menší území, nebo job "
        f"spusťte znovu později. Pokud se to opakuje, napište Petrovi {FEEDBACK_HINT}.",
    ),
    (
        r"restartem serveru",
        "Výpočet přerušil restart serveru (například nasazení nové verze). "
        "Spusťte stejný job znovu.",
    ),
    (
        r"Server byl obsazený",
        "Server byl dlouho obsazený jinými výpočty. Spusťte job znovu později.",
    ),
    (
        r"nezůstaly žádné DMR|nezůstaly žádné DMP|žádné LiDAR|mimo listy",
        "Pro zvolené území se nepodařilo získat data leteckého skenování (LiDAR). "
        "Zkontrolujte, že výřez leží v Česku, a spusťte job znovu se zaškrtnutou "
        "volbou „Znovu stáhnout a spočítat podklady“. Pokud to nepomůže, napište "
        f"Petrovi {FEEDBACK_HINT}.",
    ),
    (
        r"ZABAGED|HTTP ?5\d\d|\b50[234]\b|timed? ?out|Timeout|Connection|"
        r"Max retries|Temporary failure|Service Unavailable|Bad Gateway",
        "Nepodařilo se stáhnout podklady od ČÚZK nebo jiné služby – nejspíš jde "
        "o dočasný výpadek. Spusťte stejný job znovu za pár hodin.",
    ),
    (
        r"kódem -9|kódem 137|MemoryError|Cannot allocate",
        "Výpočtu došla paměť serveru. Zkuste menší území nebo to zkuste později, "
        f"až server nebude vytížený. Pokud se to opakuje, napište Petrovi {FEEDBACK_HINT}.",
    ),
)
_FAILURE_COMPILED = tuple((re.compile(p), t) for p, t in _FAILURE_RULES)

FAILURE_DEFAULT = (
    "Při výpočtu nastala chyba Podkladárny, nový build sám nejspíš nepomůže. "
    f"Napište prosím Petrovi {FEEDBACK_HINT}."
)


def failure_summary(technical: str) -> str:
    """Lidská zpráva pro job, který skončil ``failed`` (technika zůstává v logu)."""
    text = " ".join(str(technical or "").split())
    advice = FAILURE_DEFAULT
    for rx, msg in _FAILURE_COMPILED:
        if rx.search(text):
            advice = msg
            break
    return _clip(f"Mapu se nepodařilo vytvořit. {advice} {DETAILS_HINT}")


def _clip(text: str) -> str:
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return text
