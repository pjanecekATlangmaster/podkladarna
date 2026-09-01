from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.settings import DB_PATH, JOBS_DIR


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
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
    for sub in ("input/dmr", "input/dmp", "input/zabaged", "work", "output"):
        (job_dir / sub).mkdir(parents=True, exist_ok=True)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, name, preset_id, status, phase, options_json, error, created_at, updated_at, started_at)
            VALUES (?, ?, ?, 'pending', NULL, ?, NULL, ?, ?, NULL)
            """,
            (job_id, name, preset_id, json.dumps(options), now, now),
        )
        conn.commit()
    return get_job(job_id)


def get_job(job_id: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise KeyError(job_id)
    return _row_to_job(row)


def list_jobs(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_job(r) for r in rows]


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
    temp = job_dir / "work" / "temp"
    dmr = job_dir / "input" / "dmr"
    has_laz = False
    for folder in (lidar, dmr):
        if not folder.is_dir():
            continue
        if any(p.suffix.lower() in {".laz", ".las"} for p in folder.iterdir() if p.is_file()):
            has_laz = True
            break
    has_temp = temp.is_dir() and any(temp.iterdir())
    return {"has_reusable_lidar": has_laz, "has_temp": has_temp}


def copy_reusable_work(src_id: str, dest_id: str) -> list[str]:
    """Zkopíruje vstupní LAZ / sloučený crop. ZABAGED se vždy stahuje znovu."""
    copied: list[str] = []
    src = JOBS_DIR / src_id
    dest = JOBS_DIR / dest_id
    for rel in ("input/dmr", "input/dmp", "work/lidar"):
        s, d = src / rel, dest / rel
        if not s.is_dir():
            continue
        files = [p for p in s.iterdir() if p.is_file()]
        if not files:
            continue
        d.mkdir(parents=True, exist_ok=True)
        for path in files:
            shutil.copy2(path, d / path.name)
            copied.append(f"{rel}/{path.name}")
    return copied


def bbox_close(
    a: list | tuple, b: list | tuple, eps: float = 1e-5
) -> bool:
    if not a or not b or len(a) != 4 or len(b) != 4:
        return False
    return all(abs(float(x) - float(y)) < eps for x, y in zip(a, b, strict=True))


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


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    job_dir = JOBS_DIR / row["id"]
    keys = row.keys()
    started_at = row["started_at"] if "started_at" in keys else None
    paths = _job_paths(row["id"])
    return {
        "id": row["id"],
        "name": row["name"],
        "preset_id": row["preset_id"],
        "status": row["status"],
        "phase": row["phase"],
        "options": json.loads(row["options_json"]),
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": started_at,
        "duration_s": _job_duration_s(row),
        "has_output": (job_dir / "output" / "podkladarna_output.zip").exists(),
        "has_preview": (job_dir / "output" / "pullautus.png").exists(),
        **paths,
    }
