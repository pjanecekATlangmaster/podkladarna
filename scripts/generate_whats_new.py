#!/usr/bin/env python3
"""Vygeneruje configs/whats_new.yaml: pár recent změn + ruční milníky.

Recent = denní souhrny z git logu (jen důležité, česky).
Milníky = configs/whats_new_milestones.yaml (produktové zlomy).

Použití:
  python3 scripts/generate_whats_new.py
  python3 scripts/generate_whats_new.py --released-at 2026-09-16T18:00:00Z

V CI se spouští před Docker buildem. Soubor je součástí image; .git v image není.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "configs" / "whats_new.yaml"
MILESTONES_PATH = ROOT / "configs" / "whats_new_milestones.yaml"
# Okno pro sběr commitů; do YAML jde jen několik denních souhrnů.
ENTRY_DAYS = 21
MAX_RECENT_DAYS = 5
MAX_TITLES_PER_DAY = 2
# Recent jen z posledních N kalendářních dní (ne „5 dní s commity“ z měsíce).
RECENT_SPAN_DAYS = 10

# Commit subjecty / těla, které do boxu nepatří (dev, CI, verze, copy-nits).
_SKIP_SUBJECT = re.compile(
    r"(?i)^("
    r"merge\b|"
    r"wip\b|"
    r"tmp\b|"
    r"rule:\s*|"
    r"cursor:\s*|"
    r"apply local changes for cloud agent|"
    r"chore(\([^)]*\))?:\s*(deps|lock|gitignore|version|rules?)\b|"
    r"ci(\([^)]*\))?:"
    r"|bump\s+(the\s+)?(app\s+)?version\b"
    r"|refresh\s+whats_?new\b"
    r"|footer\s+verze\b"
    r"|tip\s*.{0,3}master\b"
    r"|fix tip.master\b"
    r")"
)
# I uvnitř subjectu – údržba, ne produkt pro uživatele mapy.
_SKIP_ANYWHERE = re.compile(
    r"(?i)("
    r"whats_?new|"
    r"mojibake|"
    r"APP_VERSION|"
    r"hardcoded\s+\d|"
    r"licence\s+<code>|"
    r"licence\s+citation|"
    r"Match licence\b|"
    r"Clarify \[rok\]|"
    r"obsolete uzitecne tests|"
    r"keep KP out of image|"
    r"Drop Karttapullautin runtime|"
    r"Ship OpenOrienteering Mapper CLI|"
    r"Fix about text\b|"
    r"Fix Czech:\s*V ZIPu|"
    r"user-facing guide copy|"
    r"drop redundant hint|"
    r"Prefer kupky\b|"
    r"Czech knolls label|"
    r"private.?mail.?retry|"
    r"private.?job.?email|"
    r"Raise Pillow georef|"
    r"Hide residual size|"
    r"Hide ostatní plocha|"
    r"Hide ostatni plocha|"
    r"Move force_refresh|"
    r"tip.?→.?master|tip.?->.?master|"
    r"\.cursor/rules|"
    r"doručovat změny na master|"
    r"dorucovat zmeny na master|"
    r"\bpytest\b|"
    r"Overpass QL test|"
    r"test after\b|"
    r"test when\b|"
    r"unit test|"
    r"fix tip CI\b"
    r")"
)
_SKIP_SHA_PARENTS = True  # merge commity (2+ rodiče) pryč


def _git(*args: str) -> str:
    # Windows default (cp1250/cp1252) rozbije UTF-8 subjecty z gitu → mojibake
    # v whats_new.yaml. Vždy dekóduj jako UTF-8.
    env = os.environ.copy()
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", "C.UTF-8")
    raw = subprocess.check_output(
        ["git", "-c", "i18n.logOutputEncoding=utf-8", *args],
        cwd=ROOT,
        stderr=subprocess.DEVNULL,
    )
    return raw.decode("utf-8")


def _is_merge(sha: str) -> bool:
    try:
        parents = _git("rev-list", "--parents", "-n", "1", sha).split()
    except subprocess.CalledProcessError:
        return False
    return len(parents) > 2


def _looks_czech(text: str) -> bool:
    if re.search(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]", text):
        return True
    # Jen výrazy typické pro češtinu (ne anglické „symbol“ / „map“).
    return bool(
        re.search(
            r"(?i)\b(oprava|přidán|nahrazen|úprava|vrstevnic|měřítko|"
            r"ekvidistanc|podklad|budov|cest[ay]|zabaged|ruian)\b",
            text,
        )
    )


# Známé subjecty → krátký český popis (pořadí: konkrétnější dřív).
_TITLE_CS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(?i)Courtyard fill: olive or the building symbol"),
        "Dvory v budovách: výplň olivou, nebo stejně jako budova",
    ),
    (
        re.compile(r"(?i)Drag handles to adjust the AOI"),
        "Výřez jde doladit: tažením za rohy, křížkem uprostřed se posouvá",
    ),
    (
        re.compile(r"(?i)Allow 6×6 km AOI again"),
        "Výřez zase až 6×6 km (36 km²)",
    ),
    (
        re.compile(r"(?i)Keep 6×6 km DEM in memory"),
        "Rychlejší měření výšky srázů na velkých výřezech",
    ),
    (
        re.compile(r"(?i)LiDAR pipeline: stream merges"),
        "Rychlejší LiDAR: proudový merge, souběžné kroky, velké výřezy nepadají na paměť",
    ),
    (
        re.compile(r"(?i)Speed up Python steps"),
        "Rychlejší srázy (7×), sestavení mapy (2×) a OSM z cache při opakování výřezu",
    ),
    (
        re.compile(r"(?i)Georef previews: fix Mapper"),
        "Georef náhledy znovu přes Mapper, záložní render 400× rychlejší",
    ),
    (
        re.compile(r"(?i)Clip OSM/ZABAGED/AOPK vectors"),
        "Mapa bez 155km přesahů z OSM – georef přes Mapper zase funguje",
    ),
    (
        re.compile(r"(?i)fill_small_holes via scipy"),
        "Vegetace: rychlejší vyplňování děr v loukách",
    ),
    (
        re.compile(r"(?i)Remove Karttapullautin leftovers|Lock sheet-crop cache"),
        "Úklid kódu po Karttapullautinu",
    ),
    (
        re.compile(r"(?i)generate whats-new|whats-new changelog|age-styled whats-new"),
        "Přehled novinek v boxu nad formulářem",
    ),
    (
        re.compile(r"(?i)simplify.?then.?curves|contour.*Bézier|contour.*Bezier|DP simplify.*curves"),
        "Vrstevnice: zjednodušení a pak hladké Bézier křivky",
    ),
    (
        re.compile(r"(?i)map-type preset|scale and contour select"),
        "Měřítko a ekvidistance místo typu mapy",
    ),
    (
        re.compile(r"(?i)short OSM bridges|footbridges"),
        "Krátké OSM lávky a mosty už se nezahazují",
    ),
    (
        re.compile(r"(?i)sprint OSM bridges|connecting path symbol|not 512\.1"),
        "Sprint: mosty jako navazující cesta (ne značka 512.1)",
    ),
    (
        re.compile(r"(?i)Lower brown above yellow|paved roads stay visible"),
        "Sprint: zpevněné cesty znovu vidět nad žlutou",
    ),
    (
        re.compile(r"(?i)paved-area symbol colors|Lower brown priority remapping"),
        "Oprava barev zpevněných ploch (Lower brown)",
    ),
    (
        re.compile(r"(?i)ISSprOM road footprints|OSM parking as paved"),
        "Sprint: footprinty ulic z klíče a OSM parkoviště jako 501",
    ),
    (
        re.compile(r"(?i)RÚIAN buildings|AOPK trees|manual doplnky"),
        "Budovy z RÚIAN, AOPK stromy a doplňky ZABAGED/OSM",
    ),
    (
        re.compile(r"(?i)color_map|ensure_isom_color|black vegetation"),
        "Oprava barev vegetace v MTBO omapu",
    ),
    (
        re.compile(r"(?i)symbol layering|401 vs ISOM|paved area"),
        "Oprava vrstev symbolů (401 / zpevněné plochy)",
    ),
    (
        re.compile(r"(?i)spot colors|importing ISOM symbols to MTBO"),
        "Oprava barev při importu ISOM symbolů do MTBO",
    ),
    (
        re.compile(r"(?i)nine-omap|9 omaps|discipline × path|path source"),
        "Více omapů podle disciplíny a zdroje cest",
    ),
    (
        re.compile(r"(?i)MTBO omap symbols|831-838|ISOM overlays"),
        "MTBO: správné cesty 831–838 a ISOM překryvy",
    ),
    (
        re.compile(r"(?i)Hrad and Zámek|Hrad and Zamek"),
        "ZABAGED Hrad a Zámek jako budovy",
    ),
    (
        re.compile(r"(?i)discipline symbol sets|drop path 507"),
        "Symbolové sady podle disciplíny, bez pěšiny 507",
    ),
    (
        re.compile(r"(?i)PATH_SOURCE_MIXED"),
        "Oprava importu zdroje cest (mix)",
    ),
    (
        re.compile(r"(?i)path source variants of \.omap|all path source"),
        "Tři varianty cest v omap souborech",
    ),
    (
        re.compile(r"(?i)remove scratch|_tmp_"),
        "Úklid dočasných souborů",
    ),
    (
        re.compile(
            r"(?i)omap files from the map title|purge old jobs on page load|"
            r"license outputs as CC BY"
        ),
        "Omap podle názvu mapy, úklid jobů při otevření webu a CC BY u výstupu",
    ),
    (
        re.compile(
            r"(?i)Drop large settlement paved|OstatniPlocha|paved areas that cover roads"
        ),
        "Velké Ostatní plochy v sídlech se zahazují (nepřekrývají silnice)",
    ),
    (
        re.compile(r"(?i)LiDAR return density|return-density vegetation"),
        "Vegetace bez KP z hustoty LiDAR odrazů",
    ),
    (
        re.compile(
            r"(?i)meadow under-detection|trees.*white forest|"
            r"bez-KP meadow"
        ),
        "Bez KP: louky pod stromy už nejsou bílý les",
    ),
    (
        re.compile(
            r"(?i)sidewalk.*(revert|line|506|834)|forest.?mtbo.?sidewalk|"
            r"chodník.*(linie|pěšin)|zpět.*(506|834|pěšin)|Revert.*sidewalk"
        ),
        "Les/MTBO: chodníky zase jako linie pěšiny (506 / 834)",
    ),
    (
        re.compile(
            r"(?i)sidewalk.*(strip|blowup|pás|blow)|chodník.*(pás|blow|obří)|"
            r"úzký pás 501"
        ),
        "Les/MTBO: chodníky jako úzký pás zpevněné plochy (ne obří 501.1)",
    ),
    (
        re.compile(
            r"(?i)CHM open|narrow meadow|úzk.*louk|chm_open|"
            r"zlom louka|meadow.?edge|louku až|Louka až"
        ),
        "Louky až k hraně lesa; zelená jen z hustoty odrazů",
    ),
    (
        re.compile(
            r"(?i)Mapper.*(AOI|ořez|orez)|ořez fialov|orez fialov|"
            r"zrušení deklinace|zruseni deklinace|undo.?grivation"
        ),
        "Webový náhled: ořez podle výběru, bez natočení deklinace",
    ),
    (
        re.compile(
            r"(?i)Web náhled přes Mapper|Web nahled pres Mapper|"
            r"Mapper \(JPEG\)|Mapper web preview"
        ),
        "Webový náhled mapy přes OpenOrienteering Mapper",
    ),
    (
        re.compile(
            r"(?i)earth.?bank.*50|104.?104|min.*sráz|overlapping earth|"
            r"kratš.*sráz"
        ),
        "Zemní srázy: min. ~50 m, kratší přes delší se zahazují",
    ),
    (
        re.compile(r"(?i)earth.?bank.*curve|sinuosity|turn.*filter|Tighten earth-bank"),
        "Zemní srázy: přísnější filtr křivosti",
    ),
    (
        re.compile(r"(?i)Formát combo|georef PNG/TIFF checkbox|opt-in georef"),
        "Georeferencovaný PNG/TIFF v ZIPu je volitelný",
    ),
    (
        re.compile(r"(?i)Residual paved|residential gaps"),
        "Doplnění zpevněných ploch v mezerách residential",
    ),
    (
        re.compile(r"(?i)footway.*sidewalk|Default footways"),
        "Sprint: footway jako zpevněný chodník (výchozí)",
    ),
    (
        re.compile(r"(?i)all-as-rock-face|rock.?face 201 cliff"),
        "Odstraněna volba „vše jako skála 201“",
    ),
    (
        re.compile(r"(?i)uzitecne|pouzite vs vyhozene|užitečné"),
        "Do ZIPu: vegetace/srázy/skály jako použité vs. vyhozené",
    ),
    (
        re.compile(
            r"(?i)fit.?content.*(?:běžíc|fronta|queue)|"
            r"panel.*(?:výšk|height).*obsah|"
            r"Běžící a fronta.*(?:výšk|obsah|scroll)|"
            r"live.?jobs.*fit"
        ),
        "Panel běžících jobů a fronty podle výšky obsahu",
    ),
    (
        re.compile(r"(?i)UI:.*panel|vyšší panel Běžící"),
        "Vyšší panel běžících jobů a fronty",
    ),
    (
        re.compile(r"(?i)Drop Karttapullautin|bez.?KP foundation|use_kp"),
        "Konec Karttapullautinu — vlastní vegetace, srázy a DEM",
    ),
    (
        re.compile(r"(?i)not ZABAGED meadows|CHM vegetation for bez-KP"),
        "Louky v auto-mapě z LiDARu, ne ze ZABAGED",
    ),
    (
        re.compile(r"(?i)private map jobs|SMTP download"),
        "Privátní joby: odkaz ke stažení e-mailem",
    ),
    (
        re.compile(r"(?i)ČÚZK WMS hillshade|Prefer.*hillshade"),
        "Hillshade z ČÚZK WMS místo lokálního výpočtu",
    ),
]

_PREFIX_CS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)^fix(\([^)]*\))?:\s*"), "Oprava: "),
    (re.compile(r"(?i)^feat(\([^)]*\))?:\s*"), "Novinka: "),
    (re.compile(r"(?i)^add(ed)?\b[:\s]*"), "Přidáno: "),
    (re.compile(r"(?i)^fix(ed|es|ing)?\b[:\s]*"), "Oprava: "),
    (re.compile(r"(?i)^replace\b[:\s]*"), "Úprava: "),
    (re.compile(r"(?i)^update\b[:\s]*"), "Aktualizace: "),
    (re.compile(r"(?i)^remove\b[:\s]*"), "Odstranění: "),
    (re.compile(r"(?i)^refactor(\([^)]*\))?:\s*"), "Refaktor: "),
    (re.compile(r"(?i)^docs(\([^)]*\))?:\s*"), "Dokumentace: "),
]


def to_czech_title(subject: str) -> str:
    """Převod commit subjectu do krátkého českého nadpisu pro box."""
    text = " ".join(subject.split()).strip().rstrip(".")
    if not text:
        return ""
    for pat, cs in _TITLE_CS:
        if pat.search(text):
            return cs
    if _looks_czech(text):
        text = re.sub(
            r"^(feat|fix|docs|refactor|perf|build|style)(\([^)]*\))?:\s*",
            "",
            text,
            flags=re.I,
        )
        return text[0].upper() + text[1:] if text else ""
    for pat, prefix in _PREFIX_CS:
        if pat.search(text):
            rest = pat.sub("", text).strip()
            if rest:
                rest = rest[0].upper() + rest[1:]
            return f"{prefix}{rest}".rstrip(": ").rstrip(".")
    # Obecný anglický subject – jemně označit jako změnu.
    cleaned = re.sub(
        r"^(feat|fix|docs|refactor|perf|build|style)(\([^)]*\))?:\s*",
        "",
        text,
        flags=re.I,
    )
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    return f"Změna: {cleaned}".rstrip(".")


def _clean_subject(subject: str) -> str:
    return to_czech_title(subject)


def _title_importance(title: str, *, mapped: bool) -> int:
    """Vyšší = důležitější pro denní souhrn."""
    score = 50 if mapped else 10
    t = title.casefold()
    if title.startswith("Změna:") or title.startswith("Oprava: Private"):
        score -= 30
    if title.startswith("Refaktor:") or title.startswith("Dokumentace:"):
        score -= 40
    if title.startswith("Úklid"):
        score -= 50
    # Produktové klíčová slova
    for word, bonus in (
        ("karttapullautin", 40),
        ("mapper", 35),
        ("náhled", 30),
        ("zabaged", 30),
        ("louk", 25),
        ("vegetac", 25),
        ("sráz", 35),
        ("50 m", 15),
        ("privát", 25),
        ("vrstevnic", 20),
        ("georef", 15),
        ("chodník", 15),
        ("rúian", 20),
        ("ruian", 20),
    ):
        if word in t:
            score += bonus
    return score


def _mapped_czech(subject: str) -> bool:
    text = " ".join(subject.split()).strip()
    return any(pat.search(text) for pat, _ in _TITLE_CS)


def collect_raw_entries(
    *,
    since_days: int = ENTRY_DAYS,
) -> list[dict[str, object]]:
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime(
        "%Y-%m-%d"
    )
    # %x1f field sep, %x1e record sep
    fmt = "%H%x1f%cI%x1f%s%x1f%b%x1e"
    try:
        raw = _git("log", f"--since={since}", f"--format={fmt}", "--no-merges")
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"git log selhal: {exc}") from exc

    entries: list[dict[str, object]] = []
    seen_titles: set[str] = set()
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split("\x1f", 3)
        if len(parts) < 3:
            continue
        sha, when, subject = parts[0], parts[1], parts[2]
        if _SKIP_SHA_PARENTS and _is_merge(sha):
            continue
        subject = " ".join(subject.split()).strip()
        if not subject or _SKIP_SUBJECT.search(subject) or _SKIP_ANYWHERE.search(subject):
            continue
        mapped = _mapped_czech(subject)
        title = _clean_subject(subject)
        if not title:
            continue
        if _SKIP_ANYWHERE.search(title):
            continue
        # Holé anglické „Změna: …“ bez mapování často = commit noise.
        if title.startswith("Změna:") and not mapped and not _looks_czech(subject):
            continue
        key = title.casefold()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        try:
            day = datetime.fromisoformat(when.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            day = when[:10]
        entries.append(
            {
                "date": day,
                "title": title,
                "body": "",
                "score": _title_importance(title, mapped=mapped),
            }
        )
    return entries


def _title_family(title: str) -> str:
    """Skupina pro dedup (např. všechny „Zemní srázy: …“)."""
    head = title.split(":", 1)[0].strip().casefold()
    if len(head) >= 8:
        return head
    return title.casefold()[:24]


def summarize_by_day(
    raw: list[dict[str, object]],
    *,
    max_days: int = MAX_RECENT_DAYS,
    max_per_day: int = MAX_TITLES_PER_DAY,
    span_days: int = RECENT_SPAN_DAYS,
) -> list[dict[str, str]]:
    """Jedna položka na den: až max_per_day nejdůležitějších titulků spojených středníkem."""
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=span_days)).isoformat()
    by_day: dict[str, list[dict[str, object]]] = defaultdict(list)
    for item in raw:
        day = str(item["date"])
        if day < cutoff:
            continue
        by_day[day].append(item)

    days = sorted(by_day.keys(), reverse=True)[:max_days]
    out: list[dict[str, str]] = []
    for day in days:
        ranked = sorted(
            by_day[day],
            key=lambda x: (-int(x["score"]), str(x["title"])),
        )
        # Vynechat dny jen s nízkým skóre (copy-nits), pokud je něco lepšího jinde.
        candidates = [x for x in ranked if int(x["score"]) >= 20] or ranked[:1]
        uniq: list[str] = []
        seen_families: set[str] = set()
        for x in candidates:
            t = str(x["title"])
            fam = _title_family(t)
            if fam in seen_families:
                continue
            if any(t.casefold()[:24] == u.casefold()[:24] for u in uniq):
                continue
            seen_families.add(fam)
            uniq.append(t)
            if len(uniq) >= max_per_day:
                break
        if not uniq:
            continue
        out.append({"date": day, "title": "; ".join(uniq), "body": ""})
    return out


def load_milestones(path: Path = MILESTONES_PATH) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("milestones") or []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        date_s = str(item.get("date") or "").strip()
        title = str(item.get("title") or "").strip()
        if not date_s or not title:
            continue
        out.append({"date": date_s[:10], "title": title, "body": ""})
    return out


def _milestone_keys(milestones: list[dict[str, str]]) -> set[str]:
    """Klíče pro dedup recent vs milníky (stejný příběh dvakrát ne)."""
    keys: set[str] = set()
    for m in milestones:
        t = m["title"].casefold()
        keys.add(t)
        # zkrácené jádro (prvních ~28 znaků)
        keys.add(t[:28])
    return keys


def _overlaps_milestone(title: str, keys: set[str]) -> bool:
    cf = title.casefold()
    if cf in keys or cf[:28] in keys:
        return True
    return any(cf[:20] == k[:20] for k in keys if len(k) >= 16)


def drop_milestone_duplicates(
    raw: list[dict[str, object]],
    milestones: list[dict[str, str]],
) -> list[dict[str, object]]:
    """Před denním souhrnem vyhodit titulky, které už jsou milníky."""
    keys = _milestone_keys(milestones)
    return [item for item in raw if not _overlaps_milestone(str(item["title"]), keys)]


def filter_recent_vs_milestones(
    recent: list[dict[str, str]],
    milestones: list[dict[str, str]],
) -> list[dict[str, str]]:
    keys = _milestone_keys(milestones)
    out: list[dict[str, str]] = []
    for item in recent:
        parts = [p.strip() for p in item["title"].split(";") if p.strip()]
        kept = [p for p in parts if not _overlaps_milestone(p, keys)]
        if not kept:
            continue
        out.append({**item, "title": "; ".join(kept)})
    return out


def collect_entries(
    *,
    since_days: int = ENTRY_DAYS,
    max_entries: int = MAX_RECENT_DAYS,
) -> list[dict[str, str]]:
    """Zpětná kompatibilita testů: recent denní souhrny (bez milníků)."""
    raw = collect_raw_entries(since_days=since_days)
    raw = drop_milestone_duplicates(raw, load_milestones())
    return summarize_by_day(raw, max_days=max_entries)


def render_yaml(
    released_at: str,
    entries: list[dict[str, str]],
    milestones: list[dict[str, str]] | None = None,
) -> str:
    lines = [
        "# AUTO-GENERATED – nepřepisovat ručně.",
        "# Zdroj: git log + configs/whats_new_milestones.yaml",
        "# (scripts/generate_whats_new.py), spouští CI před Docker buildem.",
        f"released_at: \"{released_at}\"",
        "entries:",
    ]
    if not entries:
        lines.append("  []")
    else:
        for item in entries:
            title = item["title"].replace('"', '\\"')
            lines.append(f"  - date: \"{item['date']}\"")
            lines.append(f"    title: \"{title}\"")
            body = item.get("body") or ""
            if body:
                esc = body.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f"    body: \"{esc}\"")
            else:
                lines.append('    body: ""')
    lines.append("milestones:")
    ms = milestones or []
    if not ms:
        lines.append("  []")
    else:
        for item in ms:
            title = item["title"].replace('"', '\\"')
            lines.append(f"  - date: \"{item['date']}\"")
            lines.append(f"    title: \"{title}\"")
            lines.append('    body: ""')
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--released-at",
        default="",
        help="ISO čas buildu (default: teď UTC)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT,
        help="Výstupní YAML",
    )
    args = parser.parse_args(argv)
    released = args.released_at.strip()
    if not released:
        released = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    milestones = load_milestones()
    raw = drop_milestone_duplicates(collect_raw_entries(), milestones)
    recent = summarize_by_day(raw)
    text = render_yaml(released, recent, milestones)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(
        f"Wrote {args.out} ({len(recent)} recent days, {len(milestones)} milestones, "
        f"released_at={released})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
