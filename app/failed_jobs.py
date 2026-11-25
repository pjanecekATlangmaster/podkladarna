"""Nepovedené joby pro denní kontrolu (``GET /api/ops/failed_jobs``, Petr 2026-11-25).

Denní kontrola v cloudu (``scripts/source_daily_check.py``) seskupuje selhané joby podle příčiny a navrhuje
opravy. Veřejný seznam jobů privátní joby ani joby druhé domény neukazuje, proto tento výpis za tokenem
``SOURCE_EVENTS_TOKEN`` (stejný jako deník zdrojů). Bez osobních údajů: žádný název jobu, e-mail, IP ani
jméno API klienta; z logu jen chybové řádky a konec (řádky se ``@`` vynechané, název jobu nahrazený).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db

FAILED_STATUSES = ("failed", "done_with_errors")
LOG_TAIL_LINES = 15
LINE_MAX_CHARS = 600
TRACEBACK_MAX_CHARS = 4000
MAX_JOBS = 200
ERROR_PREFIXES = ("CHYBA", "ERROR", "⚠", "Traceback")


def _parse(iso: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clean(text: str, name: str) -> str | None:
    if "@" in text:
        return None
    if name and len(name) >= 3:
        text = text.replace(name, "‹název›")
    return text


def extract(lines: list[str], name: str = "") -> tuple[list[str], str | None]:
    """Chybové řádky + konec logu (oříznuté) a poslední traceback; čistá funkce (i pro denní kontrolu)."""
    traceback = None
    picked: list[str] = []
    for idx, line in enumerate(lines):
        if line.startswith("Traceback"):
            traceback = line
            continue
        if line.startswith(ERROR_PREFIXES) or idx >= len(lines) - LOG_TAIL_LINES:
            clean = _clean(line, name)
            if clean is not None and clean[:LINE_MAX_CHARS] not in picked:
                picked.append(clean[:LINE_MAX_CHARS])
    if traceback:
        traceback = _clean(traceback, name)
        traceback = traceback[-TRACEBACK_MAX_CHARS:] if traceback else None
    return picked[-40:], traceback


def _log_extract(job_id: str, name: str) -> tuple[list[str], str | None]:
    try:
        lines = [str(r.get("line") or "") for r in db.get_logs(job_id)]
    except Exception:  # noqa: BLE001 – log je doplněk
        return [], None
    return extract(lines, name)


def _site(opts: dict) -> str:
    host = str(opts.get("site") or "").lower()
    return "omapic" if ("ulvi" in host or "omapic" in host or str(opts.get("lang") or "cs") != "cs") else "podkladarna"


def report(hours: float = 24) -> dict:
    hours = max(1.0, min(float(hours), 24 * 14))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for job in db.list_jobs(limit=2000):
        if job.get("status") not in FAILED_STATUSES:
            continue
        when = _parse(job.get("updated_at") or job.get("created_at"))
        if when is None or when < since:
            continue
        opts = job.get("options") if isinstance(job.get("options"), dict) else {}
        name = str(job.get("name") or "")
        lines, traceback = _log_extract(job["id"], name)
        error = _clean(str(job.get("error") or ""), name) or ""
        out.append({
            "id": job["id"],
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "status": job.get("status"),
            "preset_id": job.get("preset_id"),
            "country": opts.get("country") or "CZ",
            "site": _site(opts),
            "private": db.job_is_private(job),
            "sample": bool(opts.get("sample")),
            "user_sources": bool(opts.get("user_sources")),
            "reused_from": bool(opts.get("reused_from")),
            "source_outage": opts.get("source_outage"),
            "area_km2": job.get("area_km2"),
            "bbox_wgs84": opts.get("bbox_wgs84"),
            "error": error[:LINE_MAX_CHARS],
            "log": lines,
            "traceback": traceback,
        })
        if len(out) >= MAX_JOBS:
            break
    return {"since": since.replace(microsecond=0).isoformat(), "hours": hours, "jobs": out}
