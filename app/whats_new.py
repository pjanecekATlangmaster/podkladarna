"""Changelog / „co je nového“ pro box nad formulářem jobu."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.settings import APP_ROOT, APP_VERSION, CONFIG_DIR

WHATS_NEW_PATH = CONFIG_DIR / "whats_new.yaml"
ENTRY_DAYS = 30
# < 6 h → hot (červená, rozbaleno); < 2 d → warm; < 7 d → mild; jinak calm (zelená, sbaleno)
HOT_HOURS = 6
WARM_HOURS = 48
MILD_HOURS = 24 * 7


def _parse_dt(raw: object) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, date) and not isinstance(raw, datetime):
        dt = datetime(raw.year, raw.month, raw.day, 12, 0, 0, tzinfo=timezone.utc)
    else:
        text = str(raw).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                d = date.fromisoformat(text[:10])
            except ValueError:
                return None
            dt = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@lru_cache(maxsize=1)
def _load_raw(mtime_ns: int) -> dict[str, Any]:
    del mtime_ns
    if not WHATS_NEW_PATH.is_file():
        return {}
    data = yaml.safe_load(WHATS_NEW_PATH.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def load_whats_new_file() -> dict[str, Any]:
    if not WHATS_NEW_PATH.is_file():
        return {}
    st = WHATS_NEW_PATH.stat()
    return _load_raw(st.st_mtime_ns)


def resolve_released_at(now: datetime | None = None) -> datetime:
    """Čas posledního nasazení / buildu (UTC)."""
    del now
    env = os.environ.get("PODKLADARNA_BUILT_AT") or os.environ.get("BUILD_DATE")
    parsed = _parse_dt(env)
    if parsed:
        return parsed

    data = load_whats_new_file()
    parsed = _parse_dt(data.get("released_at"))
    if parsed:
        return parsed

    # Dev fallback: čas souboru whats_new / app
    for path in (WHATS_NEW_PATH, APP_ROOT / "app" / "settings.py"):
        if path.is_file():
            return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _tone_for_age(age: timedelta) -> dict[str, Any]:
    hours = age.total_seconds() / 3600.0
    if hours < 0:
        hours = 0.0
    if hours < HOT_HOURS:
        return {
            "tone": "hot",
            "open": True,
            "label": "Právě nasazeno",
        }
    if hours < WARM_HOURS:
        return {
            "tone": "warm",
            "open": True,
            "label": "Nedávná aktualizace",
        }
    if hours < MILD_HOURS:
        return {
            "tone": "mild",
            "open": False,
            "label": "Aktualizace tento týden",
        }
    return {
        "tone": "calm",
        "open": False,
        "label": "Stabilní verze",
    }


def _format_age_cs(age: timedelta) -> str:
    seconds = max(0, int(age.total_seconds()))
    if seconds < 3600:
        mins = max(1, seconds // 60)
        return f"před {mins} min"
    hours = seconds // 3600
    if hours < 48:
        return f"před {hours} h"
    days = seconds // 86400
    if days == 1:
        return "před 1 dnem"
    if days < 7:
        return f"před {days} dny"
    weeks = days // 7
    if weeks == 1:
        return "před 1 týdnem"
    if days < 45:
        return f"před {weeks} týdny"
    return f"před {days} dny"


def whats_new_payload(now: datetime | None = None) -> dict[str, Any]:
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    released = resolve_released_at(now_utc)
    age = now_utc - released
    tone = _tone_for_age(age)

    data = load_whats_new_file()
    cutoff = (now_utc - timedelta(days=ENTRY_DAYS)).date()
    entries_out: list[dict[str, str]] = []
    raw_entries = data.get("entries") or []
    if isinstance(raw_entries, list):
        for item in raw_entries:
            if not isinstance(item, dict):
                continue
            d = _parse_dt(item.get("date"))
            if d is None:
                continue
            if d.date() < cutoff:
                continue
            title = str(item.get("title") or "").strip()
            body = " ".join(str(item.get("body") or "").split()).strip()
            if not title:
                continue
            entries_out.append(
                {
                    "date": d.date().isoformat(),
                    "title": title,
                    "body": body,
                }
            )

    return {
        "version": APP_VERSION,
        "released_at": released.isoformat(),
        "age_hours": round(age.total_seconds() / 3600.0, 2),
        "age_label": _format_age_cs(age),
        "tone": tone["tone"],
        "open": tone["open"],
        "label": tone["label"],
        "entries": entries_out,
        "entry_days": ENTRY_DAYS,
    }
