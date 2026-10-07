from __future__ import annotations

import json
import re
import secrets
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.settings import (
    DB_PATH,
    DOWNLOADS_DIR,
    JOBS_DIR,
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
        conn.commit()


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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
        conn.commit()
    return get_job(job_id)


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


def mark_interrupted_running_jobs(reason: str) -> list[str]:
    """Po restartu serveru – joby „running“ v DB uvolní frontu."""
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
            (reason, now),
        )
        conn.commit()
    for job_id in ids:
        append_log(job_id, f"CHYBA: {reason}")
    return ids


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
            bool(opts.get("sprint_courtyard_olive", True)),
            bool(opts.get("sprint_residual_paved")),
            str(opts.get("sprint_residual_size") or "small"),
            str(opts.get("kp_cliff_symbol") or "auto"),
            str(opts.get("kp_cliff_sensitivity") or "low"),
            str(opts.get("kp_vege_height") or ""),
            bool(opts.get("kp_osm_benches")),
            bool(opts.get("kp_osm_lamps")),
            bool(opts.get("kp_osm_playground_equipment")),
            bool(opts.get("kp_osm_priority")),
            bool(opts.get("kp_osm_footway_as_sidewalk")),
            str(opts.get("ostatni_plocha") or "small"),
            bool(opts.get("ostatni_plocha_as_403")),
            bool(opts.get("private")),
            str(opts.get("notify_email") or "").strip().casefold(),
            str(opts.get("client_ip") or ""),
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
    with connect() as conn:
        conn.execute(
            "INSERT INTO job_logs (job_id, line, created_at) VALUES (?, ?, ?)",
            (job_id, line.rstrip(), _utcnow()),
        )
        conn.commit()


def get_logs(job_id: str, after_id: int = 0) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, line, created_at FROM job_logs WHERE job_id = ? AND id > ? ORDER BY id",
            (job_id, after_id),
        ).fetchall()
    return [{"id": r["id"], "line": r["line"], "at": r["created_at"]} for r in rows]


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
    has_out = _job_has_output(job_dir)
    has_prev = has_preview(job_dir / "output", job_dir / "work")
    has_georef = _job_has_georef_previews(job_dir)
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
    if source_meta:
        job["source_meta"] = source_meta
        job["dmp_mode"] = source_meta.get("dmp_mode")
        job["dmp_degraded"] = bool(source_meta.get("dmp_degraded"))
    return job
