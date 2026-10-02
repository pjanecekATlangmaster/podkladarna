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

# Commit subjecty / těla, které do boxu nepatří (začátek subjectu).
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
# Interní / testovací commity – stačí výskyt kdekoli v subjectu.
_SKIP_ANYWHERE = re.compile(
    r"(?i)("
    r"tip\s*[→\->]+\s*master\s+merge|"
    r"tip.?master merge|"
    r"private-mail retry test|"
    r"skip private mail retry when ZIP is missing|"
    r"ensure gdal_translate in Docker|"
    r"fix tip CI\b|"
    r"restore Drop KP package_oom"
    r")"
)
_SKIP_SHA_PARENTS = True  # merge commity (2+ rodiče) pryč


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
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
    # —— 2.0 / bez-KP + UI finale ——
    (
        re.compile(r"(?i)Drop Karttapullautin|without Karttapullautin|bez.?KP pipeline|KP runtime"),
        "Pipeline bez Karttapullautinu (vegetace a skály z LiDARu)",
    ),
    (
        re.compile(r"(?i)Formát combo|georef PNG/TIFF checkbox|opt-in georef"),
        "Volitelný georeferencovaný PNG/TIFF (ve výchozím stavu vypnuto)",
    ),
    (
        re.compile(r"(?i)force_refresh|AOI force-refresh|Hide force_refresh"),
        "Obnova mezipaměti podkladů jen v Pokročilých",
    ),
    (
        re.compile(r"(?i)residual size combo|Hide residual size"),
        "Velikost zpevněné plochy jen když je volba zapnutá",
    ),
    (
        re.compile(r"(?i)ostatn|as 403 option from the GUI|Hide.*403.*GUI"),
        "Z formuláře pryč volba „ostatní plocha jako 403“",
    ),
    (
        re.compile(r"(?i)Czech knolls|kupky over knolly|Malé kupky|knolls label \(109\)"),
        "Malé kupky (109) – česky a ve výchozím stavu zapnuto",
    ),
    (
        re.compile(r"(?i)footways as paved sidewalk|footway.*sidewalk|Default footways"),
        "Sprint: pěšiny defaultně jako zpevněný chodník",
    ),
    (
        re.compile(r"(?i)all-as-rock-face|all as 201|Vše jako skála 201"),
        "Odstraněna zastaralá volba „vše jako skála 201“",
    ),
    (
        re.compile(r"(?i)Mapper CLI in Docker|Ship OpenOrienteering Mapper CLI|keep KP out of image"),
        "Mapper CLI v Docker image (pro NAS / ostrý běh)",
    ),
    (
        re.compile(r"(?i)Drop Karttapullautin runtime|Drop Karttapullautin"),
        "Pipeline bez Karttapullautinu (vegetace a skály z LiDARu)",
    ),
    (
        re.compile(r"(?i)georef PNG/PGW download|Restore georef|georeferenced OOM preview"),
        "Georeferencované náhledy PNG+PGW ke stažení",
    ),
    (
        re.compile(r"(?i)Pillow georef|600 DPI-eq|georef fallback"),
        "Georef přes Pillow ve vyšším rozlišení (~600 DPI)",
    ),
    (
        re.compile(r"(?i)private (map )?jobs|SMTP download|private job email"),
        "Privátní joby s odkazem ke stažení e-mailem",
    ),
    (
        re.compile(r"(?i)uzitecne pouzite|Export veg/scarps/rocks"),
        "Vektorové vrstvy do složky užitečné (použité / vyhozené)",
    ),
    (
        re.compile(r"(?i)earth-bank min|tangled StupenSraz|min length to 35"),
        "Zemní srázy: delší minimum a filtr zamotaných StupenSraz",
    ),
    (
        re.compile(r"(?i)dense-contour scarp|dense contours drop"),
        "Husté vrstevnice: méně falešných srázů, jemnější skály",
    ),
    (
        re.compile(r"(?i)min-size go defaults|tip min-size"),
        "Úprava výchozích minimálních velikostí (104, skály, vegetace)",
    ),
    (
        re.compile(r"(?i)Suppress rocks on scarp|rock/scarp overlap|rock overlap"),
        "Skály nepřekrývají srázy a objekty",
    ),
    (
        re.compile(r"(?i)cultivated land 412|Pillow cultivated"),
        "Oprava černé výplně orné půdy (412) v Pillow náhledu",
    ),
    (
        re.compile(r"(?i)Tighten rock-area defaults|demote depression rocks"),
        "Přísnější výchozí skály a méně skal v depresích",
    ),
    (
        re.compile(r"(?i)Disable contour B.?zier|export OOM polylines"),
        "Vrstevnice bez Bézier křivek (menší omap)",
    ),
    (
        re.compile(r"(?i)Simplify then curves|Chaikin|contour.*B.?zier|Disable contour B"),
        "Vrstevnice: zjednodušení a hladší křivky v OOM",
    ),
    (
        re.compile(r"(?i)Split PNG products|web Pillow, georef Mapper"),
        "Oddělený webový náhled a georeferencovaný výstup",
    ),
    (
        re.compile(r"(?i)grivation|declination"),
        "Magnetická deklinace jen u georeferencovaného PNG",
    ),
    (
        re.compile(r"(?i)Generování spuštěno|light-red"),
        "Po odeslání jobu výraznější stav „Generování spuštěno“",
    ),
    (
        re.compile(r"(?i)Leaflet bbox|resizing the Leaflet"),
        "Mapu výřezu lze výškově zvětšit",
    ),
    (
        re.compile(r"(?i)paved OSM tracks to ISOM 503"),
        "Zpevněné OSM tracks jako ISOM 503 (ne 504)",
    ),
    (
        re.compile(r"(?i)standalone rock 201|Discard standalone rock"),
        "Samostatné linie skály 201 se zahazují (zůstávají plochy)",
    ),
    (
        re.compile(r"(?i)CHM vegetation|vegetation_chm|Retune bez-KP CHM"),
        "Vegetace bez KP: doladěné prahy CHM (louky vs. les)",
    ),
    (
        re.compile(r"(?i)generate whats-new|whats-new changelog|age-styled whats-new"),
        "Přehled novinek v boxu nad formulářem",
    ),
    (
        re.compile(r"(?i)simplify.?then.?curves|contour.*Bézier|contour.*Bezier|DP simplify.*curves"),
        "Vrstevnice: zjednodušení a pak hladké Bézier křivky (bez bloatu)",
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
        if not subject or _SKIP_SUBJECT.search(subject) or _SKIP_ANYWHERE.search(subject):
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
