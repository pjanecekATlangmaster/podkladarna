#!/usr/bin/env python3
"""Vygeneruje configs/whats_new.yaml z git historie (posledních 30 dní).

Použití:
  python3 scripts/generate_whats_new.py
  python3 scripts/generate_whats_new.py --released-at 2026-09-16T18:00:00Z

V CI se spouští před Docker buildem. Soubor je součástí image; .git v image není.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "configs" / "whats_new.yaml"
ENTRY_DAYS = 30
MAX_ENTRIES = 20

# Commit subjecty / těla, které do boxu nepatří.
_SKIP_SUBJECT = re.compile(
    r"(?i)^("
    r"merge\b|"
    r"wip\b|"
    r"tmp\b|"
    r"cursor:\s*apply local changes|"
    r"apply local changes for cloud agent|"
    r"chore(\([^)]*\))?:\s*(deps|lock|gitignore|version)\b|"
    r"ci(\([^)]*\))?:"
    r"|bump\s+(the\s+)?(app\s+)?version\b"
    r")"
)
_SKIP_SHA_PARENTS = True  # merge commity (2+ rodiče) pryč


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=ROOT,
        text=True,
        stderr=subprocess.DEVNULL,
    )


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
        re.compile(r"(?i)generate whats-new|whats-new changelog|age-styled whats-new"),
        "Přehled novinek v boxu nad formulářem",
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

def collect_entries(
    *,
    since_days: int = ENTRY_DAYS,
    max_entries: int = MAX_ENTRIES,
) -> list[dict[str, str]]:
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime(
        "%Y-%m-%d"
    )
    # %x1f field sep, %x1e record sep
    fmt = "%H%x1f%cI%x1f%s%x1f%b%x1e"
    try:
        raw = _git("log", f"--since={since}", f"--format={fmt}", "--no-merges")
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"git log selhal: {exc}") from exc

    entries: list[dict[str, str]] = []
    seen_titles: set[str] = set()
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split("\x1f", 3)
        if len(parts) < 3:
            continue
        sha, when, subject = parts[0], parts[1], parts[2]
        body = parts[3] if len(parts) > 3 else ""
        if _SKIP_SHA_PARENTS and _is_merge(sha):
            continue
        subject = " ".join(subject.split()).strip()
        if not subject or _SKIP_SUBJECT.search(subject):
            continue
        title = _clean_subject(subject)
        if not title:
            continue
        key = title.casefold()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        # Do boxu stačí český nadpis; anglická těla commitů vynecháme.
        try:
            day = datetime.fromisoformat(when.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            day = when[:10]
        entries.append({"date": day, "title": title, "body": ""})
        if len(entries) >= max_entries:
            break
    return entries


def render_yaml(released_at: str, entries: list[dict[str, str]]) -> str:
    lines = [
        "# AUTO-GENERATED – nepřepisovat ručně.",
        "# Zdroj: git log (scripts/generate_whats_new.py), spouští CI před Docker buildem.",
        f"released_at: \"{released_at}\"",
        "entries:",
    ]
    if not entries:
        lines.append("  []")
        return "\n".join(lines) + "\n"
    for item in entries:
        title = item["title"].replace('"', '\\"')
        lines.append(f"  - date: \"{item['date']}\"")
        lines.append(f"    title: \"{title}\"")
        body = item.get("body") or ""
        if body:
            # Jednořádkový body v uvozovkách (escape).
            esc = body.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f"    body: \"{esc}\"")
        else:
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
    entries = collect_entries()
    text = render_yaml(released, entries)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"Wrote {args.out} ({len(entries)} entries, released_at={released})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
