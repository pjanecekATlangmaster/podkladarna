from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.pipeline.job_options import (
    resolve_contour_smooth,
    resolve_map_types,
    resolve_formline_mode,
    resolve_osm_objects_mode,
    resolve_use_dtm,
    resolve_veg_smooth,
)
from app.settings import (
    DB_PATH,
    DOWNLOADS_DIR,
    JOBS_DIR,
    LIDAR_CACHE_MAX_AGE_DAYS,
    ESTIMATE_CALIB_DAYS,
    ESTIMATE_CALIB_JOBS,
    ESTIMATE_CALIB_MAX,
    ESTIMATE_CALIB_MIN,
    ESTIMATE_CALIB_MIN_JOBS,
    ESTIMATE_CALIB_QUANTILE,
    ESTIMATE_CALIB_RESERVE,
    ESTIMATE_CALIBRATION,
    ORIGIN_RETENTION_DAYS,
    PRIVATE_JOB_RETENTION_HOURS,
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_download_token() -> str:
    """Neprůhledný token pro privátní stažení (URL-safe)."""
    return secrets.token_urlsafe(32)


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                preset_id TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT,
                options_json TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                line TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
            """
        )
        cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        if "started_at" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN started_at TEXT")
        if "download_token" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN download_token TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_download_token "
            "ON jobs(download_token) WHERE download_token IS NOT NULL"
        )
        # Interní statistika původu jobů: přežije smazání jobu, maže se až po
        # ORIGIN_RETENTION_DAYS. API ani web ji nečtou (jen app.origin_stats na NAS).
        # IP a e-mail jen jako neprůhledné značky (HMAC s tajnou solí instance):
        # stejný zdroj = stejná značka, zpět je přečíst nejde.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_origin (
                job_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                ip_tag TEXT,
                ip_net_tag TEXT,
                api_client INTEGER NOT NULL DEFAULT 0,
                private INTEGER NOT NULL DEFAULT 0,
                email_tag TEXT,
                preset_id TEXT,
                map_types TEXT,
                bbox_json TEXT,
                status TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_origin_created ON job_origin(created_at)"
        )
        # Souhrnná statistika použití (3.25.0): údaje dokončeného jobu v job_origin
        # (přidané sloupce, NULL / 0 u starších řádků) a nikdy nemazané součty
        # v usage_totals. Jen čísla, žádné osobní údaje; starší image nové sloupce ignoruje.
        origin_cols = {r[1] for r in conn.execute("PRAGMA table_info(job_origin)").fetchall()}
        for col, decl in (
            ("usage_counted", "INTEGER NOT NULL DEFAULT 0"),
            ("build_seconds", "REAL"),
            ("output_bytes", "INTEGER"),
            ("source_bytes", "INTEGER"),
            ("downloads", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in origin_cols:
                conn.execute(f"ALTER TABLE job_origin ADD COLUMN {col} {decl}")
        # Samokalibrace odhadu (3.34.0): odhad vzorce při založení jobu a skutečná doba skládání map
        # (NULL u starších řádků a u jobů s převzatým LiDARem); starší image sloupce ignoruje.
        for col in ("est_prep_min", "est_maps_min", "maps_seconds"):
            if col not in origin_cols:
                conn.execute(f"ALTER TABLE job_origin ADD COLUMN {col} REAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_totals (
                key TEXT PRIMARY KEY,
                value REAL NOT NULL DEFAULT 0,
                since TEXT NOT NULL
            )
            """
        )
        # Joby založené před zavedením statistiky (dosud nesmazané) doplnit jednou.
        missing = conn.execute(
            "SELECT id, preset_id, options_json, created_at FROM jobs "
            "WHERE id NOT IN (SELECT job_id FROM job_origin)"
        ).fetchall()
        for job_id, preset_id, options_json, created_at in missing:
            try:
                opts = json.loads(options_json or "{}")
            except ValueError:
                opts = {}
            _record_origin(conn, job_id, created_at, preset_id, opts, with_estimate=False)
        conn.commit()
        # Plocha map (3.32.0) dřív započítaných jobů – před započítáním dalších, ať se nevynechají.
        _backfill_area_usage(conn)
        # Dokončené joby, které se do součtů ještě nezapočítaly (nasazení statistiky,
        # návrat ze starší image): započítat jednou, údaje z build_stats.json, je-li job na disku.
        pending = conn.execute(
            "SELECT o.job_id, COALESCE(j.status, o.status) AS status FROM job_origin o "
            "LEFT JOIN jobs j ON j.id = o.job_id WHERE o.usage_counted = 0"
        ).fetchall()
        for job_id, status in pending:
            if status in JOB_FINISHED_STATUSES:
                _count_job_usage(conn, job_id, status, _read_build_stats(JOBS_DIR / job_id))
        _backfill_build_seconds(conn)
        conn.commit()


@contextmanager
def connect():
    # Joby logují i z vláken na pozadí (DTM, ortofoto): čekat na zámek déle než výchozích 5 s.
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def create_job(name: str, preset_id: str, options: dict[str, Any]) -> dict[str, Any]:
    job_id = uuid.uuid4().hex[:12]
    now = _utcnow()
    job_dir = JOBS_DIR / job_id
    for sub in ("work", "output"):
        (job_dir / sub).mkdir(parents=True, exist_ok=True)

    opts = dict(options)
    is_private = bool(opts.get("private"))
    token = new_download_token() if is_private else None
    if is_private:
        opts["private"] = True
        email = str(opts.get("notify_email") or "").strip()
        if email:
            opts["notify_email"] = email

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, name, preset_id, status, phase, options_json, error,
                created_at, updated_at, started_at, download_token
            )
            VALUES (?, ?, ?, 'pending', NULL, ?, NULL, ?, ?, NULL, ?)
            """,
            (job_id, name, preset_id, json.dumps(opts), now, now, token),
        )
        _record_origin(conn, job_id, now, preset_id, opts)
        conn.commit()
    return get_job(job_id)


def _origin_salt() -> bytes:
    """Tajná sůl značek původu; vznikne při prvním použití vedle databáze."""
    path = DB_PATH.parent / "origin_salt"
    try:
        salt = path.read_bytes()
    except FileNotFoundError:
        salt = b""
    if len(salt) < 32:
        salt = secrets.token_bytes(32)
        path.write_bytes(salt)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return salt


def _origin_tag(kind: str, value: str) -> str | None:
    """Krátká neprůhledná značka (HMAC-SHA256, 12 hex); prázdná hodnota → None."""
    value = (value or "").strip().casefold()
    if not value or value == "unknown":
        return None
    digest = hmac.new(_origin_salt(), f"{kind}:{value}".encode(), hashlib.sha256)
    return digest.hexdigest()[:12]


def _ip_network(ip: str) -> str:
    """Síť klienta (IPv4 /24, IPv6 /48) – jen jako vstup značky, neukládá se."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return ""
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


def _record_origin(
    conn: sqlite3.Connection,
    job_id: str,
    now: str,
    preset_id: str,
    opts: dict[str, Any],
    *,
    with_estimate: bool = True,
) -> None:
    """Zapíše původ jobu do interní statistiky (IP a e-mail jen jako značky)."""
    private = bool(opts.get("private"))
    ip = str(opts.get("client_ip") or "")
    map_types = opts.get("map_types")
    est_prep = est_maps = None
    if with_estimate and not opts.get("reused_from"):
        try:
            from app.pipeline.fetch_openzu import estimate_job_parts

            area = job_area_km2(opts)
            if area is not None:
                est_prep, est_maps = estimate_job_parts(opts, area)
        except Exception:  # noqa: BLE001 – odhad je jen pro kalibraci, job kvůli němu nespadne
            est_prep = est_maps = None
    conn.execute(
        """
        INSERT OR REPLACE INTO job_origin (
            job_id, created_at, ip_tag, ip_net_tag, api_client, private, email_tag,
            preset_id, map_types, bbox_json, status, est_prep_min, est_maps_min
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            job_id,
            now,
            _origin_tag("ip", ip),
            _origin_tag("net", _ip_network(ip)),
            1 if opts.get("api_client") else 0,
            1 if private else 0,
            _origin_tag("email", str(opts.get("notify_email") or "")) if private else None,
            preset_id,
            ",".join(map_types) if isinstance(map_types, (list, tuple)) else None,
            json.dumps(opts["bbox_wgs84"]) if opts.get("bbox_wgs84") else None,
            est_prep,
            est_maps,
        ),
    )


USAGE_KEYS = (
    "jobs_done",  # dokončená zadání (done / done_with_errors)
    "maps_done",  # mapy disciplín v nich (les, MTBO, sprint)
    "jobs_failed",
    "build_seconds",  # celá doba běhu (start → konec) dokončených i spadlých jobů
    "output_bytes",  # velikost výstupních ZIPů dokončených jobů
    "source_bytes",  # zpracované podklady (stažené + z cache)
    "download_count",  # stažení výstupního ZIPu
    "download_bytes",
    "area_km2",  # plocha map dokončených jobů (výřez bbox_wgs84)
)


def _read_build_stats(job_dir: Path) -> dict[str, Any] | None:
    path = job_dir / "work" / "build_stats.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _add_usage(conn: sqlite3.Connection, key: str, amount: float, since: str | None = None) -> None:
    conn.execute(
        "INSERT INTO usage_totals (key, value, since) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = value + excluded.value, "
        "since = MIN(since, excluded.since)",
        (key, float(amount), since or _utcnow()),
    )


def _num(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _count_job_usage(
    conn: sqlite3.Connection, job_id: str, status: str, stats: dict[str, Any] | None
) -> bool:
    """Započítá dokončený job do součtů právě jednou (``usage_counted``); vrací True, když započítal."""
    row = conn.execute(
        "SELECT created_at, map_types, bbox_json FROM job_origin "
        "WHERE job_id = ? AND usage_counted = 0",
        (job_id,),
    ).fetchone()
    if not row:
        return False
    created_at, map_types, bbox_json = row[0], row[1], row[2]
    stats = stats or {}
    seconds = _num(stats.get("total_seconds"))
    if seconds is None:
        # Bez build_stats.json (timeout, restart, starší image): celá doba běhu z jobs.
        seconds = _job_run_seconds(conn, job_id)
    out_bytes = _num(stats.get("output_zip_bytes"))
    src = _num(stats.get("downloaded_bytes"))
    cached = _num(stats.get("cached_bytes"))
    src_bytes = None if src is None and cached is None else (src or 0) + (cached or 0)
    # Skládání map = součet kroků od DTM po ZIP (starší joby: jeden krok „sestavení mapy a ZIP“).
    from app.pipeline.job_progress import MAP_ASSEMBLY_STEPS

    maps_seconds = None
    steps = stats.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict) and step.get("label") in MAP_ASSEMBLY_STEPS:
                sec = _num(step.get("seconds"))
                if sec is not None:
                    maps_seconds = (maps_seconds or 0) + sec
    cur = conn.execute(
        "UPDATE job_origin SET usage_counted = 1, status = ?, build_seconds = ?, "
        "output_bytes = ?, source_bytes = ?, maps_seconds = ? "
        "WHERE job_id = ? AND usage_counted = 0",
        (status, seconds, out_bytes, src_bytes, maps_seconds, job_id),
    )
    if cur.rowcount != 1:
        return False
    if status in JOB_DONE_STATUSES:
        maps = stats.get("maps")
        if isinstance(maps, list) and maps:
            n_maps = len(maps)
        else:
            n_maps = len([t for t in (map_types or "").split(",") if t]) or 1
        _add_usage(conn, "jobs_done", 1, created_at)
        _add_usage(conn, "maps_done", n_maps, created_at)
        if out_bytes is not None:
            _add_usage(conn, "output_bytes", out_bytes)
        area = _bbox_json_area_km2(bbox_json)
        if area is not None:
            _add_usage(conn, "area_km2", area, created_at)
    else:
        _add_usage(conn, "jobs_failed", 1, created_at)
    if seconds is not None:
        _add_usage(conn, "build_seconds", seconds, created_at)
    if src_bytes is not None:
        _add_usage(conn, "source_bytes", src_bytes)
    return True


def _job_run_seconds(conn: sqlite3.Connection, job_id: str) -> float | None:
    """Celá doba běhu jobu (start → poslední změna stavu) z tabulky jobs, je-li job ještě v DB."""
    row = conn.execute(
        "SELECT started_at, updated_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if not row:
        return None
    started, ended = _parse_iso(row[0]), _parse_iso(row[1])
    if not started or not ended or ended < started:
        return None
    return round((ended - started).total_seconds(), 1)


def _backfill_build_seconds(conn: sqlite3.Connection) -> None:
    """Jednou (3.32.2): doplnit dobu zpracování jobů započítaných bez času.

    Do 3.32.1 se čas bral jen z build_stats.json; joby smazané před nasazením
    statistiky (3.25.0), zabité timeoutem nebo restartem se započítaly s nulou.
    Kde je job ještě v DB, vezme se celá doba běhu, jinak odhad = medián
    změřených jobů téhož presetu (jinak všech). Do job_origin se odhad nepíše,
    průměr za rok zůstává jen ze změřených jobů.
    """
    if conn.execute(
        "SELECT 1 FROM usage_totals WHERE key = 'build_seconds_backfilled'"
    ).fetchone():
        return
    measured = conn.execute(
        "SELECT preset_id, build_seconds FROM job_origin "
        "WHERE usage_counted = 1 AND build_seconds IS NOT NULL"
    ).fetchall()
    by_preset: dict[str, list[float]] = {}
    for preset_id, secs in measured:
        by_preset.setdefault(preset_id or "", []).append(float(secs))
    everything = sorted(v for vals in by_preset.values() for v in vals)

    def median(vals: list[float]) -> float | None:
        vals = sorted(vals)
        if not vals:
            return None
        mid = len(vals) // 2
        return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2

    missing = conn.execute(
        "SELECT job_id, created_at, preset_id FROM job_origin "
        "WHERE usage_counted = 1 AND build_seconds IS NULL"
    ).fetchall()
    total, estimated, since = 0.0, 0, None
    for job_id, created_at, preset_id in missing:
        secs = _job_run_seconds(conn, job_id)
        if secs is not None:
            conn.execute(
                "UPDATE job_origin SET build_seconds = ? WHERE job_id = ?", (secs, job_id)
            )
        else:
            secs = median(by_preset.get(preset_id or "", [])) or median(everything)
            if secs is None:
                continue
            estimated += 1
        total += secs
        since = created_at if since is None else min(since, created_at)
    if total > 0:
        _add_usage(conn, "build_seconds", total, since)
    _add_usage(conn, "build_seconds_backfilled", estimated)


def _bbox_json_area_km2(bbox_json: str | None) -> float | None:
    try:
        bbox = json.loads(bbox_json) if bbox_json else None
    except ValueError:
        return None
    return job_area_km2({"bbox_wgs84": bbox}) if isinstance(bbox, list) else None


def _backfill_area_usage(conn: sqlite3.Connection) -> None:
    """Plocha map se sčítá od 3.32.0: jednou dopočítat z dosud uložených řádků job_origin."""
    if conn.execute("SELECT 1 FROM usage_totals WHERE key = 'area_km2'").fetchone():
        return
    rows = conn.execute(
        "SELECT created_at, bbox_json FROM job_origin WHERE usage_counted = 1 AND status IN ("
        + ",".join("?" * len(JOB_DONE_STATUSES))
        + ")",
        tuple(JOB_DONE_STATUSES),
    ).fetchall()
    total, since = 0.0, None
    for created_at, bbox_json in rows:
        area = _bbox_json_area_km2(bbox_json)
        if area is not None:
            total += area
            since = created_at if since is None else min(since, created_at)
    if since is not None:
        _add_usage(conn, "area_km2", total, since)


def record_job_usage(job_id: str, status: str, stats: dict[str, Any] | None = None) -> bool:
    """Konec jobu: započítá ho do souhrnné statistiky použití (opakované volání nic nepřičte)."""
    if stats is None:
        stats = _read_build_stats(JOBS_DIR / job_id)
    with connect() as conn:
        counted = _count_job_usage(conn, job_id, status, stats)
        conn.commit()
    return counted


_CALIB_TTL_S = 600
_calib_cache: tuple[float, dict[str, Any]] | None = None


def reset_estimate_calibration_cache() -> None:
    global _calib_cache
    _calib_cache = None


def _quantile(values: list[float], q: float) -> float:
    xs = sorted(values)
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _calib_factor(ratios: list[float]) -> float:
    if len(ratios) < ESTIMATE_CALIB_MIN_JOBS:
        return 1.0
    return min(
        ESTIMATE_CALIB_MAX,
        max(ESTIMATE_CALIB_MIN, _quantile(ratios, ESTIMATE_CALIB_QUANTILE)) * ESTIMATE_CALIB_RESERVE,
    )


def estimate_calibration(*, now: datetime | None = None) -> dict[str, Any]:
    """Koeficienty odhadu doby jobu z vlastních dokončených jobů instance: ``{"prep", "maps", "jobs"}``.

    Výsledek se 10 min drží v paměti procesu; s ``now`` (testy) se cache obchází.
    """
    global _calib_cache
    if not ESTIMATE_CALIBRATION:
        return {"prep": 1.0, "maps": 1.0, "jobs": 0}
    mono = time.monotonic()
    if now is None and _calib_cache and mono - _calib_cache[0] < _CALIB_TTL_S:
        return dict(_calib_cache[1])
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=ESTIMATE_CALIB_DAYS)).isoformat()
    marks = ",".join("?" * len(JOB_DONE_STATUSES))
    with connect() as conn:
        rows = conn.execute(
            "SELECT build_seconds, maps_seconds, est_prep_min, est_maps_min FROM job_origin "
            f"WHERE status IN ({marks}) AND usage_counted = 1 AND build_seconds IS NOT NULL "
            "AND maps_seconds IS NOT NULL AND est_prep_min IS NOT NULL AND est_maps_min IS NOT NULL "
            "AND created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (*JOB_DONE_STATUSES, cutoff, ESTIMATE_CALIB_JOBS),
        ).fetchall()
    prep_ratios = [
        max(0.0, b - m) / 60 / ep for b, m, ep, _em in rows if ep >= 0.5
    ]
    maps_ratios = [m / 60 / em for _b, m, _ep, em in rows if em >= 0.5]
    result = {
        "prep": _calib_factor(prep_ratios),
        "maps": _calib_factor(maps_ratios),
        "jobs": len(rows),
    }
    if now is None:
        _calib_cache = (mono, result)
    return dict(result)


def record_download(job_id: str, nbytes: int | None) -> None:
    """Stažení výstupního ZIPu (počet a velikost; bez údajů o stahujícím)."""
    with connect() as conn:
        _add_usage(conn, "download_count", 1)
        if nbytes:
            _add_usage(conn, "download_bytes", max(0, int(nbytes)))
        conn.execute(
            "UPDATE job_origin SET downloads = downloads + 1 WHERE job_id = ?", (job_id,)
        )
        conn.commit()


def usage_totals() -> dict[str, dict[str, Any]]:
    """Součty použití: ``{klíč: {"value": číslo, "since": ISO čas}}`` (chybějící klíč = zatím nic)."""
    with connect() as conn:
        rows = conn.execute("SELECT key, value, since FROM usage_totals").fetchall()
    return {r["key"]: {"value": r["value"], "since": r["since"]} for r in rows}


def recent_usage(days: int, *, now: datetime | None = None) -> dict[str, Any]:
    """Souhrn za posledních ``days`` dní z job_origin (veřejné i privátní joby; jen počty).

    ``users`` je odhad počtu uživatelů = různé osolené značky IP (``ip_tag``) mezi všemi
    zadáními; nepřesný (sdílená IP, změna IP), ale bez osobních údajů.
    """
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
    marks = ",".join("?" * len(JOB_DONE_STATUSES))
    with connect() as conn:
        row = conn.execute(
            f"SELECT SUM(CASE WHEN usage_counted = 1 AND status IN ({marks}) THEN 1 ELSE 0 END), "
            f"AVG(CASE WHEN usage_counted = 1 AND status IN ({marks}) THEN build_seconds END), "
            "COUNT(DISTINCT ip_tag), MIN(created_at) FROM job_origin WHERE created_at >= ?",
            (*JOB_DONE_STATUSES, *JOB_DONE_STATUSES, cutoff),
        ).fetchone()
    return {
        "days": days,
        "jobs": int(row[0] or 0),
        "avg_build_seconds": row[1],
        "users": int(row[2] or 0),
        "since": row[3],  # nejstarší zadání v okně (statistika původu je od 3.14.0)
    }


def purge_job_origin(*, now: datetime | None = None, days: int | None = None) -> int:
    """Smaže záznamy původu starší než ``days`` (výchozí ORIGIN_RETENTION_DAYS)."""
    days = ORIGIN_RETENTION_DAYS if days is None else days
    if days <= 0:
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM job_origin WHERE created_at < ?", (cutoff.isoformat(),)
        )
        conn.commit()
        return cur.rowcount


def get_job(job_id: str, *, reveal_artifacts: bool = False) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise KeyError(job_id)
    return _row_to_job(row, reveal_artifacts=reveal_artifacts)


def get_job_by_download_token(token: str) -> dict[str, Any]:
    token = (token or "").strip()
    if not token:
        raise KeyError("token")
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE download_token = ?",
            (token,),
        ).fetchone()
    if not row:
        raise KeyError(token)
    return _row_to_job(row, reveal_artifacts=True, include_token=True)


def job_is_private(job: dict[str, Any]) -> bool:
    return bool((job.get("options") or {}).get("private"))


def job_expires_at(job: dict[str, Any]) -> datetime | None:
    """Konec platnosti privátního jobu (created_at + PRIVATE_JOB_RETENTION_HOURS)."""
    if not job_is_private(job):
        return None
    created = _parse_iso(job.get("created_at"))
    if created is None:
        return None
    hours = PRIVATE_JOB_RETENTION_HOURS
    if hours <= 0:
        hours = 48
    return created + timedelta(hours=hours)


def job_is_expired(job: dict[str, Any], *, now: datetime | None = None) -> bool:
    expires = job_expires_at(job)
    if expires is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now >= expires


def token_matches(job: dict[str, Any], token: str | None) -> bool:
    expected = (job.get("download_token") or "").strip()
    got = (token or "").strip()
    if not expected or not got:
        return False
    return secrets.compare_digest(expected, got)


def count_active_jobs_for_ip(client_ip: str) -> int:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM jobs
            WHERE status IN ('pending', 'queued', 'running')
              AND json_extract(options_json, '$.client_ip') = ?
            """,
            (client_ip,),
        ).fetchone()
    return int(row[0]) if row else 0


def count_active_api_jobs() -> int:
    """Běžící / čekající joby založené API klíčem (`options.api_client`)."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM jobs
            WHERE status IN ('pending', 'queued', 'running')
              AND json_extract(options_json, '$.api_client') = 1
            """
        ).fetchone()
    return int(row[0]) if row else 0


def count_api_jobs_since(since: datetime) -> int:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM jobs
            WHERE json_extract(options_json, '$.api_client') = 1
              AND created_at >= ?
            """,
            (since.isoformat(),),
        ).fetchone()
    return int(row[0]) if row else 0


def get_download_token(job_id: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT download_token FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return (row["download_token"] if row else None) or None


def count_jobs_for_ip_since(client_ip: str, since: datetime) -> int:
    since_s = since.isoformat()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM jobs
            WHERE json_extract(options_json, '$.client_ip') = ?
              AND created_at >= ?
            """,
            (client_ip, since_s),
        ).fetchone()
    return int(row[0]) if row else 0


def list_jobs(
    limit: int = 250,
    *,
    include_private: bool = True,
) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    jobs = [_row_to_job(r) for r in rows]
    if include_private:
        return jobs
    return [j for j in jobs if not job_is_private(j)]


def list_expired_private_jobs(
    *,
    now: datetime | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Privátní joby po splatnosti (včetně pending – odkaz už stejně neplatí)."""
    now = now or datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for job in list_jobs(limit=limit, include_private=True):
        if not job_is_private(job):
            continue
        if job.get("status") in ("running", "queued"):
            continue
        if job_is_expired(job, now=now):
            out.append(job)
    return out


_ZIP_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_zip_stem(name: str) -> str:
    """Název projektu → slug pro .zip (bez náhodných id)."""
    s = _ZIP_FORBIDDEN.sub("", (name or "").strip())
    s = s.casefold()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-.")
    if not s or s == "podkladarna":
        return "podkladarna"
    return s[:120]


def zip_download_filename(job_id: str, job_name: str) -> str:
    """Soubor ke stažení: podkladarna-<projekt>.zip; při shodě …-2, …-3."""
    stem = safe_zip_stem(job_name)
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name FROM jobs ORDER BY created_at ASC"
        ).fetchall()
    same: list[str] = []
    for row in rows:
        if safe_zip_stem(str(row["name"])) == stem:
            same.append(str(row["id"]))
    try:
        n = same.index(job_id) + 1
    except ValueError:
        n = 1
    if stem == "podkladarna":
        base = "podkladarna"
    else:
        base = f"podkladarna-{stem}"
    if n <= 1:
        return f"{base}.zip"
    return f"{base}-{n}.zip"


def delete_job(job_id: str) -> bool:
    """Smaže job z DB i disk (input/work/output). Neprovádí se pro běžící job."""
    job_dir = JOBS_DIR / job_id
    with connect() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return False
        if row["status"] in ("running", "queued"):
            return False
        # Konečný stav si statistika původu ponechá (job sám zmizí).
        conn.execute(
            "UPDATE job_origin SET status = ? WHERE job_id = ?",
            (row["status"], job_id),
        )
        conn.execute("DELETE FROM job_logs WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    return True


def update_job(job_id: str, **fields: Any) -> None:
    fields["updated_at"] = _utcnow()
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [job_id]
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", vals)
        conn.commit()


def mark_interrupted_running_jobs(
    reason: str, *, user_message: str | None = None
) -> list[str]:
    """Po restartu serveru – joby „running“ v DB uvolní frontu.

    ``reason`` jde do logu, ``user_message`` (je-li) na kartu jobu.
    """
    now = _utcnow()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status = 'running'",
        ).fetchall()
        ids = [row["id"] for row in rows]
        if not ids:
            return []
        conn.execute(
            """
            UPDATE jobs
            SET status = 'failed', phase = 'error', error = ?, updated_at = ?
            WHERE status = 'running'
            """,
            (user_message or reason, now),
        )
        conn.commit()
    for job_id in ids:
        append_log(job_id, f"CHYBA: {reason}")
    return ids


def list_queued_job_ids() -> list[str]:
    """Joby ve stavu „queued“ v pořadí vzniku (fronta je v paměti, po restartu se obnoví)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at",
        ).fetchall()
    return [row["id"] for row in rows]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _job_duration_s(row: sqlite3.Row) -> int | None:
    status = row["status"]
    if status in ("pending", "queued"):
        return None
    keys = row.keys()
    started = _parse_iso(row["started_at"] if "started_at" in keys else None)
    start = started or _parse_iso(row["created_at"])
    if start is None:
        return None
    if status == "running":
        end = datetime.now(timezone.utc)
    else:
        end = _parse_iso(row["updated_at"])
    if end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def _job_paths(job_id: str) -> dict[str, bool]:
    job_dir = JOBS_DIR / job_id
    lidar = job_dir / "work" / "lidar"
    has_laz = False
    if lidar.is_dir():
        has_laz = any(
            p.suffix.lower() in {".laz", ".las"} for p in lidar.iterdir() if p.is_file()
        )
    return {"has_reusable_lidar": has_laz}


def copy_reusable_work(src_id: str, dest_id: str) -> list[str]:
    """Převezme ořezaný LAZ z předchozího jobu (iterace). Vstupní data jsou ve sdílené cache.

    Hardlink místo kopie (LAZ se nepřepisují na místě, jen unlink + nový
    zápis). S dvojicí ground + veg se KP ``merged_crop`` nepřenáší.
    """
    from app.download_cache import (
        LEGACY_MERGED_LAZ_NAMES,
        lidar_pair_paths,
        link_or_copy,
    )

    copied: list[str] = []
    src = JOBS_DIR / src_id
    dest = JOBS_DIR / dest_id
    rel = "work/lidar"
    s, d = src / rel, dest / rel
    if not s.is_dir():
        return copied
    files = [p for p in s.iterdir() if p.is_file()]
    if lidar_pair_paths(s) is not None:
        files = [p for p in files if p.name not in LEGACY_MERGED_LAZ_NAMES]
    if not files:
        return copied
    d.mkdir(parents=True, exist_ok=True)
    for path in files:
        link_or_copy(path, d / path.name)
        copied.append(f"{rel}/{path.name}")
    return copied


def bbox_close(
    a: list | tuple, b: list | tuple, eps: float = 1e-5
) -> bool:
    if not a or not b or len(a) != 4 or len(b) != 4:
        return False
    return all(abs(float(x) - float(y)) < eps for x, y in zip(a, b, strict=True))


def find_duplicate_active_job(
    options: dict[str, Any],
    *,
    within_minutes: int | None = None,
) -> dict[str, Any] | None:
    """Stejný job (výřez + klíčové volby) už běží/čeká – typicky dvojklik Spustit.

    U aktivních jobů se neomezuje stářím: běh často trvá >5 min, dřívější okno
    5 minut dovolilo založit druhý stejný job (např. Švédské šance).
    ``within_minutes`` je jen zpětná kompatibilita volajícího kódu (ignoruje se).
    """
    del within_minutes

    def _fp(opts: dict[str, Any]) -> tuple:
        bbox = opts.get("bbox_wgs84") or []
        return (
            tuple(round(float(x), 5) for x in bbox) if len(bbox) == 4 else (),
            str(opts.get("map_scale") or ""),
            str(opts.get("contour_interval") or ""),
            bool(opts.get("output_zip", True)),
            bool(opts.get("output_georef")),
            bool(opts.get("output_references", True)),
            "building",  # dřív volba výplně dvorů (od 2.46.6 vždy budova) – otisk starých jobů beze změny
            resolve_formline_mode(opts),
            resolve_veg_smooth(opts),
            resolve_use_dtm(opts, "sprint"),
            resolve_use_dtm(opts, "forest"),
            bool(opts.get("sprint_residual_paved")),
            str(opts.get("sprint_residual_size") or "small"),
            str(opts.get("kp_cliff_symbol") or "auto"),
            str(opts.get("kp_cliff_sensitivity") or "low"),
            str(opts.get("kp_vege_height") or ""),
            resolve_osm_objects_mode(opts),
            bool(opts.get("kp_osm_footway_as_sidewalk")),
            str(opts.get("ostatni_plocha") or "small"),
            bool(opts.get("ostatni_plocha_as_403")),
            bool(opts.get("private")),
            str(opts.get("notify_email") or "").strip().casefold(),
            str(opts.get("client_ip") or ""),
            resolve_contour_smooth(opts),
            tuple(resolve_map_types(opts) or ()),
            bool(opts.get("output_sentinel")),
        )

    want = _fp(options)
    if not want[0]:
        return None
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status IN ('pending', 'queued', 'running')
            ORDER BY created_at DESC
            LIMIT 80
            """
        ).fetchall()
    for row in rows:
        job = _row_to_job(row)
        if _fp(job.get("options") or {}) == want:
            return job
    return None


def append_log(job_id: str, line: str) -> None:
    log_file = JOBS_DIR / job_id / "log.txt"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO job_logs (job_id, line, created_at) VALUES (?, ?, ?)",
                (job_id, line.rstrip(), _utcnow()),
            )
            conn.commit()
    except sqlite3.OperationalError:
        # Zamčená DB nesmí shodit krok pipeline (job 3082b1ae8292: „výplň oplocení přeskočena“); řádek je v log.txt.
        pass


def get_logs(job_id: str, after_id: int = 0) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, line, created_at FROM job_logs WHERE job_id = ? AND id > ? ORDER BY id",
            (job_id, after_id),
        ).fetchall()
    return [{"id": r["id"], "line": r["line"], "at": r["created_at"]} for r in rows]


# Doběhl s výstupem, ale část podkladů selhala (souhrn v ``error`` a na konci logu).
STATUS_DONE_WITH_ERRORS = "done_with_errors"
# Doběhl s výstupem (ZIP ke stažení, privátní mail).
JOB_DONE_STATUSES = frozenset({"done", STATUS_DONE_WITH_ERRORS})
# Job už nic nezapisuje do output/ (ZIP hotový nebo nevznikne).
JOB_FINISHED_STATUSES = JOB_DONE_STATUSES | {"failed"}


def _job_has_output(job_dir: Path) -> bool:
    out = job_dir / "output"
    return (out / "podkladarna_output.zip").is_file() or (out / "podkladarna_oom.zip").is_file()


def _job_has_georef_previews(job_dir: Path) -> bool:
    from app.pipeline.oom_preview import GEOREF_PREVIEWS_ZIP_NAME, list_georef_preview_files

    out = job_dir / "output"
    if (out / GEOREF_PREVIEWS_ZIP_NAME).is_file():
        return True
    return bool(list_georef_preview_files(out / "preview"))


def _job_source_meta(job_dir: Path) -> dict[str, Any] | None:
    path = job_dir / "work" / "source_meta.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


@lru_cache(maxsize=1024)
def _bbox_area_km2_cached(bbox: tuple[float, float, float, float]) -> float:
    from app.pipeline.fetch_openzu import bbox_area_km2

    return bbox_area_km2(*bbox)


def job_area_km2(options: dict[str, Any] | None) -> float | None:
    """Plocha mapy jobu v km² (výřez ``bbox_wgs84`` v S-JTSK, jako odhad v GUI)."""
    bbox = (options or {}).get("bbox_wgs84") or []
    if len(bbox) != 4:
        return None
    try:
        area = _bbox_area_km2_cached(tuple(float(x) for x in bbox))
    except Exception:  # noqa: BLE001 – plocha je jen informace, job kvůli ní nespadne
        return None
    return round(area, 2) if area > 0 else None


def _row_to_job(
    row: sqlite3.Row,
    *,
    reveal_artifacts: bool = False,
    include_token: bool = False,
) -> dict[str, Any]:
    from app.pipeline.preview import has_preview

    job_dir = JOBS_DIR / row["id"]
    keys = row.keys()
    started_at = row["started_at"] if "started_at" in keys else None
    token = row["download_token"] if "download_token" in keys else None
    paths = _job_paths(row["id"])
    source_meta = _job_source_meta(job_dir)
    options = json.loads(row["options_json"])
    is_private = bool(options.get("private"))
    # ZIPy (výstup / georef náhledy) až po doběhnutí jobu – během balení by
    # odkaz stáhl rozpracovaný archiv. Náhled PNG smí být vidět hned.
    finished = row["status"] in JOB_FINISHED_STATUSES
    has_out = finished and _job_has_output(job_dir)
    has_prev = has_preview(job_dir / "output", job_dir / "work")
    has_georef = finished and _job_has_georef_previews(job_dir)
    # Privátní job: bez tokenu neprozrazovat artefakty (náhled / ZIP).
    if is_private and not reveal_artifacts:
        has_out = False
        has_prev = False
        has_georef = False
    job: dict[str, Any] = {
        "id": row["id"],
        "name": row["name"],
        "preset_id": row["preset_id"],
        "status": row["status"],
        "phase": row["phase"],
        "options": options,
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": started_at,
        "duration_s": _job_duration_s(row),
        "area_km2": job_area_km2(options),
        "has_output": has_out,
        "has_oom": has_out,
        "has_preview": has_prev,
        "has_georef_previews": has_georef,
        "private": is_private,
        **paths,
    }
    expires = job_expires_at(job)
    if expires is not None:
        job["expires_at"] = expires.isoformat()
    if include_token and token:
        job["download_token"] = token
    from app.pipeline.dtm_summary import read_summary, warning_lines

    dtm_summary = read_summary(job_dir)
    if dtm_summary:
        # Web zvýrazní jen věty o skutečném problému (i u starších jobů bez ``warn_lines``).
        warn = warning_lines(dtm_summary)
        dtm_summary["warn_lines"] = [line for line in dtm_summary.get("lines") or [] if line in warn]
        job["dtm_summary"] = dtm_summary
    if source_meta:
        job["source_meta"] = source_meta
        job["dmp_mode"] = source_meta.get("dmp_mode")
        job["dmp_degraded"] = bool(source_meta.get("dmp_degraded"))
        job["dmp_leafless"] = bool(source_meta.get("dmp_leafless"))
        job["lidar_cache_max_age_days"] = LIDAR_CACHE_MAX_AGE_DAYS
    return job
