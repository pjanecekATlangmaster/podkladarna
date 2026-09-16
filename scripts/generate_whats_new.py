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
    r"chore(\([^)]*\))?:\s*(deps|lock|gitignore)|"
    r"ci(\([^)]*\))?:"
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


def _clean_subject(subject: str) -> str:
    text = " ".join(subject.split()).strip()
    text = re.sub(
        r"^(feat|fix|docs|refactor|perf|build|style)(\([^)]*\))?:\s*",
        "",
        text,
        flags=re.I,
    )
    if text:
        text = text[0].upper() + text[1:]
    return text


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
        body_lines = [
            ln.strip()
            for ln in body.splitlines()
            if ln.strip() and not ln.strip().startswith("Co-authored-by:")
        ]
        body_txt = " ".join(body_lines[:4]).strip()
        if len(body_txt) > 280:
            body_txt = body_txt[:277].rstrip() + "…"
        try:
            day = datetime.fromisoformat(when.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            day = when[:10]
        entries.append({"date": day, "title": title, "body": body_txt})
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
